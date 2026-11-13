"""SSRM Ops Agent — FastAPI 服务（端口 8001）

端点：
- GET  /health          健康检查（含 LLM 配置状态）
- POST /classify        单条事件根因分类
- POST /eval            数据集评测（LLM vs 规则基线）
- GET  /stats           数据集统计
- POST /assist          值班助手问答
- GET  /metrics         进程内指标快照（时序 + 请求流水）
- GET  /traces/{id}     单次请求的链路

旧版服务端渲染页面（/playground、/assistant）在 `app/pages_legacy.py`，
线上由 React 前端接管，这里只是保留兜底，不再演进。
"""

import asyncio
import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.core.budget import Budget
from app.core.builtin_tools import build_default_registry
from app.core.escalation import EscalationStore
from app.core.tools import choose_and_run
from app import prompts
from app.agents.classifier import RootCauseClassifier
from app.agents.pipeline import HandlePipeline
from app.domains.credit import pack as credit_pack
from app.domains.csa import pack as csa_pack
from app.domains.ssrm import pack as ssrm_pack
from app.config import settings
from app.evaluation.dataset import load_dataset, stats
from app.core.llm_client import llm_client
from app.observability.metrics import metrics
from app.observability.obs import ObservabilityMiddleware, attach_obs, setup_logging
from app.pages_legacy import router as legacy_pages
from app.agents.rag_assistant import RagAssistant
from app.observability.tracing import span, traces

setup_logging()
logger = logging.getLogger(__name__)

# 多领域注册表（T5）：分类器 / 编排管线 / 领域包三张表同一个 key（pack.name）
_domain_packs = (ssrm_pack, csa_pack, credit_pack)
packs = {p.name: p for p in _domain_packs}
classifier = RootCauseClassifier(llm_client, ssrm_pack)  # /eval 兼容旧引用（ssrm 域）
classifiers = {p.name: RootCauseClassifier(llm_client, p) for p in _domain_packs}

# 多领域助手注册表：每个声明了 assistant 规格的 pack 一个 RagAssistant 实例。
# 引擎是同一个（检索+阈值+grounding），知识库/阈值/人设由 pack 注入 ——「机制与知识分离」
assistants: dict[str, RagAssistant] = {
    _p.name: RagAssistant(llm_client, _p.assistant)
    for _p in _domain_packs if _p.assistant is not None
}
rag = assistants["ssrm"]  # SSRM 值班助手：工具注册表等旧引用的默认实例
registry = build_default_registry(rag)

# 处置编排：每域一条管线；工具注册表目前只有 ssrm 有（builtin_tools 是 ssrm 知识面），
# 其他域如实传 None——enrich 步骤会标注 skipped 而不是硬凑
pipelines = {
    name: HandlePipeline(c, registry if name == "ssrm" else None)
    for name, c in classifiers.items()
}
escalations = EscalationStore(Path(settings.escalation_db))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prompt 版本校验（T3-1）：active 指针、文件、占位符声明三者必须自洽。
    # 放在启动而不是 import：单测 import 不该被 prompts/ 绊倒，但带病启动比起不来更糟
    prompt_versions = prompts.validate()
    providers = ",".join(llm_client.configured_providers) if llm_client.is_configured else "未配置(规则基线降级)"
    logger.info(
        "SSRM Ops Agent 启动",
        extra={
            "event": "app_start", "stage": "boot",
            "provider": providers, "reason": f"budget={settings.request_budget_ms}ms per_try={settings.llm_timeout_per_try_ms}ms prompts={prompt_versions}",
            "outcome": "ok",
        },
    )
    # 建索引是「尽快变好」，不是「不建好就不能服务」：
    # 卡住启动比退回关键词检索严重得多（K8s 探针会直接把 Pod 判死）
    for name, assistant in assistants.items():
        try:
            await asyncio.wait_for(assistant.warmup(), timeout=settings.warmup_timeout_seconds)
        except asyncio.TimeoutError:
            assistant.degrade_to_keyword(f"warmup_timeout>{settings.warmup_timeout_seconds}s")
        logger.info("助手就绪", extra={"event": "rag_ready", "stage": "boot",
                                      "domain": name, "reason": assistant.mode, "outcome": "ok"})
    yield
    # 优雅退出：先关连接池再让进程退出，避免悬挂连接拖慢 shutdown
    await llm_client.aclose()
    for assistant in assistants.values():
        await assistant.aclose()
    escalations.close()
    logger.info("连接池已关闭，进程退出", extra={"event": "app_stop", "stage": "shutdown", "outcome": "ok"})


