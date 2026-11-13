# ssrm-aiops 优化任务清单（仓库现名 agent-gatekeeper）

> 依据：实际仓库 `/Users/pengximei/Desktop/面试/AI研发/ssrm-aiops`（1256 行 Python + React SPA + nginx）
> 对应方案：`ssrm-aiops_生产化差距与优化方案.md` §1.5 代码体检复核
> 生成时间：2026-11-10

---

## 摘要

| 批次 | 主题 | 任务数 | 工时 | 一句话目标 |
|---|---|---|---|---|
| **T0** | 修 3 个硬伤 + 成本可见 | 4 | **0.5 天** | 消除"读代码就被抓"的风险 |
| **T1** | 预算、连接、门禁收口 | 6 | 1.25 天 | 最坏耗时从 180s 降到 12s |
| **T2** | 可观测 | 4（+1 遗留） | 1.5 天 | 出事能拉出完整链路 |
| **T3** | Prompt 版本化 + 回归 | 5 | 1.25 天 | 改 prompt 有据可依 |
| **T4** | 检索升级 | 6 | 2.5 天 | 从"我说"到"我测" |
| **T5** | 状态机 + 工具（LangGraph） | 5 | 5 天 | 从分类器变成 Agent |
| **T6** | 工程卫生 | 5 | 1.25 天 | 删死代码、补测试、界面外框 |
| **T7** | 观测与诊断平台化（千问 JD 定向） | 5 | 3 天 | 从"一个应用"变成"平台雏形" |
| **合计** | | **40** | **约 16.5 天** | |

---

## 三条执行路径

| 路径 | 适合 | 顺序 | 工期 |
|---|---|---|---|
| **A｜保命** | 面试就在本周 | T0 全部 → T6-4 → T6-1 | **半天** |
| **B｜扎实** | 还有两周 | T0 → T1 → T3 → T2 → T4 | 6.5 天 |
| **C｜完整** | 有一个月，想彻底补齐 | A → B → T5 → T6 | 12.5 天 |
| **D｜定向千问** | 面「Agent 观测与诊断平台」岗 | T0 → T1 → T2 → T7 全部 → T3-2 | **6.5 天** |

**路径 D 的取舍**：T5（LangGraph 多步 Agent）可以整批砍掉——那个岗位要的是"能诊断别人的 Agent"，不是"自己会写多步 Agent"；反过来 T7（回放、trace 落库、OTel、反馈闭环、六类故障规则）必须全做，因为那五件事逐条对着 JD 的职责 2/3/4/5。

**强烈建议先走 A。** 理由：T0-1 和 T6-4 是"表述与代码不符"的风险，面试官打开仓库就能验证，**这个损失是信任层面的，不是技术层面的**。半天就能消掉。

---

## T0 · 修 3 个硬伤（0.5 天）

### T0-1 🔴 修复"宽松 JSON 降级"从未生效

| 项 | 内容 |
|---|---|
| **为什么** | 现在 400 后 `continue`，下一轮仍用 `json_mode=True`，实际是"直接失败跳下一家"。注释与简历都声称会降级为宽松提取。**开源仓库可被当场验证** |
| **改哪里** | `app/llm_client.py:90-100`（`_call_with_retry`） |
| **工时** | 30 分钟 |

```python
async def _call_with_retry(self, provider: Provider, system_prompt: str, user_payload: str) -> dict[str, Any]:
    """单 Provider 重试；json_object 不支持时立即降级宽松提取（不消耗重试次数）。"""
    try:
        return await self._call(provider, system_prompt, user_payload, use_json_mode=True)
    except _JSONModeUnsupported:
        logger.info("[LLMClient] %s 不支持 json_object，降级宽松提取", provider.name)
        return await self._call(provider, system_prompt, user_payload, use_json_mode=False)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as first_err:
        last_err: Exception = first_err
        for attempt in range(settings.llm_max_retries):
            try:
                return await self._call(provider, system_prompt, user_payload, use_json_mode=True)
            except _JSONModeUnsupported:
                return await self._call(provider, system_prompt, user_payload, use_json_mode=False)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
                last_err = e
                logger.warning("[LLMClient] %s attempt %d 失败: %s", provider.name, attempt + 2, e)
        raise LLMError(f"{provider.name}: {last_err}")
```

**顺带修掉的隐藏问题**：原代码 `last_err` 在 `_JSONModeUnsupported` 分支未赋值，全部走该分支时最终抛 `LLMError("None")`，错误信息无意义。

**验收**：mock 一个 provider，第一次返回 400，第二次返回带 ``` 围栏的 JSON。断言：调用次数 2、第二次 `use_json_mode=False`、能解析出结果。

---

### T0-2 🔴 限速改用真实客户端 IP

| 项 | 内容 |
|---|---|
| **为什么** | nginx 反代后 `request.client.host` 恒为 nginx 容器 IP → `20/min` 变成**全站** 20/min。一个人刷，所有人被限 |
| **改哪里** | `app/main.py:46-60` |
| **工时** | 45 分钟 |

```python
import time

RATE_LIMIT_PER_MIN = 20
_rate_bucket: dict[str, list[float]] = {}
_last_sweep = 0.0


def _client_ip(request: Request) -> str:
    """优先取 nginx 注入的真实 IP（frontend/nginx.conf 已设置 X-Real-IP / X-Forwarded-For）"""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(request: Request) -> None:
    """内存滑动窗口限速（单实例够用）。每 5 分钟清理一次过期 IP，避免字典无限增长。"""
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep > 300:
        for k in [k for k, v in _rate_bucket.items() if not v or now - v[-1] > 60]:
            _rate_bucket.pop(k, None)
        _last_sweep = now

    ip = _client_ip(request)
    window = [t for t in _rate_bucket.get(ip, []) if now - t < 60]
    if len(window) >= RATE_LIMIT_PER_MIN:
        _rate_bucket[ip] = window
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    window.append(now)
    _rate_bucket[ip] = window
