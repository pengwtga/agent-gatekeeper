# T2-05 · Trace 落库：从「内存 100 条」到「可按时间检索」

## 1. 问题

T2-01 给了 trace_id，T2-02 让日志能按 trace_id 串起来，T2-03 有了 span 瀑布。但三者共享同一个边界（写在 `tracing.py` 的注释里）：

- 进程内内存，重启归零；
- 只留最近 100 条，滚出去就查不到；
- 多 worker 各算各的。

后果很具体：**值班时想回看「昨天下午那批超时」的链路，只能翻日志，瀑布图已经没了**。而这正是「诊断平台」与「单机调试工具」的分界线——前者要求证据可回查，后者只需要当下可看。

## 2. 目标与非目标

**目标**
1. 业务请求的 trace（含全部 span）落盘，进程重启后仍可按 `trace_id` 点查；
2. 支持按时间范围 + 路径 + 时长/状态过滤的列表查询（回答「最近一小时哪些请求慢」）；
3. 前端零改动：`GET /traces/{trace_id}` 返回结构与现在完全一致；
4. 落库失败绝不影响主请求（观测是旁路，不是主链路）。

**非目标（明确不做，避免过度设计）**
- 不做跨进程/跨服务追踪（那是 OTel 的事，见 §9）；
- 不做采样（当前 QPS 个位数，全留；尾采样是上量后的事）；
- 不做多实例共享（单实例 + SQLite，换 Postgres 时接口不变）；
- 不做 span 级别的全文检索（attrs 是 JSON，不建 FTS）。

## 3. 方案选择

### 3.1 存储：为什么还是 SQLite

| 候选 | 结论 |
|---|---|
| **SQLite（WAL）** | ✅ 单实例零运维，与 `escalation.py` 同一套做法；点查 + 时间范围扫描都够；`sqlite3` 是标准库，不加依赖 |
| JSONL 追加文件 | 写入最简单，但「按 path / 时长过滤」「按时间倒序取 N 条」都要全文件扫，且删除过期数据要重写文件 |
| ClickHouse / ES | 查询能力强，但单机部署要多一个服务、多一份要盯的东西；当前数据量（约 0.8MB/天）用不上 |
| Postgres | 多实例时的正确答案，现在引入属于提前优化 |

**判断标准**：先证明查询模式（按 trace_id 点查、按时间范围扫、按 path/状态过滤），再选引擎。这三种模式 SQLite 加对索引都能满足。

### 3.2 表结构：两张表，不是一个大 JSON

| 候选 | 结论 |
|---|---|
| **traces + spans 两表** | ✅ 支持 span 级聚合：「`llm.try` 的失败率」「按 span 名的 P95」，这是诊断平台最常用的查询；也支持按 trace 过滤后再取 span |
| 单表 JSON blob | 写入最简单，但任何 span 级统计都要在应用层反序列化全部行——等于把数据库退化成文件 |

DDL：

```sql
CREATE TABLE IF NOT EXISTS traces (
    trace_id     TEXT PRIMARY KEY,
    ts_epoch     REAL NOT NULL,        -- Unix 秒，索引与保留期都用它
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       INTEGER NOT NULL,
    outcome      TEXT NOT NULL,        -- ok | client_error | server_error | error
    duration_ms  REAL NOT NULL,
    span_count   INTEGER NOT NULL,
    has_error    INTEGER NOT NULL,     -- 0/1，过滤「只看失败」不用 join
    usage_json   TEXT NOT NULL         -- prompt/completion/total/calls
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_traces_path_ts ON traces(path, ts_epoch DESC);

CREATE TABLE IF NOT EXISTS spans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,      -- 落库顺序 = 瀑布图顺序（已按 start_ms/depth 排过）
    name        TEXT NOT NULL,
    start_ms    REAL NOT NULL,
    duration_ms REAL NOT NULL,
    status      TEXT NOT NULL,
    depth       INTEGER NOT NULL,
    parent      TEXT,
    attrs_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name, status);
```

**为什么存 `ts_epoch` 而不存 `"18:07:14"` 字符串**：字符串只能按「当天内」排序，跨天就错；epoch 既能排序又能直接算保留期，展示时再格式化。内存里那个 `ts` 字段保留，是为了前端不改。

**为什么加 `span_count` / `has_error` 冗余列**：列表页每行要显示「几个 span、有没有错」，不冗余就得 join + count，一页 20 行要 20 次聚合。冗余是写时一次、读时 N 次的典型收益。

### 3.3 写路径：`asyncio.to_thread`，不是队列

