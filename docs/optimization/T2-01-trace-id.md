# T2-1 trace_id 与请求上下文

## 1. 问题

改一行 prompt 之后，线上出现「某个客户说分类错了」。这时候要回答三个问题：

1. 那次请求是哪一次？（有没有唯一标识）
2. 它当时调了哪个 provider、走了几次重试、为什么降级？
3. 同一个时间段其他请求正常吗？

改之前一个都答不上来。日志里只有一行行互不相干的文本，
`grep "分类失败"` 能捞出 20 条，但分不清哪条属于同一个请求。

## 2. 判断

**观测的起点不是指标，是「一次请求」这个边界。**

没有请求边界，后面所有东西都做不成：

| 想做 | 缺 trace_id 时的结果 |
|---|---|
| 一次请求的完整链路 | 打不出来，只能靠时间戳猜 |
| 用户报障定位 | 「大概是 10 点那次」——查不了 |
| 跨服务串联（nginx → api → 未来的人工工单系统） | 串不起来 |
| 日志采样、按请求聚合成本 | 做不到，只能算总量 |

所以顺序必须是：先有 trace_id，再有结构化日志，最后才有指标。
反过来先做指标，会得到一堆没有归属的数字：P95 是 800ms，但不知道是谁慢。

## 3. 方案取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **contextvars** | 隐式上下文，业务代码零参数 | ✅ 采用 |
| 函数参数逐层透传 | 显式、可测；但 路由 → 分类器 → LLM 客户端 → httpx 四层都要加参数，漏一处断链 | ❌ |
| 全局变量 | 并发请求互相覆盖 | ❌ 直接错 |
| OpenTelemetry 自动注入 | 标准、生态好；但引入 SDK + exporter，当前无后端可发 | ⏸ 留到 T7-3 |

**为什么 contextvar 是对的**：asyncio 每个请求是一个 Task，Task 有独立的 context 副本。
`set_trace_id` 只在当前请求生效，不需要加锁，也不会串到并发请求上。
这一点在验收里专门用 3 个并发请求验证过（7.1）。

**中间件形态：纯 ASGI 而不是 `BaseHTTPMiddleware`。**

`BaseHTTPMiddleware` 会把下游包进 anyio 的流式任务，两个后果：

1. 流式响应（未来的 SSE）里 contextvar 可能丢失
2. 首字节延迟被吃掉一部分

纯 ASGI 只包一层 `send`，零缓冲。代价是要自己处理 `message["type"]` 分支，
多写 20 行，但换来的是「以后加 SSE 不用改这里」。

**上游 header 的信任问题。**

复用上游传来的 `X-Trace-Id` 是对的（跨服务串联），但不能无条件信任：

- 超长 header 会让日志行爆炸
- 带 `\n` 的 header 是**日志注入**（伪造出一行看起来正常的日志）和 **header 注入**

所以加了白名单正则 `^[A-Za-z0-9._-]{8,64}$`，不匹配就重新生成。
验收 1.4 专门测了带换行的输入被拒绝。

## 4. 改动

新增 `app/obs.py`（trace + 日志 + token 累加器三者共用一套上下文）：

```python
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")

def pick_trace_id(incoming: Optional[str]) -> str:
    if incoming and _TRACE_RE.match(incoming):
        return incoming
    return new_trace_id()
```

中间件（`app/main.py` 注册，一行）：

```python
app.add_middleware(ObservabilityMiddleware)
```

它做三件事：生成/复用 trace_id → 计时 → 发结构化 access 事件。
响应头统一补 `X-Trace-Id`，前端报错截图直接带 id。

响应体也挂一份（`attach_obs`）：

```json
{"root_cause_class": "ea_sync_timeout", "trace_id": "trace-0001-abcd", "usage": {...}}
```

## 5. 验收

| # | 项 | 结果 |
|---|---|---|
| 1.1 | 响应头带 `X-Trace-Id` | ✅ `d37dd7ad5ce14d23` |
| 1.2 | 格式为 16 位 hex | ✅ |
| 1.3 | 上游合法 id 被复用 | ✅ `client-abc-12345` |
| 1.4 | 带换行的非法 id 被丢弃重建 | ✅ `3e199876106d44fd` |
| 2.1 | 一次请求的多行日志共用同一 id | ✅ `llm_usage / llm_call / http_request` |
| 7.1 | 3 个并发请求 id 不串 | ✅ `conc-0/1/2` 各自独立 |

## 6. 边界

- **单进程有效**。多 worker 时 trace_id 仍然对（每个请求自己的上下文），
  但跨进程链路要落到存储才查得到 —— 这是 T7-2（trace 落 SQLite）要解决的。
- **不进响应体的错误页**：nginx 层的 429/502 到不了应用，没有 trace_id。
  要补得在 nginx 生成 `$request_id` 并透传，当前没做。
- **不带业务 ID**。trace_id 是技术标识，不等于 incident_id；
  真正的关联应该用 incident_id，两者都记在 `http_request` 事件里可互相查。
- contextvar 在 `run_in_executor` / 线程池里会丢。当前代码没有线程池调用，
  一旦引入（比如把 embedding 挪到线程里跑）要显式 `copy_context()`。

## 7. 面试讲法

> 观测的第一步不是上指标，是给「一次请求」画个边界。我先做 trace_id，
> 用 contextvar 存 —— 因为 Agent 的调用链是 路由 → 分类器 → LLM 客户端 → httpx，
> 中间还夹着容灾重试循环，逐层传参数一定会漏。
>
> 两个细节我觉得值得说：一是中间件用纯 ASGI 不用 BaseHTTPMiddleware，
> 后者会把响应包成流式任务，以后加 SSE 时 contextvar 会丢、首字节也会变慢；
> 二是上游传来的 trace_id 我做了白名单校验，因为带换行的 header 是日志注入，
> 无条件信任等于让别人往我的日志里写假行。