```

调用点同步改：`_check_rate(request.client.host)` → `_check_rate(request)`（`main.py:421`、`436`、`487`）。

**验收**：`docker compose up` 后，
```bash
for i in $(seq 1 22); do curl -s -o /dev/null -w "%{http_code} " -H "X-Real-IP: 1.1.1.1" -X POST localhost:8001/api/classify -d '{"incident":{}}' -H 'Content-Type: application/json'; done
# 期望：前 20 个 422/200，第 21-22 个 429
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Real-IP: 2.2.2.2" -X POST localhost:8001/api/classify -d '{"incident":{}}' -H 'Content-Type: application/json'
# 期望：200/422（不同 IP 不受影响）
```

> ⚠️ 安全前提：后端只绑 `127.0.0.1:8002`，公网打不到，所以信任 `X-Real-IP` 是安全的。若将来后端直接暴露，必须改为只信任内网来源的该头。

---

### T0-3 🔴 实现证据可验证性校验

| 项 | 内容 |
|---|---|
| **为什么** | 简历写"强制模型引入输入字段作为判断依据，输出逐条可人工复核"。代码只校验枚举，**不校验 evidence 是否来自输入**。被问"你怎么验证它没编证据"时是空的 |
| **改哪里** | 新增 `app/evidence.py`；`app/classifier.py:70-91` 调用 |
| **工时** | 1.5 小时 |

```python
# app/evidence.py
import re

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fa5]{2,6}")
_STOP = {"说明", "因为", "所以", "这个", "可以", "判断", "输入", "证据"}


