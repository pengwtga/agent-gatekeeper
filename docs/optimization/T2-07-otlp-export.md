# T2-07 · OTel 接入：从「自研 span」到「标准协议出口」

## 0. 先说结论

T2-05 让 trace 能跨重启回查，但它仍然只服务**自己**：数据在自己的 SQLite 里，格式是自己的（`name/start_ms/duration_ms/depth/parent/attrs`），只有自己的前端能读。

OTel 要解决的不是「能不能看」，而是这两个问题：

1. **能不能把链路送给别人？**（客户已有 Jaeger/Tempo/签表，不想再装一套 UI）
2. **能不能接别人的链路？**（业务 Agent 是别的团队写的，用 Python/Java/Go，他们的 trace_id 怎么和我的对上？）

本任务做两件事，都是**真标准**、不是仿制：

| 做什么 | 标准 | 依赖 |
|---|---|---|
| 入站 trace_id 对齐 + 出站响应头回写 | W3C Trace Context（`traceparent`） | 零依赖，标准库即可 |
| 把 trace 导出到 Jaeger/Tempo/Collector | OTLP/HTTP + JSON 编码 | httpx（已有） |

**不引入 `opentelemetry-sdk`**，理由见 §3。

## 1. OTel 到底是什么（先把这个说清）

很多人把 OTel 当成「一个埋点库」。它其实是三层东西，价值依次递减：

| 层 | 内容 | 换掉它的代价 |
|---|---|---|
| **数据模型** | Trace = 一棵 Span 树；Span 有 `traceId/spanId/parentSpanId/name/kind/start-end/attributes/status`；Resource 描述「谁产生的」（`service.name` 等） | 高：所有后端都按这个模型存 |
| **传播协议** | W3C `traceparent: 00-<32hex traceId>-<16hex spanId>-<flags>`，跨进程/跨语言传上下文 | 高：这是「一条链跨两个服务」的唯一办法 |
| **语义约定** | 属性怎么命名：HTTP 用 `http.request.method`，LLM 用 `gen_ai.system` / `gen_ai.usage.input_tokens` | 中：改命名不动逻辑，但不遵守就查不出别人写的查询 |

SDK 只是「按上面三层生成数据」的一个实现。**只要我输出的数据符合这三层，我就是 OTel 兼容的**，用不用 SDK 是工程选择，不是合规性问题。

这也回答了一个常见追问：「你没用 SDK 怎么叫接了 OTel？」——因为对接点是**协议**，不是库。客户侧只要一个 OTel Collector（或 Jaeger 的 OTLP 端口），我的数据就能进去。

## 2. 目标与非目标

**目标**

1. 入站支持 W3C `traceparent`：上游带了这个头，就用它的 traceId，让「网关 → 我」是一条链而不是两条；
2. 响应头回写 `traceparent`，下游（或前端 RUM）能继续串；
3. 把已有的 trace 转成 OTLP/JSON 批量推到 `POST {endpoint}/v1/traces`；
4. 属性命名向 `gen_ai.*` 语义约定靠拢，同时保留原始属性（不丢信息）；
5. 全程不影响主请求：导出失败只记日志，队列满就丢并计数。

**非目标**

- 不做自动埋点（不引 `opentelemetry-instrumentation-*`，理由见 §3.2）；
- 不做 Metric / Log 的 OTLP 导出（本任务只做 Trace；指标已有 `/metrics`，日志走 stdout 由采集器收）；
- 不做 tail-based sampling（当前 QPS 个位数，全留；只做头部确定性采样，见 §5.3）；
- 不替换自研的 SQLite 与前端。**双写：SQLite 给自己看，OTLP 给别人看。**

## 3. 方案选择

### 3.1 三条接入路线