app = FastAPI(title="SSRM Ops Agent", version="0.1.0", lifespan=lifespan)
# 最外层中间件：发 trace_id、计时、发结构化 access 事件
app.add_middleware(ObservabilityMiddleware)
# 旧版服务端渲染页面：只为直连后端时兜底，线上由 React 前端接管
app.include_router(legacy_pages)


# ── 公网防护：服务会消耗 LLM API 配额，加最小成本的防刷措施 ──
RATE_LIMIT_PER_MIN = 20  # 单 IP 每分钟上限（classify/assist）
_RATE_WINDOW_S = 60
_SWEEP_INTERVAL_S = 300  # 定期清理过期 IP，避免字典随扫描器无限增长

_rate_bucket: dict[str, list[float]] = {}
_last_sweep = 0.0


def _is_trusted_proxy(host: str) -> bool:
    """只有对端是本机/内网时才相信它带来的转发头（防公网直连伪造 X-Real-IP 绕过限速）"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "")
    return ip.is_loopback or ip.is_private


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    部署形态是 nginx → 后端（后端只绑 127.0.0.1），nginx 已注入 X-Real-IP。
    直接用 request.client.host 拿到的是 nginx 容器 IP，会把「单 IP 20/min」
    退化成「全站 20/min」——一个人刷就把所有人限死。
    """
    peer = request.client.host if request.client else ""
    if _is_trusted_proxy(peer):
        xri = (request.headers.get("x-real-ip") or "").strip()
        if xri:
            return xri
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"


def _check_rate(request: Request) -> None:
    """按真实客户端 IP 的内存滑动窗口限速（单实例够用；多实例需换 Redis）"""
    global _last_sweep
    now = time.monotonic()

    if now - _last_sweep > _SWEEP_INTERVAL_S:
        for k in [k for k, v in _rate_bucket.items() if not v or now - v[-1] > _RATE_WINDOW_S]:
            _rate_bucket.pop(k, None)
        _last_sweep = now

    ip = _client_ip(request)
    window = [t for t in _rate_bucket.get(ip, []) if now - t < _RATE_WINDOW_S]
    if len(window) >= RATE_LIMIT_PER_MIN:
        _rate_bucket[ip] = window
        logger.warning(
            "触发限速",
            extra={"event": "rate_limited", "stage": "gate", "client_ip": ip,
                   "reason": f"{len(window)}/{RATE_LIMIT_PER_MIN} per 60s", "outcome": "rejected"},
        )
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    window.append(now)
    _rate_bucket[ip] = window


class ClassifyRequest(BaseModel):
    incident: dict[str, Any]


class ToolAskRequest(BaseModel):
    question: str


class EvalRequest(BaseModel):
    limit: Optional[int] = 30   # 每次评测条数（控制 LLM 成本）


@app.get("/")
async def root():
    return {
        "service": "SSRM Ops Agent",
        "docs": "/docs",
        "endpoints": {
            "POST /classify": "单条事件根因分类（body: {\"incident\": {...}}）",
            "POST /handle": "处置编排：分类 → 只读信息收集 → 确定性处置映射（human_review 自动落升级队列）",
            "GET /escalations": "升级队列（?status=pending|resolved）",
            "POST /escalations/{id}/resolve": "人工决议：批准类别后从中断快照续跑",
            "POST /eval": "数据集评测",
            "GET /stats": "数据集统计",
            "POST /assist": "值班助手问答（body: {\"question\": \"...\"}）",
            "GET /kb/stats": "知识库统计",
            "GET /metrics": "进程内指标快照（含 60 分钟时序与最近请求流水）",
            "GET /traces/{trace_id}": "单次请求的链路（各阶段耗时）",
            "GET /tools": "工具注册表清单",
            "POST /tools/ask": "工具调用 Agent：自然语言 → 选工具 → 校验执行（body: {\"question\": \"...\"}）",
        },
        "pages": {
            "playground": "/playground",
            "assistant": "/assistant",
        },
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "llm_configured": llm_client.is_configured,
        "llm_providers": llm_client.configured_providers,
        "llm_usage": llm_client.usage,   # 进程累计 token 用量，成本可见的最小实现
        "classes": list(ssrm_pack.classes.keys()),
        "assistants": {name: a.mode for name, a in assistants.items()},
        "pipelines": sorted(pipelines),
        "escalations_pending": escalations.pending_count(),
        "prompts": prompts.active_versions(),   # T3-4：当前生效的 prompt 版本，一眼可见
    }


