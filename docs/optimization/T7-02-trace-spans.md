# T7-2（一阶段）请求链路：从「能 grep」到「能点开」

## 1. 问题

T2-1 给每个请求发了 trace_id，T2-2 让每条日志都带它。但值班时的真实问题是：

> 「这条 `/assist` 花了 8 秒，是检索慢、重排慢，还是模型慢？」

日志能回答「耗时 8 秒」，回答不了「8 秒花在哪」——因为日志是**点**，链路是**线**。
一次 `/assist` 内部有检索、重排、LLM 生成三段串行；不知道分段耗时，
优化就只能靠猜（而且经常猜错：以为模型慢，其实在检索上）。

面板上的 trace_id 之前只能复制，等于把一个「可以查」的东西做成了「需要另开终端查」。

## 2. 判断

只需要**一个请求内部各阶段的起止时间**，不需要分布式追踪那一整套。

所以刻意不做的事：

| 不做 | 理由 |
|---|---|
| 引 OpenTelemetry | 要 collector、exporter、上下文传播规范。单实例一个人维护，付不起这个运维成本 |
| 父子 span 树 | 当前最深 3 层，平铺 + start_ms 就能看出嵌套（子区间必落在父区间内）。树要维护 parent 引用，当前收益不明显 |
| 采样 | 全量记，一条链路约 2KB，100 条也就 200KB |
| 跨进程 | 只有两个进程（nginx/api），nginx 那一段用 `$request_time` 看就够了 |

**但必须有的**：一个覆盖整个请求的根 span。
实测发现业务 span 的起点通常在 3~5ms 之后（ASGI 解析 + Pydantic 校验都在它前面），
没有根 span 做基准，瀑布图上业务阶段会全挤在右侧一小条，看着像画错了——
而这个「3.8ms 花在框架、0.2ms 花在业务」恰恰是**最该被看见**的事实。

## 3. 实现

### 3.1 收集（`app/tracing.py`）

```python
_spans: ContextVar[Optional[list]] = ContextVar("spans", default=None)

@contextmanager
def span(name, **attrs):
    ...
    yield record          # 块内可以补 attrs（比如最终 attempt 次数）
    record["duration_ms"] = ...
    _spans.get().append(record)   # 请求结束由中间件取走
```

用 contextvar 而不是参数透传，理由和 T2-1 一样：调用链是
`路由 → 分类器 → LLM 客户端 → httpx`，逐层加参数会漏。

`yield` 出来的是可变 dict，是为了**让打点信息在真实结果出来之后再填**：
`llm.try` 一开始不知道这次会不会失败，失败时在 except 里写 `attrs["result"]="fail"`。

### 3.2 埋点位置

| span | 位置 | 记录的属性 |
|---|---|---|
| `http`（根） | 中间件 finally | method / path / status |
| `classify` | `main.py` 的 `/classify` | budget_ms / source / degraded |
| `assist` | `main.py` 的 `/assist` | budget_ms / mode |
| `rag.retrieve` | `rag_assistant.assist` | top_k / mode / hits |
| `rag.embed` 或 `rag.keyword` | `_retrieve` 内部分支 | 向量维度 / 候选数 |
| `rag.rerank` | `_llm_rerank` | candidates / ordered |
| `rag.answer` | `assist` 的 LLM 总结 | provider |
| `llm.try` | `_call_with_retry` 每次尝试 | provider / attempt / json_mode / timeout_ms / result |

`llm.try` 是这条链上最有价值的 span：**容灾链上重试了几次、每次落在哪个 provider、
每次拿到多少预算**，只看总耗时完全看不出来。

### 3.3 存储与接口

`TraceStore` = `OrderedDict` + maxlen 100（需要「先进先出淘汰」，dict 虽保序但没有
`popitem(last=False)`）。中间件在 finally 里写入，**探针请求不存**（没有 span，
存进来只会把 100 条窗口占满）。

```
GET /traces/{trace_id}
→ {trace_id, ts, method, path, status, duration_ms, usage, spans: [...]}
→ 404：链路已滚出窗口
```

### 3.4 前端：瀑布条

条的**左边缘 = 开始时刻，宽度 = 持续时间**，横轴基准是请求总耗时。
子阶段的条会自然落在父阶段区间内，靠位置就能看出嵌套，不需要画树。
缩进深度用点号层级推（`assist` 0 层，`rag.retrieve` 1 层）。

另外支持 `?trace=xxx` 深链直接打开：
值班时把链接发给同事比「你去点那个 trace_id」靠谱——流水 5 秒就刷新，手速跟不上。

## 4. 实测（本地未配 LLM）

```
http             start=   0.0 dur=   2.1 ok
assist           start=   0.8 dur=   1.2 ok
rag.retrieve     start=   1.4 dur=   0.6 ok
rag.keyword      start=   1.5 dur=   0.4 ok
total 2.1ms
```

这段数据本身就是结论：**框架层 0.8ms + 业务 1.2ms**，而业务里检索只占 0.6ms。
配了 LLM 之后会多出 `llm.try` / `rag.answer`，那时才能看出「模型占了多少」。

## 5. 实测暴露的问题

### 5.1 外层 `try` 与内层 `with` 嵌套错误

把 `return await self._call(...)` 包进 `with span(...)` 时，原来的
`try:` 被留在了外面，而 `except` 跟着 `with` 进了内层 → 外层 `try` 没有 except，
`ast.parse` 报 `SyntaxError: expected 'except' or 'finally' block`。

