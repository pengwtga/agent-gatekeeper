# T1-6 接入智谱 Key 后暴露的三个启动期问题

> 起因：接上真实 Provider（智谱 glm-4-flash）后，服务起不来、日志里全是 `reason: ""`。
> 三个问题都不是「配错了 key」，而是**接入真实上游之后才现形的设计缺陷**。

## 1. 问题

### 1.1 启动被 embedding 卡死（最严重）

配好 key 后 `uvicorn` 启动，`/health` 连续 90 秒不响应，容器里就是「起来了但不接流量」。
日志显示：

```
embedding 429 限流，退避重试 attempt=1 reason=backoff=2s
embedding 429 限流，退避重试 attempt=2 reason=backoff=4s
embedding 429 限流，退避重试 attempt=3 reason=backoff=8s
embedding 429 限流，退避重试 attempt=4 reason=backoff=16s
```

三层原因叠在一起：

1. `lifespan` 里 `await rag.warmup()` **同步等待**全部工单向量化完成才 `yield`，
   即「索引没建好就不接请求」；
2. 30 条工单按 `BATCH_SIZE=10` 分 3 批，每批失败重试 4 次（2+4+8+16=30s），
   最坏耗时 = 3 × 30s = **90 秒**；
3. 更关键的是，这个 429 根本不是限流 —— 是**余额不足**。

```bash
curl -s https://open.bigmodel.cn/api/paas/v4/embeddings \
  -H "Authorization: Bearer $KEY" -d '{"model":"embedding-2","input":["x"]}'
# {"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}
```

智谱把「余额不足」也包装成 HTTP 429，与真正的限流共用状态码。
只按状态码判断，就会对一个**永远不会成功**的请求重试 4 次，还指数退避。

### 1.2 失败日志的 `reason` 是空串

```
{"event": "llm_attempt_fail", "provider": "zhipu", "reason": ""}
```

原因：`httpx.ReadTimeout` 的 `str(e)` 就是空字符串。代码只记了 `str(e)`，
于是「超时」和「任何没有消息的异常」在日志里长得一模一样 —— 排查时只能靠猜。
这正是 T2 想解决的问题（日志要能直接归因），结果自己在最关键的字段上漏了。

### 1.3 单次时间片 3s 对慢模型偏紧

实测 glm-4-flash 单次耗时 3.7~6.2s（约 1000 token 输入），而 `llm_timeout_per_try_ms=3000`。
10 次请求里有 2~3 次被自己的时间片掐成超时，然后降级规则基线 ——
**看起来像 provider 挂了，其实是我们没等够**。

> 中间踩了一次：先把时间片放到 4500ms，结果 10 次里还是有 3 次 ReadTimeout。
> 说明「取一次实测值 + 一点余量」这种拍脑袋不够 —— 免费档的延迟是**长尾**的，
> 最终按实测上界 6.2s 留 10% 余量取 7000ms。

## 2. 判断

| 问题 | 判断 |
|---|---|
| 启动阻塞 | 建索引是「尽快变好」，不是「不建好就不能服务」。卡住启动比退回关键词检索严重得多：K8s 探针会把 Pod 判死，滚动发布直接失败 |
| 429 重试 | 可恢复的 429（限流）值得退避；不可恢复的 429（没额度）必须立刻放弃。两者状态码相同，**只能看响应体** |
| 空 reason | 日志字段的可读性属于功能，不是装饰：`reason` 为空等于这条日志没有告警价值 |
| 时间片 | 这是**配置**问题不是代码问题：代码默认值（3000ms）对 deepseek 合理，对免费档小模型偏紧。放到 `.env` 覆盖，不动默认值 |

## 3. 方案

### 3.1 识别「不可恢复的 429」

```python
def _is_quota_exhausted(resp: httpx.Response) -> bool:
    err = resp.json().get("error") or {}
    return str(err.get("code")) == "1113" or "余额" in str(err.get("message", ""))
```

`_embed_batch_with_retry` 只在 `429 且不是没额度` 时才退避，其余直接抛。

> 为什么不按「重试次数上限」拍脑袋？因为限流与没额度都是 429，
> 靠次数上限区分不了；而错误码是上游给的**事实**。

### 3.2 预热加超时 + 显式降级

```python
try:
    await asyncio.wait_for(rag.warmup(), timeout=settings.warmup_timeout_seconds)  # 10s
except asyncio.TimeoutError:
    rag.degrade_to_keyword(f"warmup_timeout>{settings.warmup_timeout_seconds}s")
```

`degrade_to_keyword()` 是新增方法，把「放弃向量检索」这件事收口到一个出口 ——
原来这个状态是在 `warmup` 的 `except` 分支里就地改三个字段（`_embed_client=None`、
`_ready=True`、打日志），超时路径要复用它就得复制一遍。

### 3.3 异常文本兜底

```python
def _err_text(e: BaseException) -> str:
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__
```