@app.get("/metrics")
async def get_metrics():
    """进程内指标快照：延迟分位数、状态分布、降级率、JSON 失败类型、置信度分布、token。

    不返回 Prometheus 文本格式：当前没有抓取端，JSON 更好读也更好 assert。
    代价写在 docs/optimization/T2-03 的边界里（进程重启归零、多 worker 不聚合）。
    """
    return metrics.snapshot()


@app.post("/classify")
async def classify(req: ClassifyRequest, request: Request):
    _check_rate(request)
    budget = Budget(settings.request_budget_ms)
    with span("classify", budget_ms=settings.request_budget_ms) as s:
        result = await classifier.classify(req.incident, budget)
        s["attrs"]["source"] = result.get("source")
        s["attrs"]["degraded"] = bool(result.get("degraded"))
    result["budget_ms_remaining"] = budget.remaining_ms()
    return attach_obs(result)


@app.post("/eval")
async def eval_endpoint(req: EvalRequest, request: Request):
    """数据集评测：逐条分类并与标注对比，输出总体准确率、分类别准确率、混淆对。

    防刷：一次评测会循环调用 LLM 上限条数次，公网环境要求 token
    （用法：POST /eval?token=... 或请求头 X-Eval-Token；token 配置于 .env 的 SSRM_AIOPS_EVAL_TOKEN）。
    """
    token = request.query_params.get("token") or request.headers.get("x-eval-token")
    if not settings.eval_token or token != settings.eval_token:
        raise HTTPException(status_code=403, detail="评测接口需要有效 token（防公网刷量消耗 LLM 配额）")
    _check_rate(request)
    rows = load_dataset(limit=req.limit, shuffle=True)
    correct = 0
    per_class: dict[str, dict[str, int]] = {}
    confusions: list[dict[str, str]] = []
    latencies: list[float] = []
    sources: dict[str, int] = {}

    for row in rows:
        t0 = time.monotonic()
        result = await classifier.classify(row["incident"], Budget(settings.request_budget_ms))
        latencies.append((time.monotonic() - t0) * 1000)
        sources[result.get("source", "?")] = sources.get(result.get("source", "?"), 0) + 1

        pred = result.get("root_cause_class")
        label = row["label"]
        per_class.setdefault(label, {"total": 0, "correct": 0})
        per_class[label]["total"] += 1
        if pred == label:
            correct += 1
            per_class[label]["correct"] += 1
        else:
            confusions.append({"label": label, "predicted": pred, "incident_id": row["incident"]["incident_id"]})

    total = len(rows)
    return {
        "total": total,
        "accuracy": round(correct / total, 4) if total else 0,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "source_channels": sources,
        "per_class": {
            k: {"accuracy": round(v["correct"] / v["total"], 3), **v}
            for k, v in per_class.items()
        },
        "confusions": confusions[:10],
    }


@app.get("/stats")
async def dataset_stats():
    return stats()


class AssistRequest(BaseModel):
    question: str
    # 助手领域：ssrm=值班助手（默认，向后兼容）/ csa=坐席助手 / credit=催员辅助
    domain: str = "ssrm"


@app.post("/assist")
async def assist(req: AssistRequest, request: Request):
    """领域助手：描述你遇到的问题，返回相似历史案例和建议。

    同一个引擎、三份领域知识：ssrm=运维值班 / csa=电商坐席 / credit=信贷催员辅助。
    """
    assistant = assistants.get(req.domain)
    if assistant is None:
        raise HTTPException(status_code=404, detail=f"领域 {req.domain!r} 没有助手场景")
    _check_rate(request)
    budget = Budget(settings.request_budget_ms)
    with span("assist", budget_ms=settings.request_budget_ms, domain=req.domain) as s:
        result = await assistant.assist(req.question, budget)
        s["attrs"]["mode"] = result.get("mode")
        s["attrs"]["degraded"] = "error" in result
    return attach_obs(result)