值得记的是**发现方式**：这条错误不是逻辑 bug，是语法错误，
但只有在改完立刻跑 `ast.parse` 时才第一时间暴露；如果直接重启服务，
报错信息会埋在 uvicorn 的一大段 traceback 里。

### 5.2 没有根 span 时瀑布图会骗人

第一版只收集业务 span，基准取请求总耗时 → 四条业务 span 全挤在右侧 88% 之后，
看起来像渲染 bug。加上 `http` 根 span 之后，同一份数据变成了
「3.8ms 空白 + 0.2ms 业务」，一眼能看出时间不在业务上。

### 5.3 亚毫秒 span 被格式化吃掉

关键词召回只要 0.1~0.2ms，用 `Math.round` 格式化会全显示成 `0ms`，
四条 span 一样长，看不出差别。用 `fmtDur` 保留一位小数（≥10ms 才取整）。

### 5.4 查链路本身会污染流水

`GET /traces/{id}` 是业务请求，默认会计入指标和最近流水。后果是：
点开 A 的链路 → 流水第一条变成「查 A 的那次请求」→ 看着像自己引用自己，
而且它 0.3ms 的耗时会去稀释延迟分位数。

修法：观测类请求统一排除在指标之外（`/health`、`/metrics`、`/traces/*`）。
这三个的共同点是**它们不产生业务价值，只服务于「看」这个动作**。

### 5.5 父子 span 起点相同时顺序随机

亚毫秒请求里，`assist`（父）和 `rag.retrieve`（子）的 `start_ms` 可能都是 0.3，
只按 `start_ms` 排序时两者顺序不稳定——瀑布图上会出现「子阶段包住父阶段」。
排序键改成 `(start_ms, name 的点号层级, -duration_ms)`：
同一时刻开始的，层级浅的（父）先出现。这个顺序必须确定，否则同一份数据刷新两次长得不一样。

## 6. 边界

- **只在内存**：重启归零，只保留最近 100 条。要跨天检索得上 SQLite / OTel（T7-2 的剩余部分）；
- **单进程**：多 worker 各存各的，查不到别的 worker 的链路；
- **无父子树**：`spans` 是平铺列表，嵌套关系靠时间区间推断，不是显式结构；
- **并发子任务会丢 span**：`asyncio.gather` 起的子任务里 `set` 不回传父上下文。
  当前代码全是顺序 `await`，没有触发；真加了并发分支要先改成显式传递；
- **没有跨服务**：nginx → api 这一段不在链路里。

## 7. 面试讲法

> 「trace_id 解决了『日志怎么串起来』，但回答不了『时间花在哪』。
> 所以我给每个请求加了阶段埋点：检索、重排、每次 LLM 尝试、证据门禁，
> 各记一个 span，前端点 trace_id 就能看到瀑布图。
>
> 有个细节：我一开始只收集业务 span，结果图上四条全挤在右边——因为请求前 3.8ms
> 花在 ASGI 解析和 Pydantic 校验上，业务只占 0.2ms。补了一个覆盖全请求的根 span
> 之后，这个『时间不在业务上』的事实反而成了最有用的一眼。
>
> 没有引 OTel：单实例一个人维护，collector + exporter 的运维成本换不来收益。
> 但埋点位置是照着 OTel 的 span 语义放的，以后要换实现，业务代码不用动。」

## 8. 与 T7-2 完整版的差距

| 项 | 当前 | T7-2 完整版 |
|---|---|---|
| 存储 | 进程内 `OrderedDict`，100 条 | SQLite 两张表（traces / spans），可跨重启 |
| 查询 | 按 trace_id 单条 | 列表 + 按时间/耗时/降级筛选 |
| 生命周期 | 重启归零 | 按天归档，可回放 |
| 跨进程 | 无 | 与 nginx 的 `$request_time` 对齐 |

---

## 补记（2026-11-12，真 Provider 下的两个显示问题）

### 1. `rag.answer` 和它的子 span `llm.try` 会上下颠倒

原来的排序键是 `(start_ms, 名字里点号的个数, -duration)`，用点号数当层级。
但 `llm.try` 是嵌在 `rag.answer` 里的，两者点号数**一样**、起点也几乎一样，
谁在前面只能看运气。曾经真的出现过：

```
rag.answer   3237.2ms
llm.try      3237.2ms    ← 子 span 跑到了父 span 上面
```

修法：层级不能从名字猜。`span()` 里用 contextvar 记「当前栈顶」，
每个 span 落库时带上真实的 `depth` 和 `parent`：

```python
parent = _current_span.get()
record["depth"]  = parent["depth"] + 1 if parent else 0
record["parent"] = parent["name"] if parent else None
```

排序键变成 `(start_ms, depth, -duration)`，前端缩进也用 `s.depth`。

### 2. 根 span 是手工构造的，漏了新字段

`http` 根 span 在中间件出口手工拼 dict（没法用 `with` 包整个请求），
加 `depth` 时只改了 `span()`，于是前端 `s.depth ?? …` 拿到了 `undefined`。
现在两处字段对齐，并在代码里注明「手工构造的 span 必须与 span() 的字段保持一致」。

> 这类「同一结构有两处构造」是最容易漏的地方：
> 要么收口成一个构造函数，要么在注释里写明必须同步。
