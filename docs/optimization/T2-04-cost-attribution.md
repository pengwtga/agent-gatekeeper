# T2-4 成本归因（每请求 token）

## 1. 问题

T0-4 做了「进程累计 token」，能回答「这个服务一共花了多少」。但账单上多出 30% 时，
它回答不了最关键的问题：**是谁花的。**

- 是某个客户在刷？
- 是某次误发的 /eval（一次 30 条）？
- 还是某个 prompt 改完之后输出变长了？
- 还是重试变多、每次都重复付费？

## 2. 判断

**成本必须能归因到「一次请求」，否则无法优化。**

三个层次对应的动作完全不同：

| 粒度 | 能回答 | 对应动作 |
|---|---|---|
| 进程累计（T0-4） | 一共多少 | 只能看趋势 |
| **请求级（本任务）** | 这次多少 | 能定位异常请求、能进响应头 |
| 业务维度（租户/场景/模型） | 谁花的 | 能定价、能配额（未做） |

Agent 服务的成本有个特征：**同一次请求内会被收多次费**。
容灾链上第一个 provider 超时 → 第二个成功，前一次的 token 已经花掉了。
所以「一次请求的成本」必须把**失败的尝试也算进去**，否则会低估。
这也是为什么计数器挂在 `_record_usage`（每次拿到响应就记），而不是挂在
「最终成功」的分支上。

## 3. 方案取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **contextvar 累加器** | 深层代码（LLM 客户端）不需要知道调用方是谁 | ✅ |
| 把 acc 当参数传到 `chat_json` | 显式；但 llm_client 的签名会被观测需求污染 | ❌ |
| 从日志里事后聚合 | 零侵入；但响应里就没有，前端拿不到 | ⏸ 作为补充 |
| 直接算成钱（￥） | 直观；但价格表会变（免费档/促销/阶梯），算出来的数很容易过期 | ❌ 只记 token |

**为什么不记金额**：token 是事实，价格是解释。价格一变，历史数据全部作废；
而且免费档（glm-4-flash）单价是 0，算出来「这次请求花了 0 元」，
反而掩盖了它消耗的配额。展示层要换算再换。

**「最近一小时」而不是全历史**：成本问题都是「最近是不是花快了」，
一小时窗口能立刻反映，且 deque 自动裁剪不会无限增长。

## 4. 改动

`app/obs.py`（累加器，与 trace_id 同源）：

```python
_request_usage: ContextVar[Optional[dict]] = ContextVar("request_usage", default=None)

def add_request_usage(prompt_tokens, completion_tokens):   # 由 llm_client 调用
def current_request_usage() -> dict:                        # 由响应组装处调用
```

`app/llm_client.py`：

```python
add_request_usage(prompt_tokens, completion_tokens)   # 记到当前请求账上
metrics.record_tokens(prompt_tokens, completion_tokens)  # 记到最近一小时窗口
```

`attach_obs()` 把结果挂进响应体：

```json
{"root_cause_class": "ea_sync_timeout",
 "trace_id": "1f3952c4664346b5",
 "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "calls": 1}}
```

`calls` 字段不能省：`calls=2, total=150` 说明第一次失败了又重试，
这正是成本超预期的常见来源。

## 5. 验收

| # | 项 | 结果 |
|---|---|---|
| 3.2 | 响应体 `usage` 等于 provider 返回 | ✅ `{"prompt_tokens":120,"completion_tokens":30,"total_tokens":150,"calls":1}` |
| 4.3 | `/metrics` 最近一小时 total | ✅ 150 |
| — | 无 Key 场景 usage 为 0 且不报错 | ✅ `{"total_tokens":0,"calls":0}` |

容器内实测：`/metrics` 的 `tokens.last_hour.total` 与响应体 `usage.total_tokens` 一致。

## 6. 边界

- **免费档常常不返回 usage**（`usage_missing` 计数在涨）。此时 token 记 0，
  实际成本不为 0（有配额）。所以 `usage_missing` 本身是个要盯的指标：
  它高说明成本数据不可信。
- **`/eval` 的账会集中记在一次请求上**（30 条串行共用一个上下文）。
  这符合直觉（确实是一次请求触发的），但会掩盖「单条分类」的真实成本。
  正确做法是每条分类带独立的 `incident_id` 维度，属于 T7 的范围。
- **流式响应下 `usage` 拿不到**（SSE 的 usage 在最后一块或根本没有）。
  现在没有流式，接流式时要重新设计。
- **只记 token 不记「省下来的」**：缓存命中、规则直出（没调 LLM）都记 0，
  这其实是最有价值的信息（说明规则通道在省钱），但没有单独计数。
  想量化要加一个 `rule_shortcut` 计数。
- **多 worker 下 `process_total` 会分裂**，同 T2-3。

## 7. 面试讲法

> 成本优化最先要解决的不是「怎么省」，是「钱花在哪」。我把 token 记到请求级，
> 挂进响应体，这样任何一个异常请求都能立刻看出它花了多少。
>
> 有个细节是我特意做的：计数器挂在「每次拿到响应」而不是「最终成功」，
> 因为容灾链上第一个 provider 超时的时候，钱已经花掉了。如果只统计成功的那次，
> 成本会被低估 —— 而「重试变多」恰恰是成本超预期最常见的原因。
> 所以我在响应里还带一个 `calls` 字段，`calls=2` 就是重试过的证据。
>
> 我也没把 token 换算成钱，因为价格表会变、免费档价格是 0，
> 算出来的数反而误导。token 是事实，价格是解释，解释放到展示层。