| 路线 | 做法 | 本项目的结论 |
|---|---|---|
| **A. 引 SDK + 自动埋点** | `opentelemetry-sdk` + `instrumentation-fastapi` + `instrumentation-httpx`，`span()` 换成 OTel API | ❌ 环境装不上（见 3.2），且会牵动所有调用点 |
| **B. 自研数据 → OTLP 导出** | 保留现有 `span()`，只在出口把扁平 span 列表转成 OTLP | ✅ 本次采用 |
| **C. 走 sidecar collector 转发** | 应用只推 OTLP 到本地 collector，由 collector 决定后端 | ✅ 兼容：我的 `endpoint` 填 collector 即可，代码不用改 |

B 与 C 不冲突：B 是「谁生成数据」，C 是「数据去哪」。生产上通常是 B + C（应用推 collector，collector 扇出到 Jaeger + 归档）。

### 3.2 为什么这次不引 SDK

**直接原因**：`pip install opentelemetry-sdk` 在当前环境被安全策略拦下（safe-delete 拒绝覆盖已装包）。这是环境限制，不是判断。

**但判断本身也支持不引**：

| 考量 | 说明 |
|---|---|
| 依赖重量 | `opentelemetry-sdk` + `exporter-otlp` + 两个 instrumentation ≈ 8 个新包，其中 `wrapt`、`asgiref` 会和现有版本争抢；本项目 `requirements.txt` 只有 6 个包，加一倍 |
| 收益重叠 | SDK 的核心产出（span 树 + 属性 + 导出）我已经有了；引入它得到的主要是「自动埋点」（httpx/SQLAlchemy 的库级 span）——而我的链路里**只有 1 个外部调用（LLM）且已经手工包了 `llm.try`**，自动埋点几乎没有增量 |
| 控制权 | 自研版能精确控制「哪些 span 不收」「attrs 怎么截断」「失败怎么降级」；SDK 这些都要通过 sampler/processor 配，配置成本高于代码量 |
| 可逆 | 一旦将来要换 SDK，因为对外已经是 OTLP，**换的是内部实现，出口不变**——这正是分层的好处 |

**诚实的边界**：这是「协议兼容」，不是「原生 SDK 集成」。原生集成能拿到的东西（库级自动埋点、Baggage、Span Links、Metrics 关联）我没有。面试时应当这样表述，而不是说「我接了 OTel SDK」。

## 4. 设计

### 4.1 trace_id 映射：16 hex ↔ 32 hex

自研 trace_id 是 `uuid4().hex[:16]`（16 个 hex 字符），OTel 要求 32 个。映射规则必须**双向且稳定**，否则「查同一条链路」会对不上。

```
本地 → OTel：tid32 = tid16.rjust(32, "0")        # 左侧补零
OTel → 本地：tid16 = tid32.lstrip("0") or tid32[-16:]   # 去前导零；全零则取后 16 位
```

为什么左补零而不是拼两遍：`tid16 * 2` 会让 32 位里出现重复模式，某些后端的去重/哈希会撞；补零是单射，且 `lstrip("0")` 可逆。

**校验**：OTel 规定 traceId 不能全零。`parse_traceparent` 会拒掉全零与长度不符的输入。

### 4.2 span 层级：补 `span_id` / `parent_span_id`

现在 `tracing.span()` 产出的 record 里，父子关系只记了 `parent: "rag.answer"`（**名字**）。OTLP 要的是 `parentSpanId`（**ID**）。名字会重名：一次请求里两个 `llm.try` 的父都是 `rag.answer`，用名字无法唯一定位。

所以给每个 span 加两个字段（纯增量，前端不用改）：

```python
"span_id": uuid4().hex[:16],
"parent_span_id": parent["span_id"] if parent else None,
```

**根 span 的处理**：中间件里的 `http` 根 span 是手工构造的，而业务 span 的 `_current_span` 起点是 `None`（`begin_spans()` 里设的），所以业务 span 的 `parent_span_id` 全是空——它们和根 span 是**兄弟**而不是**父子**。

