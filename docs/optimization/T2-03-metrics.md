# T2-3 五个指标（进程内滑动窗口）

## 1. 问题

做 T2-1/T2-2 之后，能查「某一次请求发生了什么」，但答不了「整体在变好还是变坏」：

- 今天分类的 P95 是多少？比上周慢了吗？
- 降级率多少？降级是因为预算不够，还是 provider 挂了？
- 模型输出不守格式的频率在上升吗？
- 置信度分布是不是整体在下移（模型变保守/变差）？

这些问题靠翻日志能凑出来，但每次都要现写脚本 —— 等于没有监控。

## 2. 判断

**只需要五个指标就能覆盖 Agent 服务的健康度**，再多是自欺欺人：

| 指标 | 回答什么问题 | 出问题时的表现 |
|---|---|---|
| `requests`（total/ok/4xx/5xx） | 服务活着吗 | 5xx 上升 = 代码或依赖炸了 |
| `latency`（P50/P95/P99） | 慢不慢 | P95 从 4s 涨到 11s = 快撞预算了 |
| `degrade`（按原因） | 结果可信吗 | `budget_exhausted` 升 = 预算不够；`provider_error` 升 = 上游挂 |
| `json.fail`（按类型） | 模型守格式吗 | `truncated` 升 = max_tokens 不够；`no_json` 升 = prompt 退化 |
| `confidence_hist` | 模型自信吗 | 整体左移 = 模型开始含糊，即使准确率没立刻掉 |

**第三个指标是 Agent 特有的**，也是普通 Web 服务没有的：
HTTP 200 不代表结果可信，得单独盯降级。

**「按原因拆」比总量重要**：降级率 5% 本身不说明问题，但如果这 5% 全是
`budget_exhausted`，说明是容量问题（加预算/加 provider），
如果全是 `provider_error`，说明是上游问题（换 provider）。混在一起看只会瞎调。

## 3. 方案取舍

| 方案 | 取舍 | 结论 |
|---|---|---|
| **进程内 deque + 排序取分位数** | 零依赖、零运维；重启归零、多 worker 不聚合 | ✅ |
| Prometheus + exporter | 长期存储与多实例聚合；但个人项目多一个要盯的组件、多一份 9090 暴露面 | ❌ 记在 T7 备选 |
| 直方图桶（固定内存、可合并） | 内存恒定；但需要选桶边界，10~12000ms 跨两个数量级，桶会很粗 | ⏸ 上量再换 |
| 只记累计均值 | 最省；但均值会被长尾骗（10 个 3s + 1 个 30s，均值仍好看） | ❌ 必须看分位数 |

**为什么分位数用「最近的秩」而不是线性插值**：样本 20 个时插值会给出一个
数据库里不存在的值（比如 p95 = 7312.7ms），排障时容易误以为「有这一条慢请求」。
最近的秩只返回真实存在的样本值。

**窗口 15 分钟 + 最多 2000 样本**：2000 个 float 约 16KB，排序约 0.2ms，
放在请求路径上可忽略；15 分钟足够看到「刚才那次发布有没有影响」。

## 4. 改动

新增 `app/metrics.py`（约 150 行，纯标准库），`app/obs.py` 的中间件写入，
`GET /metrics` 输出快照。

埋点位置：

| 指标 | 采集点 |
|---|---|
| `requests` / `latency` | `ObservabilityMiddleware`（包住整个请求） |
| `degrade` | `RootCauseClassifier.classify`（按 `LLMError.code` 分原因） |
| `json.fail` / `json.recovered` | `llm_client._extract_json`（按失败类型） |
| `confidence_hist` | `classify` 成功分支，0.1 分桶 |
| `tokens` | `llm_client._record_usage` |

为了让降级原因可归因，给 `LLMError` 加了 `code`：

```python
raise LLMError(msg, code="budget_exhausted")   # 预算不够，预期行为
raise LLMError(msg, code="json_fail")          # 模型没给 JSON
raise LLMError(msg, code="provider_error")     # 网络/HTTP
raise LLMNotConfiguredError(msg)               # code="not_configured"
```