class HandleRequest(BaseModel):
    incident: dict[str, Any]
    domain: str = "ssrm"


class ResolveRequest(BaseModel):
    approved_class: str
    note: str = ""


@app.post("/handle")
async def handle(req: HandleRequest, request: Request):
    """处置编排（T5）：分类 → 只读信息收集 → 确定性处置映射。

    - auto_dispose：高置信 + 证据过门禁，按 suggested_action 自动处置
    - human_review：低置信/证据拦截 → 中断快照落升级队列，等人工决议续跑
    - escalate：unknown，无处置建议转人工兜底
    """
    pipeline = pipelines.get(req.domain)
    if pipeline is None:
        raise HTTPException(status_code=404, detail=f"领域 {req.domain!r} 未注册编排管线")
    _check_rate(request)
    budget = Budget(settings.request_budget_ms)
    with span("handle", budget_ms=settings.request_budget_ms, domain=req.domain) as s:
        result = await pipeline.run(req.incident, budget)
        s["attrs"]["disposition"] = result.get("disposition")
        s["attrs"]["steps"] = len(result.get("pipeline", []))
    if result.get("disposition") == "human_review":
        eid = escalations.add(req.domain, result)
        result["escalation_id"] = eid
        logger.info("升级队列新增待办",
                    extra={"event": "escalation_created", "stage": "pipeline",
                           "domain": req.domain, "reason": result.get("disposition_reason", "")[:100],
                           "outcome": "ok"})
    return attach_obs(result)


@app.get("/escalations")
async def list_escalations(status: str = "pending", limit: int = 20):
    """升级队列：人工接管的待办清单（pending）或已决议记录（resolved）。"""
    if status not in ("pending", "resolved"):
        raise HTTPException(status_code=422, detail="status 只支持 pending / resolved")
    return {"pending_count": escalations.pending_count(),
            "items": escalations.list(status=status, limit=limit)}


@app.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: int, req: ResolveRequest, request: Request):
    """人工决议：批准类别后从中断快照续跑 decide（不重新分类——人已裁决）。"""
    _check_rate(request)
    record = escalations.get(escalation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"升级记录 {escalation_id} 不存在")
    if record["status"] != "pending":
        raise HTTPException(status_code=409, detail="该记录已决议（审计线索不可覆盖）")
    pack = packs.get(record["domain"])
    if pack is None or req.approved_class not in pack.classes:
        raise HTTPException(status_code=422,
                            detail=f"approved_class 必须是 {record['domain']} 域的合法类别: {list(pack.classes) if pack else '域不存在'}")
    pipeline = pipelines[record["domain"]]
    result = pipeline.resume(record["state"], req.approved_class, req.note)
    escalations.resolve(escalation_id, f"{req.approved_class}（{req.note or '无备注'}）")
    result["escalation_id"] = escalation_id
    return result


@app.get("/tools")
async def list_tools():
    """工具注册表清单：LLM 能看到什么，这里原样暴露什么（handler 不暴露）"""
    return {"tools": registry.specs()}


@app.post("/tools/ask")
async def tools_ask(req: ToolAskRequest, request: Request):
    """工具调用 Agent：自然语言 → LLM 选工具+参数 → 白名单/schema/置信度三道关 → 执行"""
    _check_rate(request)
    with span("tools.ask", budget_ms=settings.request_budget_ms):
        return await choose_and_run(
            llm_client, registry, req.question, budget=Budget(settings.request_budget_ms)
        )


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """按 trace_id 取单次请求的链路（各阶段耗时）。

    只保留最近 100 条：这里不是追踪系统，是「刚才那条慢请求到底慢在哪」的现场取证。
    真要做跨天检索得上 OTel + 后端存储，那是 T7 之后的事。
    """
    record = traces.get(trace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="链路已滚出窗口（只保留最近 100 条）或 trace_id 不存在")
    return record


@app.get("/kb/stats")
async def kb_stats():
    from collections import Counter
    c = Counter(d["root_cause_class"] for d in rag.docs)
    return {"tickets": len(rag.docs), "by_class": dict(c), "mode": rag.mode, "ready": rag.ready}
