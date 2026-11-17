# T1-7 重试与容灾在抢同一份预算

## 1. 问题

配好第二家 provider（硅基流动 Qwen2.5-7B）后，`/health` 已经列出两家，但真实请求仍然
降级到规则基线。trace 里看得清清楚楚：

```
http          12105ms
classify      12104ms  degraded=True
llm.try        7134ms  provider=zhipu  attempt=1    ← 超时
llm.try        4970ms  provider=zhipu  attempt=2    ← 预算只剩这条，还是超时
（siliconflow 一次都没被尝试）
```

原因是两个参数各自看都合理、乘在一起就不成立：

```
llm_max_retries = 1          → 单家最多 2 次尝试
llm_timeout_per_try_ms = 7000 → 2 × 7000 = 14000ms
request_budget_ms = 12000     → 第一个 provider 就把预算吃干
```

**结论先说：加了备用 provider 不等于有容灾。** 容灾链的每一步都要从同一份 budget 里切片
（T1-1），而「原地重试」把切片机会花在了同一家身上。

## 2. 判断

重试和切换解决的不是同一类失败，占用同一份预算时必须分主次：

| 失败类型 | 正确动作 | 理由 |
|---|---|---|
| 同一家偶发抖动（连接重置、429 突发） | **重试** | 换一家要重新建连、重新喂 prompt，成本更高 |
| 同一家持续超时/挂了 | **切换** | 重试只是把同一份等待再来一遍 |

免费档 provider 的失败模式恰恰是**第二类**：不是抖一下，而是持续 7s 不出结果。
这时候再试一次等于把「换个活路」的时间花在「再等一遍」上。

所以判据不是「要不要重试」，而是**这家值不值得重试**：

- 单次 1~2s 的 provider（deepseek 付费档）→ 重试 1 次合理，2 次才 4s，还在预算内
- 单次 4~7s 的免费档 → 重试一次就吃掉半个预算，必须关掉，把机会留给下一家

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ 关掉单家重试（`llm_max_retries=0`），预算给到 15000 | 每次容灾链 = 每家一次机会；改动只在 `.env`，代码默认值不动（换深寻时调回 1） |
| ❌ 保持重试、把预算提到 28000 | 超过 nginx 20s 的线，网关先 504、后端还在烧 token（T1-4 的原始问题） |
| ❌ 保持重试、把 per_try 压到 3000 | 免费档 p50 就 6.3s，等于每次都必超时，重试变成纯浪费 |
| ⚠️ 链路级重试（整条链失败后再来一轮） | 更贵：一轮已经是 2 家 × 7s；只有单家 <1s 时才值得 |

## 4. 改动

`.env`（本地部署参数，代码默认值保持不变）：

```ini
SSRM_AIOPS_LLM_TIMEOUT_PER_TRY_MS=7000
SSRM_AIOPS_LLM_MAX_RETRIES=0        # 每家只给一次机会
SSRM_AIOPS_REQUEST_BUDGET_MS=15000  # 7000 × 2 家 + 1s 余量，仍 < nginx 20s
```

`.env.example` 补了「超时三元组」注释块，把约束写成公式：

```
per_try × provider 数 ≤ request_budget < nginx proxy_read_timeout
```

代码侧没有改动——这本来就是配置层的因果，把它写进代码反而会让「换 provider 时该调什么」不可见。

## 5. 验收

同一个请求（`http_status=504 / retry_count=3 / downstream=ea`）改后 trace：

```
http          12490ms
classify      12488ms
llm.try        7154ms  provider=zhipu       attempt=1   ← 超时，切下一家
llm.try        5334ms  provider=siliconflow attempt=1   ← 成功
```

接口返回：`source=llm, llm_provider=siliconflow, evidence_mode=refs, evidence_score=1.0,
confidence=0.97, needs_human_review=false`。

两家 provider 五条对照（隔离单家跑真实分类链路）：

| Provider | 走 LLM 通道 | 分类命中 | p50 | 单次区间 |
|---|---|---|---|---|
| 智谱 glm-4-flash | 4/5（1 条 14.2s 撞超时降规则） | 5/5 | 6324ms | 5.2~14.2s |
| 硅基 Qwen2.5-7B | **5/5** | **5/5** | 6258ms | 4.1~6.6s |

质量相当，硅基略快、超时更少；顺序保持「智谱优先、硅基兜底」——智谱健康时 6s 出结果，
不健康时 12.5s 也能拿到同样的结论。

## 6. 边界

1. **`per_try` 要 ≥ 单次 p90**。实测免费档 p90 约 7s，所以 7000 是下限；再压低就变成
   「每家都必然超时」，容灾链退化成三次空转。
2. **预算余量从 8s 缩到 5s**。T1-4 的口径是 `budget 12s + 余量 8s = nginx 20s`；改成 15s 后
   余量是 5s。仍然满足「nginx > 下游最坏承诺」，但给 nginx 的缓冲变薄了——如果再加第三家
   provider，必须重新算，不能只加 key 不改预算。
3. **`retries=0` 放弃了抖动重试**。连接重置这类瞬时失败现在会直接切下一家（成本是一次
   重新建连 + prompt 重发）。在免费档上这是划得来的；付费档 1~2s 的 provider 应调回 1。
4. **没有做健康度感知**。理想形态是记住每家的近期失败率，失败率高的排到后面或降权；
   现在的顺序是静态的，靠人决定。

## 7. 面试讲法

> 我给系统加了第二家 LLM provider，键配上、健康检查也显示两家，但真实请求还是走降级。
> 查 trace 发现：单家重试两次，7.1s 加 5.0s 就把 12s 预算吃干了，第二家一次都没被尝试。
> 这事的教训是——**加了备用 provider 不等于有容灾**。重试和切换在抢同一份预算，
> 而免费档 provider 的失败模式是持续超时，不是抖一下，重试等于把换活路的时间花在再等一遍。
> 我把单家重试关掉、预算按「每家一次机会」重算成 15s，trace 里第一次看到
> zhipu 超时 → siliconflow 成功 这条完整链路。这类问题的味道是：
> 每个参数单独看都对，乘在一起才失效，所以约束必须写成一条公式而不是三个数值。