导出时修正：`build_payload` 把「无 parent 且不是根 span 本身」的 span，挂到根 span 的 `spanId` 下。理由写在函数注释里——**OTLP 里一条 trace 应当是一棵树**，扁平兄弟关系会让 Jaeger 画出 5 条并列的根链，看着像 5 次独立请求。

（根因是自研 `begin_spans()` 没有预置根 span。改成「入口就建根 span」能根治，但那要动中间件与前端排序逻辑，收益只是少一次导出侧修正——先记在这里，不动。）

### 4.3 属性映射：`gen_ai.*` 语义约定

原始属性一律保留（前缀不变），另对 LLM 相关 span 追加标准别名：

| 自研属性 | OTel 语义约定 |
|---|---|
| `llm.try` 的 `provider` | `gen_ai.system`（如 `zhipu`） |
| `llm.try` 的 `model` | `gen_ai.request.model` |
| span 名 `llm.try` / `rag.answer` | `gen_ai.operation.name = "chat"` |
| `prompt_tokens` / `completion_tokens` | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` |
| `http` 根 span 的 `path` / `method` | `http.request.method` / `url.path` |

为什么两边都留：语义约定让别人的看板能直接查（`gen_ai.usage.input_tokens` 是通用词），原始属性让我自己的前端和历史数据不受影响。**加别名比改名字安全**——改名字等于让两个月前的 SQL 全部失效。

**值类型**：OTLP/JSON 的属性值是 `{"stringValue": ...}` 这样的 tagged union，64 位整数要写成**字符串**（JSON number 精度不够）。`dict`/`list` 序列化成一个 JSON 字符串，不展开——OTLP 的 array 只支持同构标量，嵌套对象展开容易出错。

### 4.4 导出：内存队列 + 后台批量

```
中间件 finally ──enqueue(record)──> deque(maxlen=2000)
                                      │
                      后台 task（每 5s 或攒够 32 条）──> POST /v1/traces
                                      │
                              失败 → 丢弃 + 计数 + 记日志
```

四个刻意的选择：

1. **`deque(maxlen=...)` 而不是无界队列**：collector 挂了的时候，无界队列会把内存吃干。满了就丢最老的，并在下一个日志行里报 `dropped` 数量——**观测系统自己挂掉不能拖死业务**。
2. **后台 task 而不是中间件里 await**：导出是网络 IO，哪怕 3s 超时也会把请求拖到 3s。中间件只做 `enqueue`（纯内存操作，微秒级）。
3. **批量而不是逐条**：一条 trace 约 4~10 个 span，逐条发就是逐条建 TCP；32 条一批能把请求数压到 1/32。
4. **失败不重试**：trace 数据的价值随时间衰减，5 分钟前的链路重传意义有限；重试还会在 collector 挂掉时形成堆积。宁可丢，不可堵。**这是与「业务请求必须重试」相反的策略**，因为两者的可靠性要求不同。

### 4.5 采样：按 trace_id 哈希，不按请求随机

`otel_sample_ratio < 1` 时，用 `int(trace_id[:8], 16) / 0xFFFFFFFF < ratio` 判定。

必须按 trace_id 而不是每次随机，因为**一条 trace 的所有 span 必须同时被采或同时被丢**——否则 Jaeger 里会出现「有子 span 没有父 span」的碎链，比不采还糟。用 trace_id 哈希天然保证这个性质（span 导出时按 trace 分组，判定一次）。

## 5. 验收标准

1. `POST /classify` 带 `traceparent: 00-<32hex>-<16hex>-01` → 响应头 `x-trace-id` 等于该 traceId 去掉前导零的 16 位；
2. 不带或带非法 `traceparent`（长度错 / 非 hex / 全零）→ 正常新建，不报错；
3. 响应头含合法 `traceparent`，可被下游继续传播；
4. `otel_enabled=true` 且本地起 collector 时，Jaeger 里能看到一次 `/assist` 的完整树（根 `http` → `rag.retrieve/rerank/answer` → `llm.try`）；
5. collector 不在线时：请求延迟不变（p95 差值 < 5ms），日志出现 `otlp_export_fail`，队列丢弃计数递增；
6. `build_payload` 纯函数单测覆盖：traceId 补零、parentSpanId 挂根、`gen_ai.*` 别名、intValue 为字符串、status 映射；
7. `pytest` 全绿，`ruff` 通过。

### 5.1 实测记录（2026-11-16）

本地起一个假 collector（`http.server` 收 `POST /v1/traces` 并打印 span 摘要），带
`SSRM_AIOPS_OTEL_ENABLED=true` 启动服务，发一条带 `traceparent` 的 `/classify`
（该请求本身走了 LLM 降级：zhipu 两次超时 → 规则基线，正好是一条真实故障链）：

```
请求头  traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
响应头  x-trace-id:  a3ce929d0e0e4736
        traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-243009ebb96d4b10-01