def _flatten(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            out.extend(_flatten(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_flatten(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def evidence_score(evidence: str, incident: dict) -> float:
    """evidence 中的关键片段有多少能在输入原文里找到。0~1，<0.5 视为"无据"。

    不依赖模型自省，是白盒可审计的一层。
    """
    pool = " ".join(_flatten(incident)).lower()
    toks = [t for t in _TOKEN.findall(evidence or "") if t not in _STOP]
    if not toks:
        return 0.0
    return sum(1 for t in toks if t.lower() in pool) / len(toks)
```

在 `classifier.classify()` 的 LLM 分支里接上：

```python
result["root_cause_class"] = self._validate_class(result.get("root_cause_class"))

ev = evidence_score(result.get("evidence", ""), incident)
result["evidence_score"] = round(ev, 3)
if ev < 0.5:
    # 证据无法回溯 → 强制降置信度，不允许"看起来很有把握"地编
    result["confidence"] = min(float(result.get("confidence", 0)), 0.4)
    result["evidence_warning"] = "判断依据未能回溯到输入原文"
result["source"] = "llm"
result["llm_provider"] = result.pop("_provider", "unknown")
result["needs_human_review"] = result["confidence"] < settings.min_confidence_for_auto_action
return result
```

**验收**：构造两条输入——
1. evidence 正常引用 `EA_TIMEOUT`、`504` → `evidence_score ≥ 0.8`，置信度保持；
2. evidence 写"看起来是网络问题导致供应商数据同步异常" → `evidence_score < 0.5`，置信度被压到 0.4，`needs_human_review=True`。

---

### T0-4 加 `max_tokens` 与 usage 统计

| 项 | 内容 |
|---|---|
| **为什么** | 输出长度无上界；`resp.json()["usage"]` 被丢弃，**成本完全不可见** |
| **改哪里** | `app/config.py`、`app/llm_client.py:102-129` |
| **工时** | 30 分钟 |

```python
# config.py 新增
llm_max_tokens: int = 512
```

```python
# _call 里
body["max_tokens"] = settings.llm_max_tokens
...
data = resp.json()
usage = data.get("usage") or {}
logger.info(
    "[LLMClient] provider=%s model=%s in=%s out=%s",
    provider.name, provider.model, usage.get("prompt_tokens"), usage.get("completion_tokens"),
)
```

**验收**：日志里每次调用都有 in/out token 数；`max_tokens` 生效（让模型输出超长内容时被截断）。

---

## T1 · 预算、连接、门禁收口（1 天）

### T1-1 请求级超时预算（3 小时）

| 项 | 内容 |
|---|---|
| **为什么** | 现在最坏 30s × 2 次 × 3 provider = **180s**，而 nginx `read_timeout=120s` → **nginx 先 504，后端还在烧 token** |
| **改哪里** | 新增 `app/budget.py`；`classifier.py`、`rag_assistant.py`、`llm_client.py` 贯穿 |
| **工时** | 3 小时 |

```python
# app/budget.py
import time


class Budget:
    def __init__(self, total_ms: int):
        self.total_ms = total_ms
        self.deadline = time.monotonic() + total_ms / 1000

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def slice(self, want_ms: int) -> int:
        return min(want_ms, self.remaining_ms())

    def exhausted(self) -> bool:
        return self.remaining_ms() <= 0
```

```python
# config.py
request_budget_ms: int = 12000      # 单请求总预算
llm_timeout_per_try_ms: int = 3000  # 单个 Provider 单次尝试
```

```python
# llm_client.chat_json 签名加 budget，循环里
for provider in self.chain:
    per_try = budget.slice(settings.llm_timeout_per_try_ms)
    if per_try < 300:
        errors.append(f"{provider.name}: skipped(budget)")
        break
    ...
```

```python
# main.py 入口
budget = Budget(settings.request_budget_ms)
result = await classifier.classify(req.incident, budget)
```

预算不够时不许"硬着头皮调"，要返回带 `degraded: true` 的规则基线结果。

**验收**：把 `llm_timeout_per_try_ms` 设成 500 模拟慢 provider，断言总耗时 < 13s 且响应里有 `degraded` 标记。

---

### T1-2 httpx 全局连接池 + 三段超时（1.5 小时）

| 项 | 内容 |
|---|---|
| **为什么** | 每次调用 `async with httpx.AsyncClient(...)` 新建客户端 → 无复用、每次 TLS 握手；单一 `timeout=30` 不能区分"连不上"和"算得慢" |
| **改哪里** | `app/llm_client.py:117-121`、`app/embeddings.py:52`、`main.py` 的 lifespan |
| **工时** | 1.5 小时 |

```python
# LLMClient
@property
def client(self) -> httpx.AsyncClient:
    if self._client is None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return self._client

async def aclose(self):
    if self._client is not None:
        await self._client.aclose()
        self._client = None
```

```python
# main.py lifespan
yield
await llm_client.aclose()
logger.info("LLM 连接池已关闭")
```

**验收**：压 20 并发、跑 100 次调用，`ss -tn state established | grep :443 | wc -l` 明显小于请求数（连接被复用）。

---

### T1-3 `needs_human_review` 收口到后端（1 小时）

| 项 | 内容 |
|---|---|
| **为什么** | 规则路径有该字段、LLM 路径没有；前端用 `d.needs_human_review ?? (c < 0.85)` 兜底 → **门禁判定分散在前后端两处，改一处忘一处** |
| **改哪里** | `app/classifier.py`（T0-3 已顺带加上）、`frontend/src/App.jsx` |
| **工时** | 1 小时 |

- 后端：**所有分支**（LLM / 规则 / unknown）都必须返回 `needs_human_review` 与 `confidence`；
- 前端：删掉 `?? (c < 0.85)` 兜底，只读后端字段；`0.85` 这个魔法数字从 JS 里消失。

**验收**：决策逻辑里 0.85 出现 0 次（`grep` 仅剩 Tags.jsx 的展示配色，含「非判定」注释）；
三条路径（LLM 成功 / LLM 降级规则 / 纯规则）的响应 JSON 字段一致，均含 `needs_human_review`。

---

### T1-4 nginx 超时与后端对齐（0.5 小时，实际扩展到 1.5 小时）

| 项 | 内容 |
|---|---|
| **为什么** | `proxy_read_timeout 120s` 是"允许挂 2 分钟"，而 T1-1 之后后端承诺 12s 内必返回。两者语义不一致，且 120s 会让慢请求长期占连接 |
| **改哪里** | `frontend/nginx.conf` |
| **工时** | 30 分钟（+ 1 小时做 upstream keepalive / 限流 / 429 同构） |

```nginx
# 后端承诺单请求 12s 内返回（见 settings.request_budget_ms），留 8s 余量
proxy_connect_timeout 5s;
proxy_send_timeout 20s;
proxy_read_timeout 20s;
```

**同一轮顺带补的四处**（都是配置层，不改代码）：

| 补什么 | 为什么 |
|---|---|
| `upstream` + `keepalive 32` + `proxy_set_header Connection ""` | 不加则每个请求新建到后端的 TCP 连接，长连接池形同虚设 |
| `limit_req_zone` + `limit_req burst=5 nodelay` | 应用层限流是进程内计数，**多 worker 后各算各的等于没限**；nginx 的漏桶在 worker 间共享 |
| `client_max_body_size 2m` | 默认 1m，显式写出来避免以后支持日志上传时莫名 413 |
| `error_page 429 = @rate_limited` 返回 JSON | nginx 默认 429 是 HTML，前端 `res.json()` 解析失败只能显示 "HTTP 429" |

**验收**（容器实测，非推演）：

```
nginx -t                                    → syntax is ok
GET /api/classify                           → 200（反代通）
连续 7 次                                    → 200×6 后 429（rate 20r/m + burst 5）
429 响应                                     → Content-Type: application/json
                                              {"detail":"请求过于频繁，请稍后再试"}
                                              Retry-After: 3
```

坑：`add_header` 默认只对 2xx/3xx 生效，429 要加 `always` 才会带上 `Retry-After`。

---

### T1-5 优雅退出（0.5 小时）

| 项 | 内容 |
|---|---|
| **为什么** | Agent 请求 5~10s，`docker restart` 直接 kill 会打断在途请求 |
| **改哪里** | `Dockerfile`（CMD 加 `--timeout-graceful-shutdown`）、`docker-compose.yml`（`stop_grace_period`） |
| **工时** | 30 分钟 |

```yaml
# docker-compose.yml api 服务
stop_grace_period: 30s
```

```dockerfile
# Dockerfile CMD
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--timeout-graceful-shutdown", "25"]
```

**验收**：发一个 `/classify` 请求，1 秒后 `docker restart`，该请求仍返回完整结果。

### T1-6 接真 Provider 后的启动期问题（2 小时，**已完成**）

> 原清单没这项，是接上智谱真 Key 之后暴露的：启动被 embedding 卡 90 秒、
> 余额不足的 429 被当成限流重试、失败日志 `reason` 是空串、3s 时间片把成功掐成超时。
> 详见 [`T1-06-startup-and-provider.md`](T1-06-startup-and-provider.md)。

---

## T2 · 可观测（1.5 天）

### T2-1 trace_id 中间件（2 小时）

新增 `app/obs.py`：`contextvars` 存 `trace_id`；中间件生成（或复用 `X-Request-Id`），响应头带回。

**验收**：响应头有 `X-Trace-Id`；同一请求的所有日志行都带同一个 id。

### T2-2 结构化 JSON 日志（2 小时）

`logging.basicConfig` 换成 JSON formatter，字段固定：`ts / level / trace_id / event / stage / provider / duration_ms / tokens / outcome`。

**验收**：`docker compose logs api | tail -5 | jq .` 每行都能解析。

### T2-3 `/metrics` 五个指标（3 小时）

| 指标 | 采集点 |
|---|---|
| `request_total` / `success` | 中间件 |
| `request_duration_ms`（P50/P95） | 中间件 |
| `degrade_level` | `classifier` 降级时 |
| `json_parse_fail`（按类型） | `_extract_json` 失败处 |
| `confidence_bucket`（0.1 分桶） | 输出处 |

先用进程内滑动窗口统计，不上 Prometheus（一个人维护，别引入运维负担）。

**验收**：跑 100 条评测后 `/metrics` 能给出 P95 与降级率。

### T2-4 成本归因（1 小时）

每请求累计 `prompt_tokens + completion_tokens`，写进响应字段 `usage` 与日志。

**验收**：`/metrics` 能报"过去 1 小时总 token / 单请求均值"。

### T2-5 /eval 改异步任务（0.5 天，**未做**）

**为什么**：`/eval` 30 条串行 × 数秒 = 100s+，超过 nginx 的 `proxy_read_timeout 20s`
（T1-4）和优雅退出的 25s 窗口（T1-5）。现在必然 504，且重启时必然被 cancel。

**怎么做**：`POST /eval` 立刻返回 `{task_id}`，后台 `asyncio.create_task` 跑；
`GET /eval/{task_id}` 返回 `{status, progress, result}`。任务态放进程内 dict（单实例够用）。

**验收**：`POST /eval` 200ms 内返回 task_id；轮询能看到 progress 递增；
nginx 不再出现 504。

**状态**：这是 T1 实测时发现的**架构不匹配**，不属于「可观测」范畴，
单列出来不混进 T2 的验收计数。

### T2-6 可观测面板（0.5 天，**已完成**）

**为什么**：T2-1~T2-4 的数据全在容器日志和 `/metrics` 的 JSON 里，只有写代码的人能用。
值班同学看到「变慢了」还是得先来问人。

**怎么做**：
- 后端 `app/metrics.py` 增加 60 个分钟桶（只存可累加量）+ 最近 50 条请求流水；
- `/metrics` 返回体加 `series` 与 `recent_requests`，**不新增端点**（只有一个消费方）；
- 前端 `/observability` 页：6 个 KPI + 4 张趋势图 + 3 组分布 + 请求流水表；
- 折线图手写 SVG（`TrendChart.jsx`，约 100 行），不引 echarts。

**验收**：`series.points` 恒为 60（含补零）；探针不进流水；
顶层与折线图错误率/降级率同口径；1440×1400 实测布局无错位。

**文档**：[`T2-06-observability-dashboard.md`](T2-06-observability-dashboard.md)

---

## T2 完成情况（2026-11-12）

| 任务 | 文档 | 验收 |
|---|---|---|
| T2-1 trace_id | [`T2-01-trace-id.md`](T2-01-trace-id.md) | 7 项（含并发不串、非法 header 拒绝） |
| T2-2 JSON 日志 | [`T2-02-json-logs.md`](T2-02-json-logs.md) | 5 项 + 容器实测全行可解析 |
| T2-3 五个指标 | [`T2-03-metrics.md`](T2-03-metrics.md) | 8 项（含 `/health` 排除、降级按原因） |
| T2-4 成本归因 | [`T2-04-cost-attribution.md`](T2-04-cost-attribution.md) | 3 项（响应体 / 指标 / 无 Key 场景） |
| T2-6 可观测面板 | [`T2-06-observability-dashboard.md`](T2-06-observability-dashboard.md) | 接口 3 项 + 页面 4 项（含 2×2 栅格、空状态、口径一致） |

新增模块：`app/obs.py`（trace + 日志 + token 累加器）、`app/metrics.py`。
新增端点：`GET /metrics`。改动端点：`/classify`、`/assist` 响应体加 `trace_id` + `usage`。
新增页面：`/observability`（React 路由，走 `/api/metrics`）。

T2-5（`/eval` 改异步）仍**未做**，见上。

---

## T3 · Prompt 版本化 + 回归（1.25 天）

> **T3-0 已完成**（详见 [`T3-00-rag-retrieval-eval.md`](T3-00-rag-retrieval-eval.md)）：
> RAG 检索评测集（常规 30 + 困难 10）+ Recall@1/@3/MRR 指标 + `rag_min_score` 阈值扫描。
> 结论：困难集上关键词召回 Recall@1 90% → 加 LLM 重排 100%；0.55 是精度不掉的最高覆盖点。
> T3-2 的 20 条分类回归集可直接复用本任务的评测集结构与跑法。
>
> **T3-1~4 已完成**（详见 [`T3-01-prompt-versioning.md`](T3-01-prompt-versioning.md)）：
> 三处 prompt 外置到 `prompts/`（index + active + 启动占位符校验），20 条手工回归集，
> `make eval` 一条命令，结果带 `prompt_version`、分数归档 `prompts/scores.json`。
> v1 基线：回归 19/20（0.95，留 REG-04 miss 不调）/ 困难 25/25。
>
> **T3-5 已完成**（详见 [`T3-02-evidence-refs.md`](T3-02-evidence-refs.md)）：
> `root_cause.v2` 要求 `evidence_refs`（字段 + 值），门禁改成「字段存在吗、值相等吗」，
> 分数与文本长短无关；refs 缺失仍回退 T0-03 字面匹配。v2 基线：回归 0.95 / 困难 0.96，
> refs 采用率 100%，**回归集上字面判据误杀 3 条诚实样本 → 结构化判据零误杀**。

### T3-1 `prompts/` 目录 + loader（2 小时）

```
prompts/
├─ root_cause.v1.md
├─ rag_answer.v1.md
└─ index.json          {"root_cause": {"active": "v1", "versions": {"v1": {...}}}}
```

`app/prompts.py` 提供 `load(name) -> (text, version)`，启动时校验 `active` 存在。

### T3-2 20 条回归集（2 小时）

`data/regression.jsonl`：5 类根因各 4 条，**包含 T0-3 里那两条证据校验的 case**。

### T3-3 `make eval` 一条命令（2 小时）

```makefile
eval:
	python -m app.evaluation.eval 30 --hard
	python -m app.eval 20 --regression
```

输出格式要能对比："v1: hard 0.92 / regression 1.00"

### T3-4 trace 记 prompt 版本（1 小时）

每次调用日志带 `prompt_version`。

**验收**：改 prompt → 跑 `make eval` → `index.json` 里 v2 的分数被写入，能看到 v1 vs v2 对比。

### T3-5 证据从自由文本改为结构化引用（2 小时）

**为什么**：T0-03 的 `evidence_score` 是字面匹配，实测有反转 —— 只丢两个错误码的证据得 1.000，
引了四个字段还讲通顺的只得 0.684。原因有三：结构化输入里字段名是英文（`retry_count`）而证据是中文
（「重试 4 次」）、分母是证据自身的词数（越详细越低）、编码锚点数量压不住中文 2-gram。
这三条都不是调权重能解决的，必须换判据。

**怎么改**：输出 schema 增加 `evidence_refs`，校验退化为「字段存在吗、值相等吗」。

```json
{"root_cause_class": "...", "confidence": 0.95,
 "evidence_refs": [{"field": "error_code", "value": "EA_TIMEOUT"},
                   {"field": "http_status", "value": "504"}],
 "evidence": "EA 侧同步超时", "suggested_action": "..."}
```

- `evidence_score = 有效引用数 / 总引用数`，与文本长短无关
- `evidence_refs` 缺失（模型不按 schema 输出）时退回 T0-03 的字面匹配，行为不变
- 前端「判断依据」区域渲染成「字段 → 值」的引用条，比一段话更可审计

**为什么排在 T3**：schema 变更会改变所有样本的输出，**必须配回归集才能证明没变差**。
先有 T3-1/T3-2 再做这一步，顺序不能反。

**验收**：同一批 10 条标定样本，诚实样本全部 ≥0.9，编造样本 ≤0.2；
`make eval` 的 accuracy 不低于 v1。

**注意**：换判据后 0.35 这个阈值作废，需要重新推导（结构化引用下诚实样本预期 ≥0.9，
阈值应移到 0.7 附近）。真正的统计标定依赖真实 LLM 输出 + 人工标注画 P/R 曲线，
在有真实流量前只做方向性验证，阈值宁紧勿松 —— 门禁动作是压置信度转人工，
拦错诚实样本只多一次人工确认，放进编造样本才是真风险。

**实际落地**（[`T3-02-evidence-refs.md`](T3-02-evidence-refs.md)）：
阈值按预判设为「占比 0.7 + 条数 2」。标定结果是诚实样本 1.000、纯编造 0.000，阈值落在空档里不敏感。
原任务的验收「accuracy 不低于 v1」只做到一半：回归持平（0.95），困难集 24/25（v1 25/25），
掉的那条经因子实验证明是「任何扰动都会翻」的边界样本（52 字中性填充同样能翻），
顺带暴露了 25 条样本上的 ±1 差异无法与抖动区分。更值得记的数字是**门禁误杀从 3 条降到 0 条**。

---

## T4 · 检索升级（2.5 天）

> **T4 已收口（2026-11-13）**，各项结论：
> - **T4-1 结论「不做」**：KB 已扩为 150/100/120 条带簇结构，切块前提不成立；
>   实验证明 resolution/root_cause 入索引全线变差（credit 簇级 R@1 87.5% → 52.5%），详见 [T4-01](T4-01-chunking-experiment.md)
> - **T4-2 / T4-3 被 T3-0 超越**：三域 230 条查询 + Recall@1/@3 / MRR@8 + strict/cluster 双口径 + 阈值扫描已落地
> - **T4-4 已完成**：向量+字符双路 RRF（k=60）混合排序，采信门禁改「任一信号过阈值」；
>   顺带修复 rag_eval 的 PYTHONHASHSEED 漂移（relevant 跨簇查询取簇并集），详见 [T4-04](T4-04-rrf-hybrid-retrieval.md)
> - **T4-5 已完成**：向量缓存按 KB 内容 sha256 失效（旧实现只比条数），详见 [T4-05](T4-05-cache-hash-invalidation.md)
> - **T4-6 已完成**：重排不再伪造排名分，keyword+rerank 门禁恢复有效，详见 [T4-06](T4-06-honest-rerank-score.md)

### T4-1 切块：30 条 → 90 chunk（2 小时）

现在一条工单一个向量（`title + symptom`），`resolution` 没参与检索但参与了生成。
改为按字段切三块：`symptom` / `root_cause` / `resolution`，候选集 30 → 90。

### T4-2 50 条 query 标注集（3 小时）

`data/rag_eval.jsonl`：

```json
{"query": "审批通过了但系统里没任务", "expected": ["ONCALL-001", "ONCALL-002"]}
```

来源：把 30 条工单改写成口语化提问 + 几条"系统里没有"的负样本（期望空结果）。

### T4-3 `recall@K` 脚本（2 小时）

```python
# scripts/eval_retrieval.py
# 对每条 query：检索 top_k → 与 expected 求交 → recall@3 / MRR
# 输出：recall@3 / MRR / 平均最高相似度
```

**这是最关键的一步**——有了它，你才能说"我把 recall@3 从 0.62 提到 0.85"，而不是"试了几条感觉还行"。

### T4-4 混合检索 + RRF（4 小时）

向量分 + BM25 分，用 RRF（Reciprocal Rank Fusion）融合：`score = Σ 1/(k + rank_i)`，k 取 60。
`rank_bm25` 库即可，无需 ES。

### T4-5 向量缓存改为文件 hash 失效（1 小时）

```python
# rag_assistant.warmup
kb_hash = hashlib.sha256(KB_FILE.read_bytes()).hexdigest()[:16]
if cache.load(CACHE_FILE) and cache.meta.get("kb_hash") == kb_hash:
    ...
```
`vector_store.save()` 增加 `meta` 字段。

### T4-6 修"降级模式伪造 score"（1 小时）

`rag_assistant.py:123-124` 的 `score = 1.0 - rank*0.1` 会让排第一恒为 1.0，**永远不触发 0.55 阈值**。

改法二选一（推荐 A）：

**A. 不伪造分数，明确区分来源**
```python
c["score"] = None
c["match_type"] = "keyword"     # 与 "vector" 区分
```
阈值判断按 `match_type` 走不同常量（vector 0.55 / keyword 0.25），响应里带 `confidence_basis`。

**B. 用真实字符重叠率当分数**（保持接口不变，但分数含义仍是"字符覆盖率"，要在文档里说清）

**验收**：构造一条与知识库完全无关的提问，降级模式下 `needs_human_review=True`（现在会返回 1.0 的高置信）。

---

## T5 · 状态机 + 工具（5 天，LangGraph）

> **状态（2026-11-13）**：已完成，但 LangGraph 改道为自研确定性状态机——理由见
> `T5-02-handle-pipeline.md` §0（编排不需要智能 / 每步可观测 / 上界是结构保证）。
> 落地项：T5-0（工具层+Domain Pack）、T5-1→状态机 pipeline.py、T5-2→内置只读工具
> +enrich 信息收集、T5-3→SQLite 升级队列+续跑、T5-4→步数/预算上界并入状态机、
> T5-5→credit 对比评测（错分 0/2 自动关闭）。

| 编号 | 任务 | 产出 | 工时 |
|---|---|---|---|
| T5-1 | LangGraph 骨架：`intake → retrieve → decide → (act) → generate → verify`，`MemorySaver` 起步 | 能跑通单步，图可视化 | 1 天 |
| T5-2 | 3 个只读工具：`get_service_status` / `search_runbook` / `get_change_history`（本地 mock 即可），统一错误码 + 超时 + 结果裁剪 | 工具层可单测 | 1 天 |
| T5-3 | `interrupt` 人工接管：置信度低时中断，人确认后从中断点续跑 | 状态可持久化到 SQLite | 1 天 |
| T5-4 | 预算与步数控制：`max_iterations=3`、`max_wall_time`、超限时带"当前最优 + 不确定性"退出 | 不会无限循环 | 1 天 |
| T5-5 | 与新评测集对齐：困难集跑一遍，与单步版本对比准确率/成本/延迟 | 有对比数据 | 1 天 |

**注意**：模型调用、校验、降级仍然用你现有的函数，作为 node 塞进图里。**框架只包骨架。**

---

## T6 · 工程卫生（1 天）

> **状态（2026-11-13）：全部完成。** T6-1 死代码删除（pages_legacy 下线，直连后端兜底取消，
> 前端 SPA 是唯一 UI）；T6-2 44 个单测（`pytest -q`，纯函数优先不烧 token）；
> T6-3 GitHub Actions（ruff + pytest，规则取向见 `ruff.toml`）；T6-4 三处简历/README
> 表述逐条核验，全部与代码对齐（详见 `T6-02-tests-and-ci.md`）；T6-5、T6-6 此前已完成。

### T6-1 删 300 行死代码（1 小时，**部分完成**：已抽出，未删除）

> 现状：340 行页面代码已抽到 `app/pages_legacy.py`，`main.py` 593 → 273 行。
> 页面本身保留（直连后端时兜底），彻底删除的成本是 1 个文件 + 1 行 `include_router`。
> 详见 [`T6-01-main-py-slim.md`](T6-01-main-py-slim.md)。

`main.py:206-406`（`/playground`、`/assistant` 两个路由 + `PAGE_CSS`、`PLAYGROUND_CSS`、`ASSISTANT_CSS`、`page_shell`、`COMMON_JS`）。

**依据**：生产由 nginx 提供 SPA，且 `nginx.conf` 的 `location /` 会 `try_files` 到 `index.html`，**这两个后端路由永远不可达**。

删完 `main.py` 从 495 行降到约 200 行——**这是个能当场讲的"重构"故事**。

### T6-2 补 tests/（3 小时）

纯函数优先，性价比最高：

| 被测对象 | 用例 |
|---|---|
| `_extract_json` | 纯 JSON / 带围栏 / 前后有解释 / 无 JSON |
| `_rule_baseline` | 5 类各一条 + 一条 unknown |
| `evidence_score` | 命中 / 全编造 / 空 evidence |
| `Budget` | `slice` 不超剩余 / 耗尽判定 |
| `_validate_class` | 合法 / 非法 / None |

`pytest` + `pytest-asyncio`，含 T0-1 那个 mock provider 的用例。

### T6-3 CI（1 小时）

GitHub Actions：`ruff check` + `pytest`。不搞多版本矩阵。

### T6-4 🔴 简历 / README 表述对齐（1 小时）

> **这条优先级最高，因为它是信任问题。**

现在有三处表述需要和代码对齐：

| 表述 | 现状 | 处理 |
|---|---|---|
| "400 自动降级宽松 JSON 提取" | 未实现 | **做 T0-1 修好**（30 分钟），表述就不用改 |
| "证据链可审计 / 逐条可人工复核" | 只输出字段，没校验 | **做 T0-3 修好**，表述才成立 |
| "评测集回归" | 有 CLI 和接口，但容器内 `/eval` 会超时 | 做 T3-3 改成可跑通，或在 README 注明"本地跑 CLI" |

**原则：能花 30 分钟修好的，就去修；修不了的，改措辞。** 不要留着"打开仓库就能验证不成立"的说法——这个仓库是开源的，面试官真的会打开。

### T6-5 左侧栏 + 可观测拆页（2 小时，**已完成**）

> 原清单没这项，是拿面板实际用起来之后补的：一页塞 6 KPI + 4 图 + 3 组分布 + 50 行流水，
> 值班时必须滚两屏；导航还是每页顶部平铺的胶囊，表达不了「可观测」下面的层级。
>
> 结果：导航抽成 `Layout` 外框，可观测按「看的时间点」拆成 概览 / 请求流水 / 质量分析 三个子路由，
> 数据在 shell 层只轮询一次、子页用 context 取。
> 详见 [`T6-05-ui-shell-and-obs-split.md`](T6-05-ui-shell-and-obs-split.md)。

---

## T7 · 观测与诊断平台化（3 天，千问 JD 定向）

> **为什么单开一批**：T0–T6 的目标是"把一个 Agent 做到生产可用"；T7 的目标是"把定位问题的过程变成平台能力"。
> 前者是应用工程，后者是**诊断平台**。JD 六条职责里有四条（2/3/4/5）落在这一批。
>
> 这一批的共同设计前提：**诊断结论必须带证据、可回放、可被人工推翻**。没有这三点的"智能诊断"就是猜。

### T7-1 请求录制与回放（0.5 天）

| 项 | 内容 |
|---|---|
| **为什么** | 线上问题的第一句话永远是"能复现吗"。现在的 `/eval` 只跑固定数据集，真出事时那条请求的入参、模型原始输出、最终结论都没有留痕，**问题只能靠当事人回忆** |
| **改哪里** | 新增 `app/recorder.py`、`scripts/replay.py`；`main.py` 两个入口各加一行 |
| **工时** | 半天 |

**录制什么**（一条 JSONL 一行，按天分文件 `data/recordings/2026-11-12.jsonl`）：

```json
{"trace_id":"t_7f3a","ts":"2026-11-12T10:21:03Z","endpoint":"/classify",
 "input":{...},"llm_raw":{"provider":"deepseek","content":"{...}","finish_reason":"stop"},
 "result":{"root_cause_class":"ea_sync_timeout","confidence":0.92},
 "meta":{"budget_ms":3118,"degraded":false,"tokens_in":412,"tokens_out":86}}
```

**两个关键设计**：

1. **录 `llm_raw` 而不是只录 `result`**。只录结果，回放时无法区分"模型本来就这么答"和"你的解析/校验改坏了"。录原始 content 才能定位到是模型层还是代码层。
2. **写盘丢线程池**（`asyncio.to_thread`），不阻塞事件循环；失败只记日志不抛——**录制是旁路，不能影响主流程**。

**回放怎么做**（不去打真 LLM）：

```python
# scripts/replay.py 核心：把录制里的 llm_raw 塞回客户端，复跑同一套逻辑
class ReplayLLM:
    def __init__(self, rec): self.rec = rec
    async def chat_json(self, system_prompt, user_payload, budget=None):
        return json.loads(self.rec["llm_raw"]["content"]) | {"_provider": "replay"}

cls = RootCauseClassifier(ReplayLLM(rec))
got = await cls.classify(rec["input"])
diff = {k: (rec["result"].get(k), got.get(k))
        for k in rec["result"] if rec["result"].get(k) != got.get(k)}
```

**验收**：
- 20 条录制回放，除 `latency` / `tokens` 外字段 **100% 一致**（证明回放保真）
- 故意把 `min_evidence_score` 从 0.35 改到 0.9，回放一致率下降且 diff 能列出**哪几条结论变了** —— 这一步才是回放的真正用途：**改判定逻辑前先跑一遍历史流量，看会影响谁**

**边界**：录制含工单文本，开源仓库必须脱敏。做法是正则打码（手机号/邮箱/单号）+ `data/recordings/` 进 `.gitignore`，仓库里只放 `examples/recordings.sample.jsonl`（手工构造 3 条）。

### T7-2 trace 落 SQLite + 查询接口（1 天，**一阶段完成**）

> 一阶段（内存版）已落地：`app/tracing.py` 收集 span，`GET /traces/{id}` 查单条链路，
> 面板点 trace_id 出瀑布图。剩余：落 SQLite、`GET /traces` 列表与筛选。
> 详见 [`T7-02-trace-spans.md`](T7-02-trace-spans.md)。

| 项 | 内容 |
|---|---|
| **为什么** | 现在 trace 只在日志里，**按 trace_id 拼不出完整链路**（跨 provider 重试、降级、证据门禁是散在 4 个模块的 print）。JD 第 2 条要的是"从用户任务到具体执行步骤的问题追踪" |
| **改哪里** | 新增 `app/trace_store.py`；`main.py` 加 `/traces/{id}`、`/traces`；`llm_client` 每次尝试产一个 span |
| **工时** | 1 天 |

**表结构**（两张表就够，别上 span 表 + 事件表 + 属性表的完整 OTel 模型）：

```sql
CREATE TABLE traces (
  trace_id TEXT PRIMARY KEY, ts TEXT, endpoint TEXT, status TEXT,
  duration_ms INTEGER, degraded INTEGER, provider TEXT,
  tokens_in INTEGER, tokens_out INTEGER,
  root_cause_class TEXT, confidence REAL, error TEXT
);
CREATE TABLE spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, seq INTEGER,
  name TEXT,              -- retrieve | llm.call | evidence.gate | rule_baseline
  start_ms INTEGER, end_ms INTEGER, status TEXT,
  attrs TEXT              -- JSON：provider/model/http_status/retry/finish_reason
);
```

**三个必须先想清楚的工程问题**（面试就问这些）：

| 问题 | 做法 | 不这么做的后果 |
|---|---|---|
| 写库会不会拖慢请求 | `asyncio.Queue(maxsize=1000)` + 单消费者协程批量提交（50 条或 200ms 触发） | 同步 `commit` 每次 fsync，请求尾延迟 +5~20ms |
| 队列满了怎么办 | **丢弃 + 计数 + `/metrics` 暴露 dropped** | 无限队列 = 内存泄漏；阻塞 = 用观测拖垮主流程 |
| 单机 SQLite 能不能扛 | 能，WAL 模式下写不阻塞读；但**多 worker 会锁争用**，那时换 PG | 直接上 PG 是过度设计，面试要说清这条分界线 |

**验收**：
- 一次 `/classify`（含一次 provider 失败、一次降级）后，`GET /traces/{id}` 返回**按 seq 排序的 span 序列**，能看出"第一个 provider 3s 超时 → 切第二个成功 → 证据门禁 0.12 压置信度"
- `GET /traces?limit=50&status=degraded` 能筛出降级请求
- 压 200 并发后 `dropped=0`、P95 尾延迟增量 < 5ms

### T7-3 接入 OpenTelemetry（0.5 天）

| 项 | 内容 |
|---|---|
| **为什么** | JD 加分项明写 OTel / 分布式链路追踪。自研 trace 能证明你懂原理，但**接标准协议**能证明你的东西能被别人的平台消费——这是"平台"和"小工具"的分界 |
| **改哪里** | 新增 `app/otel.py`；`main.py` lifespan 初始化；`llm_client` 的 span 加 `gen_ai.*` 属性 |
| **工时** | 半天 |

**做法**：`FastAPIInstrumentor` 自动埋点 + 手动 span 包 LLM 调用，**属性用 GenAI 语义约定**（这是加分细节，很多人不知道已经有了）：

```python
span.set_attribute("gen_ai.system", "openai")          # 供应商族
span.set_attribute("gen_ai.request.model", provider.model)
span.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
span.set_attribute("gen_ai.usage.output_tokens", usage["completion_tokens"])
span.set_attribute("gen_ai.response.finish_reasons", [choice.get("finish_reason")])
```

**关键取舍**：**不替换自研 trace**。OTel 是导出通道，SQLite 是本地存储，两者用同一个 trace_id 关联——没配 `OTEL_EXPORTER_OTLP_ENDPOINT` 时自动降级为 no-op，本地开发零依赖。

**验收**：`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` + jaeger all-in-one，`/classify` 一次后在 Jaeger 里看到 `POST /classify` → `llm.call` 两层 span，且 LLM span 带 token 属性；不配环境变量时 trace 表照常写、无报错。

### T7-4 诊断反馈闭环（0.5 天）

| 项 | 内容 |
|---|---|
| **为什么** | 现在"准确率"只有一个数，来源是固定评测集。JD 第 1 条要求"处理反馈"、第 4 条要求"诊断准确率"。**没有采纳/推翻记录的准确率，是自评，不是度量** |
| **改哪里** | `trace_store` 加表；`main.py` 加 `POST /diagnosis/{trace_id}/feedback`、`GET /metrics/diagnosis`；前端结果卡片加两个按钮 |
| **工时** | 半天 |

```sql
CREATE TABLE diagnosis_feedback (
  trace_id TEXT, verdict TEXT,          -- accepted | rejected | corrected
  corrected_class TEXT, operator TEXT, note TEXT, ts TEXT
);
```

**判定口径**（这段必须写清，否则准确率是假的）：

| verdict | 含义 | 计入 |
|---|---|---|
| `accepted` 且 `corrected_class` 为空 | 人工认可原结论 | TP |
| `corrected` 且给了新类别 | 结论错、类别被改 | FP，且进混淆矩阵 |
| `rejected`（结论错但说不出正确类别） | 至少判定为不可用 | FP（弱标签） |

> 准确率 = `accepted / (accepted + corrected + rejected)`，**并且必须同时报样本量**。只报 92% 不报 n=12 的，都是耍流氓。

**验收**：造 12 条反馈（8 accepted / 3 corrected / 1 rejected）→ `/metrics/diagnosis` 返回 0.667 且按类别列出错误分布；`corrected` 的记录能反查原 trace 的 evidence，看是"证据不足"还是"规则判错"。

### T7-5 六类 Agent 故障的诊断规则（0.5 天）

| 项 | 内容 |
|---|---|
| **为什么** | JD 第 3 条点名六类故障：**任务失败、执行超时、重复执行、工具调用异常、上下文处理异常、Token 消耗异常**。你项目里这些信号全都有，但**散在各处、没有一条规则把它们变成结论** |
| **改哪里** | 新增 `app/diagnosis_rules.py`（纯函数，无 IO，可单测）；`/traces/{id}/diagnose` 复用 |
| **工时** | 半天 |

**规则表**（每一行都是"信号 → 判定 → 证据字段"，直接可落代码）：

| 故障类型 | 可观测信号 | 判定条件 | 证据字段 |
|---|---|---|---|
| **执行超时** | `budget.remaining_ms` 归零、span 被跳过 | 存在 `status=skipped_budget` 的 span | `budget_ms`、`elapsed` |
| **任务失败** | `degraded=true`、全 provider 失败 | `provider_failures == len(chain)` | 每个 provider 的 http_status |
| **重复执行** | 同 `idempotency_key` 在窗口内多次出现 | 5 分钟内同 key ≥2 次 | 两次 trace_id |
| **工具调用异常** | span `tool.call` 非 2xx 或超时 | `tool.status != 200` | 工具名、错误码、参数摘要 |
| **上下文处理异常** | `tokens_in > 窗口 × 0.8`、检索为空仍生成 | `tokens_in` 突增 或 `retrieved==0 && answer 非空` | tokens_in、top_k 命中数 |
| **Token 消耗异常** | 单请求 tokens 超 P99、`finish_reason=length` | `tokens > 3×median` 或 被 max_tokens 截断 | tokens_in/out、finish_reason |

**验收**：构造 24 条 span 序列（每类 4 条，含 2 条正常样本做负例）→ 规则命中率 ≥ 90%、**误报 ≤ 1 条**；每条结论必须带 `evidence` 字段（复用 T0-3 的思路：**说得出是哪条 span、哪个字段**）。

> 注意：这一项刻意做成**纯规则、不用 LLM**。理由写在文档里——六类故障的判定边界是确定的（超时就是超时），用 LLM 反而引入不确定性；**LLM 该用在"规则判不出来但需要归因"的那部分**（比如"用户说卡住了但我看不出哪一步错"）。这个"哪里用规则、哪里用模型"的划分，是这一项最值得讲的东西。

---

## 依赖关系

```
T0-1 ─┬─> T0-4 ─> T2-4
T0-2  │
T0-3 ─┴─> T1-3

T1-1 ──> T1-4
  └────> T5-4（状态机必须有预算，否则循环失控）

T1-2 ──> T1-5

T2-1 ──> T2-2 ──> T2-3
T2-1 ──> T3-4

T3-1 ──> T3-2 ──> T3-3

T4-1 ──> T4-2 ──> T4-3 ──> T4-4
T4-5、T4-6 独立

T5-* 依赖 T1-1 + T4-3（要做效果对比，得有检索评测）
T6-* 独立，随时可做

T7-1 ──> T7-4（回放依赖录制；反馈要能挂到具体 trace）
T2-1 ──> T7-2 ──> T7-3（trace span 是 OTel 属性和 SQLite 落库的共同上游）
T7-2 ──> T7-5（规则判定读 span 序列）
T7-1、T7-4 可用录制 + 规则先跑通，不强依赖 T2
```

---

## 文件改动索引

| 文件 | 涉及任务 | 改动性质 |
|---|---|---|
| `app/llm_client.py` | T0-1、T0-4、T1-1、T1-2 | 修 bug + 加预算 + 连接池 |
| `app/main.py` | T0-2、T1-1、T1-3、T1-5、T2-1、T6-1 | 限速 + 预算 + 删死代码 |
| `app/classifier.py` | T0-3、T1-1、T1-3、T3-1 | 证据校验 + 门禁收口 + prompt 外置 |
| `app/rag_assistant.py` | T1-1、T3-1、T4-1、T4-4、T4-5、T4-6 | 检索升级 |
| `app/config.py` | T0-4、T1-1 | 新增配置项 |
| `app/budget.py` | T1-1 | **新增** |
| `app/evidence.py` | T0-3 | **新增** |
| `app/obs.py` | T2-1、T2-2、T2-3 | **新增** |
| `app/prompts.py` | T3-1 | **新增** |
| `prompts/*.md` | T3-1 | **新增** |
| `data/regression.jsonl` | T3-2 | **新增** |
| `data/rag_eval.jsonl` | T4-2 | **新增** |
| `scripts/eval_retrieval.py` | T4-3 | **新增** |
| `tests/*` | T6-2 | **新增** |
| `frontend/src/App.jsx` | T1-3 | 删魔法数字 |
| `frontend/nginx.conf` | T1-4 | 超时对齐 |
| `docker-compose.yml` / `Dockerfile` | T1-5 | 优雅退出 |
| `app/agent_graph.py` | T5-1~5 | **新增** |
| `app/recorder.py` | T7-1 | **新增**（录制，丢线程池） |
| `scripts/replay.py` | T7-1 | **新增**（回放 + diff） |
| `app/trace_store.py` | T7-2、T7-4 | **新增**（SQLite + 异步批量写） |
| `app/otel.py` | T7-3 | **新增**（OTLP，未配置则 no-op） |
| `app/diagnosis_rules.py` | T7-5 | **新增**（六类故障规则，纯函数） |

---

## 如果你只做一件事

做 **T6-4**（表述对齐），加上它依赖的 **T0-1 和 T0-3**。

理由：技术差距可以现场讨论、可以承认"这块我还没做"；但**"简历说了、代码里没有"是另一回事**——它损害的是你所有其他陈述的可信度。而这个仓库是开源的、面试官真的会看。

三件事加起来不到 2 小时，消掉的是最大的一类风险。
