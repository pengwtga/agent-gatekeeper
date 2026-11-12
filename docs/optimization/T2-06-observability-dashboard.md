# T2-6 可观测面板（把指标从日志搬进页面）

## 1. 问题

T2-1~T2-4 做完之后，数据是全的，但拿不到：

- trace_id 只在响应头和 JSON 日志里，要用 `docker logs api | jq` 才看得见；
- 五个指标只有 `GET /metrics` 返回的一坨 JSON，没有趋势，只有一个当前值；
- token 归因只在每条日志的 `tokens` 字段里，想知道「这小时花了多少」得自己加。

结果是**只有写代码的人能用**。值班同学看到「分类变慢了」，第一步还是来问我。
面试演示时也只能截一屏终端输出——「有可观测性」和「看得出可观测性」是两件事。

## 2. 判断

面板的价值不是好看，是**把「发现异常 → 定位到具体请求」的两步压到同一个屏幕上**。

所以它有且只有两种时间尺度，缺一不可：

| 尺度 | 看什么 | 用途 |
|---|---|---|
| 聚合（分钟级趋势） | 请求量、延迟、错误率、降级率的曲线 | 发现「什么时候开始变坏」 |
| 流水（最近 50 条请求） | 单条请求的 trace_id、状态、耗时、token | 拿到一个 id 去日志里还原链路 |

只有聚合没有流水 → 知道出事了，不知道是哪一条。
只有流水没有聚合 → 像看 tail -f，看不出趋势。

## 3. 方案取舍

### 3.1 图表：手写 SVG，不引 echarts/recharts

只需要「折线 + 面积 + 三根网格线 + 轴标签」。echarts 打包后 gzip 仍有 100KB+，
而首屏为它多等一秒，换来的交互（缩放、框选）这里一个都用不上。

`frontend/src/components/TrendChart.jsx` 约 100 行，含：
自动 Y 轴上界（取整到 1/2/5/10 的整数倍）、60 点画 4 个 X 刻度、全零时显示空状态而不是画一条贴底的假线。

**什么时候该换**：要缩放/框选/十几个序列叠加时，手写会失控，那时候换库是对的。
现在的取舍写在文件头注释里，不靠记忆。

### 3.2 刷新：5 秒轮询，不上 SSE

- 5s 对「人盯面板」足够，异常从发生到被看见的延迟上限就是 5s；
- SSE 要在 nginx 里同时打开 `proxy_buffering off` 和 `add_header X-Accel-Buffering no`，
  还要处理断线重连——为省 4 秒的延迟付这些复杂度不值。

**但轮询频率有上限**：nginx 对该路径限流 20 次/分钟，面板自己 1s 一次就会把配额吃满，
然后它显示的 429 是自己在打自己。所以选项只给 5s / 15s / 暂停，5s 是下限。

### 3.3 后端存什么：分钟桶只存可累加量

`app/metrics.py` 新增 `_series`（60 个分钟桶）+ `_recent`（`deque(maxlen=50)`）。

分钟桶里存 `count / client_error / server_error / excluded / degrade / lat_sum / lat_max / tokens`，
**不存每分钟的延迟样本列表**。原因：样本数随流量线性涨（60 桶 × N 个 float），
而它的唯一用途是算历史分位数；分位数的当前值由滑动窗口给（`snapshot().latency_ms`）。

这是明确的降级：**看不到「14:03 那一分钟的 P95」**，只能看那一分钟的平均值和最大值。
真需要分钟级分位数时的正解是直方图桶（固定内存、可合并），不是存样本。

### 3.4 时间轴必须补零

没有请求的分钟不会建桶。如果直接返回「有什么给什么」，前端 X 轴会被压缩——
图上看着一直有流量，实际是 14:03 之后停了 20 分钟。`_series_out()` 按 `[now-59, now]`
逐分钟补齐，缺失分钟填 0。

### 3.5 分母口径：探针不算业务

`/health` 每 30s 一次、`/metrics` 每 5s 一次，两者永远 200。混进分母的后果：

- 错误率被稀释（10 笔业务全失败 + 100 次探针成功 = 9%，看着还行）；
- 降级率同理。

所以错误率、降级率的分母统一为 `total - excluded_from_latency`。
**关键是两处必须同一个口径**：顶层的 `degrade.rate` 和折线图的 `degrade_rate` 不一致时，
看的人第一反应是「数据错了」，而不是「口径不同」。这是实测发现的问题，见 §5.2。

## 4. 改动

| 文件 | 改了什么 |
|---|---|
| `app/metrics.py` | 新增 `_bucket()` / `_series_out()`；`record_request` 收 `trace_id`、`tokens`；`record_degrade`、`record_tokens` 同步写分钟桶；`snapshot()` 增加 `series`、`recent_requests`；`reset()` 清理新结构 |
| `app/obs.py` | `usage` 提前到 `record_request` 之前取，让流水行带上本次请求的 token |
| `frontend/src/components/TrendChart.jsx` | 新增，零依赖 SVG 折线图 |
| `frontend/src/pages/Observability.jsx` | 新增，6 个 KPI + 4 张趋势图 + 3 组分布 + 请求流水表 |
| `frontend/src/App.jsx` / `components/Header.jsx` | 新增 `/observability` 路由与导航 |
| `frontend/src/api.js` | `fetchMetrics()` |
| `frontend/src/styles.css` | 面板样式；`.wrap.wide` 放宽到 1180px（其它页面不动） |
| `frontend/vite.config.js` | 代理目标支持 `SSRM_API_TARGET` 覆盖 |