之前上层只能靠 `str(e)` 里有没有「skipped(剩余预算不足」来判断原因，一改文案就断 ——
这是典型的「用错误信息当控制流」。

## 5. 验收

| # | 项 | 结果 |
|---|---|---|
| 4.1 | 有 P50/P95/P99/max | ✅ `{"p50":1.6,"p95":1.6,"p99":1.6,"max":1.6}` |
| 4.2 | 置信度分桶 | ✅ `{"0.9": 1}` |
| 4.3 | token 最近一小时 | ✅ `{"prompt":120,"completion":30,"total":150}` |
| 4.4 | 成功时 `degrade.by_level.none` +1 | ✅ |
| 4.5 | `/health` 不计入延迟分位数 | ✅ `excluded_from_latency=3` |
| 5.3 | provider 500 → 按 `provider_error` 计数 | ✅ |
| 5.4 | 非 JSON → `no_json` | ✅ |
| 6.1 | 围栏 JSON 救回 → `recovered_by_kind.code_fence` | ✅ |

容器内另测了无 Key 场景：`degrade.by_level = {"not_configured": 1}`。

**验收过程中修的一个真问题**：`degrade.rate` 原来用「总请求数」当分母，
而 `/health` 每 30s 一次，于是 5 次请求里 1 次降级算出 20%，
实际业务请求是 100%。已改成 `denominator = total - excluded_from_latency`，
并在响应里显式给出分母（不让人猜）。

## 6. 边界

- **重启归零**。进程重启后所有计数清零，看不到「昨天」。
  唯一出路是落存储（T7-2 的 SQLite）或上 Prometheus。
- **多 worker 不聚合**。`--workers 4` 时每个进程各有一份，`/metrics` 只反映处理
  这次请求的那个进程。当前单进程部署没问题，扩 worker 前必须先解决
  （要么上 exporter，要么按进程暴露再由 nginx 汇总）。
- **`confidence_hist` 只统计 LLM 通道**。规则通道的置信度是代码里的常数
  （0.8/0.85/0.9），混进来会变成两根柱子，掩盖模型行为。所以无 Key 部署时这个
  直方图是空的 —— 这是设计，不是 bug。
- **没有成功率的分母修正**：`ok` 包含 4xx？不包含，4xx 单独计。
  但 `/classify` 返回 200 且 `degraded=true` 时仍记 `ok` —— 正确，
  因为它 HTTP 层面成功了；可信度看 `degrade` 指标。
- **分位数窗口内样本数少时 p95 ≈ max**。前 10 个请求的 P95 没有统计意义，
  看 P95 至少要几十个样本，这一点在使用时要有意识。

## 7. 面试讲法

> 我只做五个指标，因为再多就没人看了。其中「降级率按原因拆」是 Agent 特有的：
> HTTP 200 不代表结果可信，降级率 5% 如果全是预算耗尽，那是容量问题；
> 全是 provider 错误，那是上游问题。混在一起看只会瞎调。
>
> 另外两个取舍：分位数用最近的秩不用插值，因为插值会给出一个不存在的数值，
> 排障时容易被误导；`/health` 必须排除在延迟窗口外，否则探针每 30 秒一次 1ms，
> 能把 P95 稀释得看不出问题 —— 这是个很容易忽略的坑，我一开始的分母也踩了。

---

## 补记（2026-11-12，接真 Provider 后发现）

**降级率口径错了**：`degrade.by_level` 里含 `none`（正常走通），
但 `degrade.total` 用的是 `sum(by_level.values())`，于是「降级率」实际是
「产生过通道决策的请求占比」。面板上出现过自相矛盾的一屏：

```
降级率 57.1%
降级构成: none 5, provider_error 2      ← 只有 2 笔是真降级
```

同时 `record_degrade("none")` 也给分钟桶的 `degrade` 加了 1，
折线图上「降级率」在完全正常时也是 100%（每条请求都是 none）。

修法：

```python
decisions = sum(self._degrade.values())
degraded  = decisions - self._degrade.get("none", 0)   # none 不算降级
...
"rate": degraded / business
# 分钟桶只在 level != "none" 时 +1
```

顺手把 `decisions` 也返回了 —— 「降了 3 笔」和「3 笔里有 2 笔是真降级」
是两个不同的问题，前者看影响面，后者看构成。

**教训**：指标口径不能只看「有没有算」，要看「算出来的数合不合常识」。
一屏里出现 `57.1%` 和「构成里只有 none」并存，就是口径错了的自证。