| 候选 | 结论 |
|---|---|
| 直接在中间件里同步写 | ❌ SQLite 写 + commit 约 1~3ms，且持有 GIL 与连接锁，会拖慢事件循环（并发时排队） |
| **`await asyncio.to_thread(store.put, record)`** | ✅ 写入放到线程池，事件循环不被阻塞；实现简单；失败可捕获 |
| 后台线程 + 队列（fire-and-forget） | 延迟最低，但引入「进程崩溃时队列里未落盘的数据丢失」和「测试要 flush」的复杂度；QPS 个位数时收益为零 |

当前选 `to_thread`。切换条件是**写入 QPS 过百**：那时单条 commit 会成为瓶颈，应改成「攒批 + 定时 flush」（同一事务写 100 条，吞吐提升一个量级）。

**写入点**：`obs.py` 中间件的 `finally` 块，紧跟 `traces.put()`（内存热缓存）之后。两条规则保持一致：
- 只持久化 `counted=True` 且有 span 的请求（探针 `/health`、`/metrics`、看流量的 `/traces/*` 不落库，否则会把保留期占满）；
- 记录里手工补的根 span `http` 一起落库（否则瀑布图缺基准，业务 span 会全挤在右侧）。

**失败处理**：`try/except Exception` 包住，失败只打 WARNING 日志（`event=trace_persist_fail`）。观测是旁路——**落库挂了不能把 200 变成 500**，这是可观测系统的基本要求。

### 3.4 读路径：内存优先，DB 兜底

```
GET /traces/{id} → traces.get(id)   # 内存，命中即返回（热数据亚毫秒）
                 → trace_db.get(id) # 未命中查库（重启后、滚出 100 条后）
                 → 404
```

**为什么内存优先而不是 DB 优先**：内存里的记录与库里完全同构（同一份 dict 落库），命中即返回省一次反序列化；而「刚发生的那条」恰恰是值班最常查的。代价是两边不一致的可能——用「同一次写入同时更新两处」消除，不存在异步同步问题。

新增列表接口：

```
GET /traces?limit=20&path=/classify&min_duration_ms=5000&only_errors=true&since_minutes=60
```

返回精简行（不含 spans，避免一页传几百 KB），点进去再取详情。这是「列表-详情」的标准分层，也是前端 Requests 页以后可以直接复用的接口。

### 3.5 保留期与容量

- 默认保留 **7 天**（`trace_retention_days`）；
- 容量估算（实测校准）：4 span 的 trace 落库约 **0.8KB**，每天 1000 请求 ≈ 0.8MB，7 天 ≈ 5.6MB。SQLite 完全无压力（实测 300 条 = 236KB）；
- 清理顺序：先删 spans 再删 traces（未开外键级联，两步同事务，避免半清理状态）。

**「7 天」和「每 500 次写」不是两套过期策略，是判据与触发时机**：

| 参数 | 管什么 | 换掉它的后果 |
|---|---|---|
| `trace_retention_days = 7` | **删什么**：`ts_epoch < now - 7d` | 改它才是改保留时长 |
| `_PRUNE_EVERY = 500` | **什么时候检查**：每 500 次写入触发一次 DELETE | 只影响清理的及时性，不影响删哪些数据 |

为什么不每次写都清：DELETE 要扫 `ts_epoch` 索引并写 WAL，本来 0.11ms 的写入会变成几毫秒，等于把一个旁路操作做成了主路径开销。
为什么不能只在启动时清：进程跑几个月不重启，磁盘只增不减。

**代价（写在这里，不藏）**：最多会有 499 条过期数据滞留。按每天 1000 请求算，约 12 小时清一次，稳态下磁盘占用是「7 天 + 半天」，偏差 7%。需要更准时，把 `_PRUNE_EVERY` 调小，或用 cron 调 `TraceStore.prune()`——方法本来就是公开的。

## 4. 兼容性

| 面 | 影响 |
|---|---|
| `GET /traces/{id}` 返回结构 | **完全不变**（trace_id / ts / method / path / status / duration_ms / outcome / usage / spans[]），前端 `Observability/Requests.jsx` 零改动 |
| 内存窗口 | 保留（100 条热缓存），行为不变；落库只是多了一条旁路 |
| 指标 | 不变；落库不参与 metrics |
| 测试 | 44 个单测不受影响（中间件里的落库有 try/except，且测试不依赖 DB） |

## 5. 验收标准