后端**没有新增端点**：`/metrics` 的返回体直接变宽。理由是只有一个消费方，
多一个端点就多一份要维护的契约；等抓取端（Prometheus）真接进来时再拆分。

## 5. 实测暴露的问题

### 5.1 `const [interval, setIntervalSec] = useState(5)` 把 `setInterval` 遮蔽了

局部变量 `interval` 覆盖了全局 `setInterval`，`setInterval(pull, ...)` 直接 TypeError，
页面白屏。改名 `refreshSec` 解决。这类错误 `vite build` 不报（语法合法），只有打开页面才炸。

### 5.2 错误率两处口径不一致

顶层 KPI 用 `(4xx+5xx)/total`（含探针），分钟桶用 `errors/count`（也含探针），
但降级率两处都用业务请求数。屏幕上出现「错误率 9.09%、降级率 66.7%，
而失败 2 笔 / 总 22 笔、降级 8 笔 / 12 笔」——四个数字互相矛盾。

修法：三处全部改为业务请求做分母，KPI 的副标题也直接写「2 笔失败 / 14 笔业务请求」，
让口径在屏幕上是自证的。

### 5.3 亚毫秒延迟的 Y 轴标签重复

`niceMax(0.4) = 0.5`，三个刻度 0.5/0.25/0 经 `Math.round` 后变成 "1ms / 0ms / 0ms"。
轴刻度单独用 `axisMs()` 保留一位小数；表格里仍用整数（那里 0.4ms 显示成 0ms 是对的，
说明它快到不值得看）。

### 5.4 CSS `auto-fit` 把 2×2 算成了 3+1

`repeat(auto-fit, minmax(320px, 1fr))` 在 1180px 容器里能放下 3 列，
四个图表变成「上 3 下 1」，最后一张孤零零挂在左边，看着像渲染坏了。
改成 `repeat(2, minmax(0, 1fr))` + 860px 断点降为单列。

### 5.5 8001 被 9 天前的容器占着，代理打到了旧服务

本机 `ssrm-aiops-web` 容器一直映射 `0.0.0.0:8001`。本地起 uvicorn 时，
`/api/metrics` 实际打到了旧容器的 SPA fallback，返回的是 `index.html`，
前端 `res.json()` 解析失败 → 页面一直「加载中」。

这是**环境问题伪装成代码问题**的典型。处理：`vite.config.js` 支持 `SSRM_API_TARGET`，
本地验证用 `8010`：

```bash
PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
SSRM_API_TARGET=http://127.0.0.1:8010 npx vite --port 5175
```

## 6. 验收

接口：

```bash
curl -s localhost:8010/metrics | jq '{points: (.series.points|length), recent: (.recent_requests|length)}'
# {"points": 60, "recent": 12}
```

- `series.points` 恒为 60（含补零）；
- `recent_requests` 不含 `/health`、`/metrics`（探针不进流水）；
- 顶层 `degrade.rate` 与 `series.points[-1].degrade_rate` 同口径（同一分钟内应相等）。

页面（1440×1400 实测）：

- 6 个 KPI 一行排开，P95 高于 4s 转橙、高于 8s 转红；
- 4 张趋势图 2×2，无数据时显示「近 1 小时没有错误请求」这类空状态，而不是画假线；
- 请求表的状态列 2xx 绿 / 4xx 橙 / 5xx 红，trace_id 可点击复制；
- 切换 15s / 暂停后不再发请求（Network 面板确认）。

## 7. 边界

诚实写清楚，这些在面试里主动说比被问出来好：

- **进程内**：多 worker / 多实例各算各的，面板只能看单进程；聚合要 Prometheus + 远端存储；
- **重启归零**：分钟桶和流水都在内存里，发一次版就清零（曲线会突然断掉）；
- **无持久化**：60 分钟以前的趋势不存在，不能回答「上周 P95 多少」；
- **无告警**：阈值只用来上色，没有主动通知。告警需要「持续 N 分钟」的判定窗口，属于 T7；
- **流水里没有降级原因**：`degrade` 只到聚合层，单条请求为什么降级要回日志里看。

## 8. 面试讲法

> 「指标做完了但如果只有 `/metrics` 一个 JSON，等于只有我能用。所以我加了一个面板：
> 上面是分钟级趋势，回答『什么时候开始变坏』；下面是最近 50 条请求流水，
> 每条带 trace_id，复制就能去日志里 grep 出整条链路。两种时间尺度在同一个屏幕上，
> 从『感觉变慢了』到『就是这一条请求』不用切工具。
>
> 有个细节我特意对齐过：错误率和降级率的分母都扣掉 `/health` 这类探针。
> 不扣的话 10 笔业务全失败、100 次健康检查成功，错误率显示 9%，看着像没事。
> 而且顶层数字和折线图必须同口径，不然屏幕上的数自己打架，看的人第一反应是数据错了。」

## 9. 没做（以及为什么）

| 想法 | 为什么不做 |
|---|---|
| 引入 Prometheus + Grafana | 单实例、一个人维护，多一个 exporter 就多一个要盯的东西；它解决的问题（多实例聚合、长期存储）当前不存在 |
| WebSocket 实时推送 | 5s 轮询已满足「人盯」的精度，推送要多一套连接管理 |
| 面板上加「一键评测」按钮 | `/eval` 是同步长任务（30 条串行约 100s），必然撞 nginx 20s 超时，要先改成异步任务（T2-6 待定） |
| 按 path 拆分的延迟分位 | 当前只有一个业务路径，拆了也没有对比意义 |