httpx 超时 → `ReadTimeout`；HTTP 错误 → `HTTPStatusError: 400 ...`。
`errors` 列表、`llm_provider_fail`、`LLMError` 消息全部改用它，避免层层传递后变成 `zhipu: `。

### 3.4 本机放宽时间片（只改 `.env`）

```
SSRM_AIOPS_LLM_TIMEOUT_PER_TRY_MS=7000
```

三层对齐仍成立：`7000 × 2 次重试` 由 `request_budget_ms=12000` 的 `Budget.slice()`
逐次切分 —— 第二次尝试只会拿到剩下的 ~5s，总时长仍 < nginx 的 20s。

> 为什么不直接把代码默认值改成 7000？因为 3000 对 deepseek（1~2s）是合理的，
> 7000 会让「真挂了」的场景多等 4 秒才降级。**延迟预算跟 provider 走，不跟代码走** ——
> 这正是配置该承担的东西。

## 4. 改动

| 文件 | 改动 |
|---|---|
| `app/embeddings.py` | 新增 `_is_quota_exhausted()`；重试条件加「非没额度」 |
| `app/rag_assistant.py` | 新增 `degrade_to_keyword(reason)`，warmup 异常路径复用它 |
| `app/config.py` | 新增 `warmup_timeout_seconds=10` |
| `app/main.py` | `lifespan` 用 `asyncio.wait_for` 包住 warmup，超时降级关键词模式 |
| `app/llm_client.py` | 新增 `_err_text()`；三处日志与 `LLMError` 消息改用它 |
| `.env.example` | 智谱 Key 脱敏为占位符（真 Key 只留在 `.env`） |

## 5. 验收

| # | 项 | 结果 |
|---|---|---|
| 1 | 启动耗时 | 修复前 >90s 不可用；修复后 **< 1s** 即响应 `/health` |
| 2 | embedding 余额不足 | 1 次请求后立即降级，**不再重试**；`rag_mode_degraded` 一次 |
| 3 | 降级后检索可用 | `/assist` 返回 `mode=keyword+rerank`，Top-3 命中且有 LLM 摘要 |
| 4 | 空 reason | 失败日志变成 `"reason": "ReadTimeout"` / `"HTTPStatusError: ..."` |
| 5 | LLM 分类 | `source=llm`、`provider=zhipu`、conf 0.95~0.98、usage 有 token 记账 |
| 6 | 助手问答 | 连续 2 次 `provider=zhipu`、conf 0.8、answer 非降级文案 |
| 7 | 时间片 4500ms | 10 次里仍有 3 次 ReadTimeout（不够） |
| 8 | 时间片 7000ms | 连续 **6/6** 全部命中 LLM，p95 4.2s，降级率 0%（原来约 30%） |

实测启动日志：

```
app_start        provider=zhipu  budget=12000ms per_try=7000ms
rag_mode_degraded reason="Client error '429 Too Many Requests' ..."   ← 只一次，无重试
rag_ready        reason=keyword+rerank
```

## 6. 边界

- **embedding 依然不可用**：智谱 `embedding-2` 需要资源包，免费额度的只有 chat。
  所以本机长期运行在「关键词召回 + LLM 重排」模式，这是设计好的降级路径，不是故障。
  要真用上向量检索，得换 provider（硅基流动有免费 embedding）或充值。
- **预热超时后不会自愈**：进程内没有后台重建索引的逻辑，重启才会再试。
  真要做成「启动快 + 后台补索引」，需要加一个 `asyncio.create_task` 重建 + 就绪标志位切换，
  属于 T4（检索升级）的范围。
- **`warmup_timeout_seconds` 是粗粒度保险**：它保证「启动不会被拖死」，
  不保证「10s 内建完索引」。索引没建完就降级，是有意的取舍。

## 7. 面试讲法

> 「接真 key 之后服务起不来，第一反应是 key 配错了，其实是三个叠加的问题。
> 最外层是 lifespan 里同步等索引建完 —— 这是把『优化』当成了『前置条件』。
> 第二层是智谱把余额不足包成 429，我的重试逻辑只认状态码，对着一个永远不会成功的请求
> 指数退避了 90 秒。第三层是失败日志的 reason 是空串，因为 httpx 超时异常的 str 是空的。
>
> 这三个都不是配置错误，而是**接入真实上游才暴露的假设**：假设 429 一定是限流、
> 假设异常都有 message、假设索引必须先建好。修法分别是：看响应体区分错误码、
> 异常文本兜底成类名、预热加超时并降级。
>
> 顺带一个观察：把单次时间片从 3s 放到 7s，成功率从 70% 变成 6/6 ——
> 之前的『provider 不稳定』有相当一部分是我们自己的超时太紧造成的假故障。
> 中途我试过 4.5s，还是不够，说明余量不能拍脑袋，要按延迟长尾的上界来定。」