1. 发一次 `/classify` → 重启服务 → `GET /traces/{trace_id}` 仍返回完整瀑布（这是本任务的核心验收）；
2. `GET /traces?limit=5` 返回按时间倒序的列表，`min_duration_ms` / `only_errors` 过滤生效；
3. 落库失败（如把 db 路径设为只读）时，请求仍返回 200，日志有 `trace_persist_fail`；
4. `pytest` 全绿，新增用例覆盖：写入-读取往返、span 顺序与 attrs、保留期清理、列表过滤；
5. 落库耗时不计入请求延迟：对比落库前后 p95 无显著变化（本地实测差值 < 5ms）。

### 5.1 验收记录（2026-11-16 实跑）

**核心验收：重启后仍可查**

```
POST /classify → trace_id=166d295e462f494f，总耗时 12107.8ms，4 个 span
（zhipu #1 7125.6ms 超时 + #2 4980.7ms 吃完剩余 → 规则基线兜底）
→ kill 进程 → 重新 uvicorn 启动（内存缓存为空）
→ GET /traces/166d295e462f494f → 200，瀑布与 attrs 完整（provider/attempt/timeout_ms/result 全在）
```

**写入开销（300 次连续写入，4 span/条）**

| 指标 | 数值 |
|---|---|
| avg | **0.11ms** |
| p50 / p95 / max | 0.10 / 0.15 / 0.40ms |
| 300 条 trace 落库体积 | 236KB（≈0.8KB/条） |

结论：**0.11ms 的量级即便同步写也不会影响请求**，用 `to_thread` 是为了给「记录变大（以后存 prompt 引用/更长 attrs）」留余量，同时避免并发时连接锁拖住事件循环。

**修正容量估算**：按实测 0.8KB/条（4 span）算，每天 1000 请求 ≈ 0.8MB，7 天 ≈ 5.6MB，比原估的 2KB/条更省。

**单测**：51 passed（原 44 + 新增 7：往返、缺失、幂等重写、列表过滤、保留期、不可序列化 attrs、**落库失败不影响 200**）。

## 6. 实现清单

| 文件 | 改动 |
|---|---|
| `app/observability/trace_store.py` | 新增：`TraceStore`（建表、put、get、list、prune）+ 懒加载单例 `get_trace_store()` |
| `app/observability/obs.py` | 中间件 `finally` 中 `await asyncio.to_thread(...)` 持久化，异常吞掉并记日志 |
| `app/main.py` | `/traces/{id}` 增加 DB 兜底；新增 `GET /traces` 列表；更新 docstring（不再说「只保留 100 条」） |
| `app/config.py` | 新增 `trace_db`、`trace_retention_days`、`trace_persist` |
| `tests/test_trace_store.py` | 新增用例（tmp_path 建库，不碰真实 data 目录） |
| `docs/` | 本文档 + `T2-03-metrics.md` / `tracing.py` 注释里的边界说明同步更新 |

## 7. 已知边界（写清楚，别藏着）

1. **单实例**：多实例时 SQLite 文件不共享，要换 Postgres；`TraceStore` 的方法签名不变，只换实现；
2. **无采样**：全量落库，靠保留期控制体积；上量后要补尾采样（错误/慢/高 token 全留，正常按比例）；
3. **无跨服务**：没有 `traceparent` 传播，前端/网关到后端这一段只有自生成的 trace_id；
4. **无脱敏**：span attrs 目前只有 provider、attempt、timeout_ms 这类元数据，不含 prompt 原文；一旦要存 prompt/response，必须先过脱敏与访问审计（见 Q8）；
5. **写入非事务性跨表**：traces 与 spans 分两条语句写，理论上存在「trace 写了、spans 没写完」的窗口（进程被杀）。读侧用 `span_count` 与实际 span 数比对可发现，暂不做修复——诊断场景下「缺 span」比「整条丢失」危害小。

## 8. 后续演进（对应面试里的「平台化」）

| 步骤 | 动作 | 触发条件 |
|---|---|---|
| 1 | 换 Postgres（JSONB 存 attrs） | 多实例部署 |
| 2 | span 表加 `tenant_id` / `session_id` | 接入第二个业务方 |
| 3 | 尾采样 + 冷热分层 | 日请求 > 10 万 |
| 4 | 接 OTel：`span()` 换实现 + OTLP exporter | 需要与公司现有观测体系打通 |

## 9. 与面试口径的对应

- **Q4（trace 存储边界）**：现在有答案了——「内存热缓存 + SQLite 冷存，点查走内存优先、DB 兜底；多实例换 PG」；
- **Q5（存储选型）**：本文档 §3.1 的判断标准（先证明查询模式再选引擎）可直接复用；
- **Q21（单实例到平台的差距）**：存储这一格已经从「缺口」变成「已补」，剩下的是多租户、权限、调度。
