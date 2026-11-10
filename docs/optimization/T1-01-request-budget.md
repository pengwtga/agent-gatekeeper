# T1-1 请求级超时预算

## 1. 问题

改预算前，一次 `/classify` 的最坏耗时没有任何一处负责：

```
最坏 = 3 provider × 30s 超时 × (1 重试 + 1) = 180s
```

而 nginx `proxy_read_timeout=120s`。也就是说：**nginx 会先 504 断开，后端还在继续烧 token**。用户的体验是 504，成本照付，且这个慢请求还占着事件循环和连接。

各段超时（httpx timeout、nginx timeout、uvicorn）互相独立时，「这次请求总共允许花多久」这个问题没有答案。

## 2. 判断

超时治理的正确模型不是「每段配一个超时」，而是「请求入口发一个预算，下游每个耗时操作从预算里切时间」。区别在于：

- **分段超时**：每段的上界之和可以远大于任何一方的预期（3×30×2=180s）
- **预算制**：总时长有硬上界；预算花完，后面的路径直接跳过走降级

这也是分布式系统里 deadline propagation 的最小实现：下游拿到的不是「我能等多久」，而是「还剩多少」。

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ 请求入口 `Budget`，逐层传递 | 总时长有上界；代码量 ~80 行；缺点是要在调用链上多传一个参数 |
| ❌ 只调小各段超时 | 3×3s×2=18s 仍超 12s 预算，且每段 3s 对慢 provider 太乐观，治标不治本 |
| ❌ asyncio.wait_for 包整个 handler | 能掐断，但掐断时 LLM 调用已经发出，token 照烧；预算制在**发出前**就放弃 |

关键取舍：**预算不够时「跳过」而不是「硬着头皮调」**。一个注定超时的 LLM 调用只会白烧 token 和连接，规则基线几毫秒就能给出可用结果。

## 4. 改动

- 新增 `app/budget.py`：`Budget(total_ms)` + `remaining_ms()` / `slice(want_ms)` / `exhausted()`。
  用 `time.monotonic()`（不受系统对时回拨影响）；`MIN_TRY_MS=300` 以下不再发起调用
- `config.py`：`request_budget_ms=12000`、`llm_timeout_per_try_ms=3000`
- `llm_client.chat_json(..., budget)`：遍历 provider 前检查剩余预算，不足则停止容灾遍历；
  `_call_with_retry` 的**每次尝试（含重试）都重新切片**——第一次失败烧掉的预算会让重试的
  时间片自动变小，而不是还按满额去等
- `_call(..., read_timeout_s)`：按切片覆盖 httpx 的 read 超时（connect 上限 3s）
- `classifier.classify(incident, budget)` / `rag.assist(question, budget)` 贯穿
- `main.py`：`/classify`、`/assist`、`/eval`（每条一行预算）入口发放；
  响应带 `budget_ms_remaining` 便于观测

## 5. 验收

真实 TCP 慢服务器（读完请求后 sleep 5s）+ 预算 2000ms + 单次时间片 800ms：

```
2.1 慢 provider 被预算掐断，抛 LLMError        PASS
2.2 总耗时 1.71s < 2.8s（旧实现 2×30s=60s）    PASS
2.3 尝试次数 2 次 ≤ 4                          PASS
2.4 预算不足 300ms 时零调用直接跳过            PASS
```

注意：**验收必须用真 socket**。httpx 的 MockTransport 会绕过超时机制（handler sleep 多久都"成功"），mock 测不出超时语义——这也是本次踩到的测试陷阱。

## 6. 边界

- 预算只管 LLM 调用；embedding、向量检索不在预算内（单机内存操作，微秒级）
- 最坏路径 3 provider × 3s × 2 = 18s > 12s 预算 → 预算会掐掉尾部 provider，**这是有意的**：
  宁可降级规则基线，也不超过 nginx 的 504 线
- 多 worker 部署时预算依然是进程内概念，无需共享（每个请求独立）

## 7. 面试讲法

> 我最初每个 LLM 调用各配 30 秒超时，算完账发现最坏 180 秒，而 nginx 120 秒就 504 了——
> 各段超时互相独立时，总时长没人负责。所以改成请求入口发预算、下游切片的模型：
> 每次重试都从剩余预算里切，切不出来就直接降级规则基线并标 degraded。
> 这里有个测试陷阱值得说：httpx 的 MockTransport 不走真实超时，验收必须起真 socket 服务。
