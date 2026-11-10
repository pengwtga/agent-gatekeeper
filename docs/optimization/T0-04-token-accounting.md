# T0-04 输出上界与 token 成本可见

> 文件：`app/config.py`、`app/llm_client.py`、`app/main.py::health`
> 类型：成本与稳定性（输出无上界 + 成本完全不可见）

## 1. 问题

两件事，本质是同一个：

**① 输出没有上界。** 请求体里没有 `max_tokens`，模型输出多长完全由它决定。
分类任务的输出是固定 4 个字段的 JSON（约 150 token），理论上不需要长输出；
但模型一旦进入「解释性长文」，就会把一次 5 秒的调用拖到 20 秒以上 —— 在单进程服务里，
这 20 秒会一直占着一个事件循环的并发位。

**② 成本不可见。** `resp.json()["usage"]` 被直接丢弃。
没人知道一次分类花多少 token、哪个 Provider 更贵、一次 `/eval` 30 条要烧多少钱。
延迟和吞吐大家在监控里盯，成本只在账单上出现 —— 而账单是月底才来的。

## 2. 方案

**上界取 512。** 依据是输出结构固定：

```
root_cause_class (~5 token) + confidence (~4) + evidence (1~2 句, ~60) + suggested_action (~50) + JSON 骨架 (~30)
≈ 150 token
```

512 是 3 倍余量，足够容纳模型偶尔的啰嗦，又能挡住失控输出。取值不能太小：截断会让 JSON
不完整 → `_extract_json` 失败 → 触发重试或换 Provider，**重试的成本远高于多给的那几百 token**。
这是个容易被忽略的取舍：为了省 token 把上限压到 200，反而可能让失败率上升、总成本更高。

**成本记账做三层，从轻到重：**

| 层 | 实现 | 用途 |
|---|---|---|
| 单次日志 | 每次调用打 `provider / model / in / out` | 排查「这次为什么贵」 |
| 进程累计 | `LLMClient._usage` 字典，`/health` 暴露 | 看当前实例花了多少 |
| （T2 再加）指标 | Prometheus counter，按 provider/model 打标签 | 按维度看趋势、告警 |

现在是前两层。刻意没做第三层：没有真实流量时，指标只是自娱自乐。

## 3. 改动

`app/config.py`
```python
llm_max_tokens: int = 512
```

`app/llm_client.py`
- `_call` 的请求体加 `max_tokens`
- `_record_usage(provider, data.get("usage"))`：累加 `calls / prompt_tokens / completion_tokens`
- `usage_missing` 计数：免费档（glm-4-flash、部分中转站）可能不返回 `usage`，
  这种情况下**不能**当成 0 静默处理，否则成本统计会凭空偏低
- `finish_reason == "length"` 时打 warning：截断的根因在日志里直接可见，
  不用等 `JSONDecodeError` 再去猜
- `usage` property 返回快照（返回副本，避免调用方改到内部状态）

`app/main.py`
```python
"llm_usage": llm_client.usage,   # /health 暴露
```

## 4. 验收

```
PASS T0-4 请求体带 max_tokens  512
PASS T0-4 usage 记账  {'calls': 1, 'prompt_tokens': 120, 'completion_tokens': 30, 'usage_missing': 0}
```

`/health` 实测：

```json
{"status":"healthy","llm_configured":false,"llm_providers":[],
 "llm_usage":{"calls":0,"prompt_tokens":0,"completion_tokens":0,"usage_missing":0},
 "classes":["xflow_callback_lost","ea_sync_timeout","duplicate_submit","data_inconsistency","frontend_param_error"]}
```

## 5. 边界与遗留

- **多 worker 各算各的**。`_usage` 是进程内变量，`gunicorn -w 4` 下 `/health` 只会显示其中一个 worker
  的数字。生产应改成 Prometheus counter（进程无关，按 worker 打标签聚合）。现在单容器单进程，不误导。
- **没有单价换算**。只记 token 不记钱，是因为三家 Provider 的计价规则不同且会变；
  把价格写进代码是个维护负担。要看钱就在 Grafana 里乘。
- **`max_tokens` 不控制输入**。system prompt + few-shot 每次约 700 token，随 prompt 版本变化。
  输入侧的成本优化属于 T3（prompt 版本化 + 精简 few-shot），这里不动。
- **未做**：按请求累计成本并回报给调用方（`X-Cost-Tokens` 之类）。当前接口是同步返回，
  加这个头会打破「一次请求一次响应」的简单模型，收益不大。

## 6. 面试讲法（40 秒）

> Agent 服务有三个瓶颈：延迟、吞吐、成本。前两个大家在监控里盯，成本只在账单上出现。
>
> 我做了两件小事：给 `max_tokens` 定上界，和把 `usage` 记下来。上界取 512 不是拍脑袋 ——
> 输出是固定 4 字段的 JSON，约 150 token，512 是 3 倍余量。这里有个反直觉的点：
> 上限压太狠会截断 JSON，解析失败触发重试，**总成本反而更高**。
>
> 记账分三层：单次日志、进程累计、指标。我现在只做到第二层，因为没真实流量时指标是自娱自乐；
> 但 `usage_missing` 这个计数我特意留着 —— 免费档不返回 usage，如果当成 0 静默处理，
> 成本统计会凭空偏低，这种「静默失真」比没有统计更危险。
