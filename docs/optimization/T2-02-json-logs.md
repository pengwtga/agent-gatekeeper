# T2-2 结构化 JSON 日志

## 1. 问题

改之前的日志长这样：

```
2026-11-12 21:03:11,482 [WARNING] app.classifier [RootCauseClassifier] LLM 降级到规则基线: 所有 LLM Provider 均失败: primary: ...
```

人要读懂它没问题，但**机器读不懂**。后果是：

- 想知道「降级率」只能靠 `grep -c` 数行数，正则一改就崩
- 想知道「P95 延迟」完全没有数据 —— 耗时根本没打
- 想知道「这次降级是超时还是预算耗尽」得肉眼读中文
- `trace_id` 不存在，日志之间无法关联

## 2. 判断

**日志不是给人读的文本，是一张宽表。** 每行是一个事件，每个字段是一列。

一旦按这个视角设计，字段就必须固定下来，否则无法 `group by`：

| 字段 | 作用 |
|---|---|
| `ts` / `level` / `logger` | 基础定位 |
| `trace_id` | 关联同一请求的所有行 |
| `event` | **事件类型**，等价于「表名」：`http_request` / `llm_call` / `classify_degrade` |
| `stage` | 发生在哪一层：`boot` / `llm` / `classify` / `rag` / `gate` |
| `duration_ms` | 耗时，唯一能算 P95 的来源 |
| `outcome` | `ok` / `degraded` / `fail` / `retry` / `rejected`，一眼看出好坏 |
| `provider` / `model` | 哪个上游、哪个模型 |
| `tokens` | 这次调用花了多少 |
| `reason` | 失败/降级的简短原因（截断 200 字） |

**关键判断：`event` 和 `msg` 分离。** 大多数实现把一整句中文当 event，
结果就是「事件类型」有几百种取值，等于没有类型。现在 `event` 是枚举值
（`llm_call`、`http_request`…），`msg` 才是人话。

## 3. 方案取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **自定义 `JsonFormatter` + 白名单字段** | 30 行，零依赖；字段可控 | ✅ |
| `python-json-logger` | 少写代码，但多一个依赖，且默认把 `extra` 全倒出来 | ❌ |
| structlog | 能力强（绑定上下文、processor 链）；但团队/项目规模不值 | ❌ 记在 T7 备选 |
| 保留 uvicorn 原生 access log | 现成；但纯文本会打断 `jq` | ❌ 自己发 |

**为什么白名单而不是「`extra` 全倒进去」**：`extra` 里混进不可 JSON 序列化的对象
（异常、ORM 实体、datetime）时，`json.dumps` 抛错，而 logging 的默认行为是
**静默吞掉整条日志**。也就是说：写日志的代码没错，但日志丢了，而且没人知道。
白名单 + `default=str` 把这类问题挡在原地。

**为什么不用字符串模板拼 JSON**：

- 中文转义、引号、换行都要自己处理，拼错一次就是一行坏数据
- `logger.info("%s", x)` 的延迟求值特性会丢

## 4. 改动

`app/obs.py`：

```python
class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": ..., "level": record.levelname,
            "logger": record.name, "trace_id": current_trace_id(),
        }
        payload["event"] = getattr(record, "event", None) or record.getMessage()
        for key in _ALLOWED_EXTRA:            # stage/provider/duration_ms/... 白名单
            if (v := getattr(record, key, None)) is not None:
                payload[key] = v
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).strip().splitlines()[-1][:300]
        return json.dumps(payload, ensure_ascii=False, default=str)
```

`setup_logging()` 替换 root handler，并把 `uvicorn` / `uvicorn.error` 挂到 root
（否则容器里会同时存在两份格式）。

业务侧调用变成：

```python
logger.info("LLM 调用完成", extra={
    "event": "llm_call", "stage": "llm", "provider": provider.name,
    "model": provider.model, "duration_ms": duration_ms, "tokens": total, "outcome": "ok",
})
```

同时把 access log 自己发（`ObservabilityMiddleware`），因为需要
`trace_id` / `duration_ms` 这些 uvicorn 给不了的字段。Dockerfile 加 `--no-access-log`。

`SSRM_AIOPS_LOG_FORMAT=text` 保留纯文本模式：本地 `uvicorn app.main:app` 调试时
JSON 反而难读，这个开关不能省。

## 5. 验收

**容器内实测**（构建镜像 → 起容器 → 请求 → 逐行解析）：

```
总行数=10  解析失败=0
事件: {app_start:0, rag_mode_degraded:0, rag_ready:0, http_request:3, ...}
纯文本行: []
```

access 事件样例：

```json
{"ts":"2026-11-12T22:03:48.282","level":"INFO","logger":"app.access",
 "trace_id":"1fb0ebbbe4af440c","event":"http_request","msg":"GET /metrics -> 200",
 "duration_ms":0.5,"outcome":"ok","method":"GET","path":"/metrics","status":200}
```

T2 验收脚本另外覆盖了 2.1~2.5：字段壳完整、每行合法 JSON、
一次请求的三类事件（`llm_call` / `llm_usage` / `http_request`）共用同一 `trace_id`。

**容器里才发现的一个坑**：`--no-access-log` 只是把 `uvicorn.access` 的 handler
清空，而 `setup_logging()` 如果给它设了 `propagate=True`，记录会重新冒到 root ——
于是日志里又出现一份纯文本 access log。第一版就是这样，本机用 TestClient 完全
测不出来（TestClient 不经过 uvicorn 的日志配置）。修法是显式
`access.disabled = True`。

## 6. 边界

- **绝不记录请求体**。`/assist` 的 `question`、`/classify` 的 `incident` 里可能有
  供应商名称、联系人、工单内容。只记 `path`（不含 query），不记 body。
  要排查具体输入，走 T7-1 的录制文件（有权限、可清理），不要进日志。
- **密钥永不入日志**。`provider.base_url` 可以记，`api_key` 不可以（当前代码没有
  任何路径会打它，这条是约束而非现状）。
- **日志量没有采样**。单实例 QPS 个位数时不必要；上量后按 `trace_id` 哈希采样
  1% 或只保留 `outcome != ok` 的全量 + 正常的 1%。
- **`json.dumps` 有成本**（每次约 10~20μs）。当前每请求 3~5 行可忽略；
  若某个循环里打日志（如 /eval 30 条），会把总耗时推高，届时按级别降噪。

## 7. 面试讲法

> 我把日志当表设计：`event` 是表名，`trace_id` 是主键的一部分，
> `outcome` 是状态列。这样「降级率」「P95」「哪个 provider 老失败」
> 都变成一次 `group by`，而不是写正则数行数。
>
> 两个细节只有实际跑容器才会发现：一是 uvicorn 的 `--no-access-log` 只清 handler，
> 你要是又给这个 logger 开了 propagate，纯文本 access log 会重新冒出来，
> 日志里就有两份格式；二是 access log 必须自己发，因为 uvicorn 那行里没有
> trace_id 和耗时字段，而这两样恰恰是排查时最需要的。