```

sink 收到 4 个 span：

| span | spanId | parentSpanId | kind | 耗时 |
|---|---|---|---|---|
| `http` | `243009ebb96d4b10` | —（根） | 2 = SERVER | 12227.9ms |
| `classify` | `572fb09897734f4f` | `243009ebb96d4b10` | 1 = INTERNAL | 12223.4ms |
| `llm.try` | `c0420ba867064b3a` | `572fb09897734f4f` | 1 | 7209.2ms |
| `llm.try` | `1eb6fdd72bc144bd` | `572fb09897734f4f` | 1 | 5013.3ms |

四个要确认的点都成立：

- **traceId 与上游一致**（32 位没有被截断成 16 位）——这是 §4.1 那条映射规则在起作用；
- **`classify` 的 parent 等于根 span 的 spanId**，也等于响应头 `traceparent` 里的 spanId，说明「响应头、导出、本地存储」三处引用的是同一个根；
- **两个 `llm.try` 各自独立、平行挂在 `classify` 下**，没有因为父 span 重名而串成一条；
- `gen_ai.system=zhipu`、`gen_ai.operation.name=chat` 都在，原始 `provider` 属性也保留。

`GET /otel/stats` 返回 `sent_spans=4 / failed_batches=0 / dropped_traces=0`。
同一条 trace 在本地 SQLite 里的 `span_id` 与导出值**完全一致**——双写同源，不是两套数据。

反向用例（collector 不在线）由单测覆盖：`flush` 返回 0、`failed_batches` 递增、不抛异常、请求仍 200。

## 6. 后续演进

| 事项 | 何时做 |
|---|---|
| 换成 `opentelemetry-sdk` | 需要「库级自动埋点」（httpx/SQLAlchemy/Redis 的 span）或多语言混布时 |
| Metrics 也走 OTLP | 指标要从「自己看」变成「进统一看板」时；现在 `/metrics` 够用 |
| 接 Baggage / Span Links | 多 Agent 协作、异步任务（回调）场景才需要 |
| tail-based sampling | QPS 上百、全留成本明显时；头部采样会漏掉「恰好慢的那条」 |

## 7. 面试口径

> OTel 我接的是**协议层**不是 SDK：入站解析 W3C `traceparent` 让上游 traceId 能延续，出站按 OTLP/HTTP JSON 把自研 span 树推给 collector，属性向 `gen_ai.*` 语义约定对齐。没引 SDK 有两个原因——环境装不上，以及我的链路里只有一个外部调用、自动埋点几乎没有增量；但出口是标准协议，所以将来换 SDK 时**换的是实现，出口不变**。
>
> 一个具体的坑：自研 span 只记了父 span 的**名字**，两个 `llm.try` 的父都叫 `rag.answer`，导出时无法唯一定位，所以我加了 `span_id` / `parent_span_id`。这类「自研 schema 看着够用，一到标准协议就暴露」的地方，正是接 OTel 的真正价值——它逼你把隐式约定显式化。
