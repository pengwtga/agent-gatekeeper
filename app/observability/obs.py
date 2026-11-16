"""可观测基础件：trace_id 上下文 + 结构化 JSON 日志 + 请求级 token 累加器（T2-1 / T2-2 / T2-4 的数据底座）

三个东西放在一个模块，因为它们共用同一套上下文：
1. `trace_id`：contextvar 存，一个请求一个；中间件生成并写回响应头
2. JSON 日志：formatter 从 contextvar 取 trace_id 自动塞进每条日志，业务代码不用传参
3. 请求级 token 累加器：同样走 contextvar，LLM 客户端埋在深处也能记到当前请求账上

为什么用 contextvar 而不是把 trace_id 当函数参数一路透传：
Agent 的调用链是 路由 → 分类器 → LLM 客户端 → httpx，中间还夹着重试与容灾循环。
逐层加参数会让签名全面变脏，而且漏传一处就断链。contextvar 由 asyncio 的
任务上下文隔离，天然是「当前请求作用域」，并发请求之间不会串。
"""

import asyncio
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from typing import Any, Optional

from app.config import settings
from app.observability.otel import (
    export_trace,
    format_traceparent,
    is_sampled_out,
    new_span_id,
    normalize_trace_id,
    parse_traceparent,
    to_local_trace_id,
)
from app.observability.trace_store import get_trace_store
from app.observability.tracing import begin_spans, collect_spans, traces

# ── 上下文变量 ──
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_request_usage: ContextVar[Optional[dict[str, int]]] = ContextVar("request_usage", default=None)

_TRACE_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")  # 只收安全字符，防日志注入/响应头注入


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(tid: str):
    return _trace_id.set(tid)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


def current_trace_id() -> str:
    return _trace_id.get()


def pick_trace_id(incoming: Optional[str], tp_ctx: Optional[dict[str, Any]] = None) -> str:
    """复用上游 trace_id（便于跨服务串联），非法或缺失则新建。

    不信任任意长度的外部输入：超长/带换行的 header 直接进日志会污染日志行，
    进响应头更是 header 注入。所以只接受白名单字符且长度受限。

    优先级：自家 `x-trace-id` > W3C `traceparent`（T2-07）> 新建。
    前者优先是因为它就是我们自己的格式，不用做长度换算。
    """
    if incoming and _TRACE_RE.match(incoming):
        return incoming
    if tp_ctx:
        return to_local_trace_id(tp_ctx["trace_id"])
    return new_trace_id()


# ── 请求级 token 累计（T2-4）──
def begin_request_usage() -> None:
    _request_usage.set({"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})


def add_request_usage(prompt_tokens: int, completion_tokens: int) -> None:
    acc = _request_usage.get()
    if acc is None:
        return
    acc["prompt_tokens"] += prompt_tokens
    acc["completion_tokens"] += completion_tokens
    acc["calls"] += 1


def current_request_usage() -> dict[str, int]:
    acc = _request_usage.get() or {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    total = acc["prompt_tokens"] + acc["completion_tokens"]
    return {
        "prompt_tokens": acc["prompt_tokens"],
        "completion_tokens": acc["completion_tokens"],
        "total_tokens": total,
        "calls": acc["calls"],
    }


# ── 结构化日志 ──
# 固定字段壳：ts / level / logger / trace_id / event，其余按需附加。
# 白名单而不是「把 extra 全倒进去」：extra 里混进不可 JSON 序列化的对象会让
# 整个日志行写失败（logging 的默认异常处理是静默吞掉），排查时最恨这种。
_ALLOWED_EXTRA = (
    "stage", "provider", "model", "duration_ms", "tokens", "outcome",
    "method", "path", "status", "attempt", "reason", "score", "root_cause_class",
    "source", "client_ip", "degraded", "count",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "trace_id": current_trace_id(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
            payload["msg"] = record.getMessage()
        else:
            # 没写 event 的老代码：整条消息当 event，不丢信息
            payload["event"] = record.getMessage()

        for key in _ALLOWED_EXTRA:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).strip().splitlines()[-1][:300]
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """一次性替换 root handler。

    同时把 uvicorn 的三个 logger 挂到 root 上（它们默认 propagate=False + 自带
    handler，不处理的话容器里会混着两份格式：JSON 业务日志 + 纯文本 access log，
    `docker logs | jq` 直接断在 access log 那行）。
    access log 由中间件自己发结构化事件，所以容器启动参数用 --no-access-log 关掉。
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    # uvicorn 的 --no-access-log 只是把 uvicorn.access 的 handler 清空，
    # 而下面一行如果给它 propagate=True，记录会重新冒到 root —— 于是容器日志里
    # 又出现一份纯文本 access log，和中间的 http_request 结构化事件重复。
    # 这个坑只有「容器里看实际输出」才能发现（本机 jq 不在时更明显）。
    # access 由 ObservabilityMiddleware 统一发，这里直接禁用该 logger。
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True

    # httpx 每次请求都打一条 INFO（含完整 URL），和我们的 llm_call 事件重复，
    # 而且会把 provider 域名重复写进日志。降到 WARNING 只留真正的连接错误。
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ── 纯 ASGI 中间件 ──
class ObservabilityMiddleware:
    """一个中间件同时干三件事：发 trace_id、计时、发结构化 access 事件。

    用纯 ASGI 而不是 BaseHTTPMiddleware：后者会把响应包一层 anyio 的流式任务，
    trace_id 的上下文在流式响应里可能丢失，且会吃掉 StreamingResponse 的首字节
    延迟（以后加 SSE 就踩坑）。纯 ASGI 只是包一层 send，零缓冲。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 延迟 import：metrics 依赖本模块的 current_trace_id，避免循环导入
        from app.observability.metrics import metrics

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        tp_ctx = parse_traceparent(headers.get("traceparent"))
        tid = pick_trace_id(
            headers.get("x-trace-id") or headers.get("x-request-id"), tp_ctx
        )
        # 与外部对齐的 32 位 id：上游给的原始值，或由本地 id 补零得来。
        # 本地 tid 是 16 位（人看的短号），拿它反推 traceparent 会得到另一个值，链路就断了，
        # 所以这两个 id 必须分开存：tid 用于日志/DB/前端，otel_tid 用于导出与响应头。
        otel_tid = (tp_ctx or {}).get("trace_id") or normalize_trace_id(tid)
        token = set_trace_id(tid)
        begin_request_usage()
        begin_spans()

        # 根 span 的 ID 在入口就生成：响应头要在 http.response.start 时就写上
        # traceparent（晚于这个时机就拿不到 header 了），而落库的 record 里
        # 根 span 必须用同一个 ID，否则导出时父子对不上。
        root_span_id = new_span_id()
        sampled = not (settings.otel_enabled and is_sampled_out(otel_tid, settings.otel_sample_ratio))

        path = scope.get("path", "-")
        method = scope.get("method", "-")
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                raw = list(message.get("headers") or [])
                raw.append((b"x-trace-id", tid.encode()))
                # T2-07：回写 W3C traceparent，下游（或前端 RUM）能继续串成一条链
                raw.append((b"traceparent", format_traceparent(otel_tid, root_span_id, sampled).encode()))
                message["headers"] = raw
            await send(message)

        error: Optional[BaseException] = None
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as e:  # 含 CancelledError（优雅退出），都要留痕
            error = e
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            # 健康检查每 30s 一次，混进延迟分位数会把 P95 拉向 1ms，指标就废了：
            # 计数照记，分位数窗口排除
            # 观测类请求不进指标也不进流水：
            # - /health、/metrics 是探针，高频且永远 200，会把分位数和错误率稀释掉；
            # - /traces/* 是「看流量的流量」，一次排查会往流水里插一条自己，
            #   列表看着像自己引用自己，值班时反而得多想一层
            # 「看」类端点不进业务流水：探针、指标快照、链路查询、元数据统计。
            # 它们的共同点是只服务于观察动作，混进流水会把真实请求稀释掉
            # （最典型：点开一条链路，列表第一条变成「查这条链路的那次请求」）
            counted = not (
                path in ("/health", "/metrics", "/stats", "/kb/stats", "/otel/stats")
                # "/traces" 同时覆盖详情与列表：看流量的请求不该混进流水/落库
                or path == "/traces" or path.startswith("/traces/")
            )
            outcome = "error" if error is not None else (
                "ok" if status < 400 else ("client_error" if status < 500 else "server_error")
            )
            # usage 要在 record_request 之前取：请求流水行要带上本次请求烧掉的 token，
            # 这样面板上「哪条请求贵」不用再回日志里对 trace_id
            usage = current_request_usage()
            metrics.record_request(
                path=path, method=method, status=status,
                duration_ms=duration_ms, outcome=outcome, counted=counted,
                trace_id=tid, tokens=usage["total_tokens"] if usage["calls"] else 0,
            )
            # 链路只存业务请求：探针没有 span，存进来只会把 100 条窗口占满。
            # 根 span「http」是必须的：业务 span 的起点通常在 3~5ms 之后
            # （ASGI 解析、Pydantic 校验都在它前面），没有根 span 做基准，
            # 瀑布图上业务阶段会全挤在右侧一小条，看着像画错了。
            child_spans = collect_spans()
            if counted and child_spans:
                record = {
                    "trace_id": tid,
                    # 32 位、与外部对齐的 id（导出与溯源用）；不落库，只跟着 record 走一程
                    "otel_trace_id": otel_tid,
                    "ts": time.strftime("%H:%M:%S", time.localtime()),
                    # 内存版用 "HH:MM:SS" 展示；落库要能跨天排序与算保留期，所以另存 epoch
                    "ts_epoch": time.time(),
                    "method": method, "path": path, "status": status,
                    "duration_ms": duration_ms, "outcome": outcome,
                    "usage": usage,
                    "spans": [{
                        "name": "http", "start_ms": 0.0, "duration_ms": duration_ms,
                        "status": "error" if error is not None else "ok",
                        # 根 span 是手工构造的（中间件里不好用 with），
                        # depth/parent 要跟 tracing.span() 产出的字段保持一致，
                        # 否则前端拿 s.depth 会拿到 undefined
                        "depth": 0, "parent": None,
                        "span_id": root_span_id, "parent_span_id": None,
                        "attrs": {"method": method, "path": path, "status": status},
                    }] + child_spans,
                }
                traces.put(tid, record)
                # 落库走线程池：SQLite 写 + commit 约 1~3ms，且持连接锁，
                # 直接在事件循环里做会拖慢并发请求。观测是旁路——写失败只记日志，
                # 绝不能让 200 变 500（可观测系统的第一原则）。
                if settings.trace_persist:
                    try:
                        await asyncio.to_thread(get_trace_store().put, record)
                    except Exception as e:
                        logger = logging.getLogger("app.trace_store")
                        logger.warning(
                            "trace 落库失败（不影响主请求）",
                            extra={
                                "event": "trace_persist_fail", "stage": "storage",
                                "reason": f"{type(e).__name__}: {str(e)[:180]}",
                                "outcome": "fail",
                            },
                        )
                # T2-07：同一条 record 再送一程到 OTLP（双写）。
                # enqueue 只做入队，网络 IO 在后台 task 里；未启用时是空操作。
                export_trace(record)
            logger = logging.getLogger("app.access")
            logger.info(
                "%s %s -> %s", method, path, status,
                extra={
                    "event": "http_request",
                    "method": method, "path": path, "status": status,
                    "duration_ms": duration_ms, "outcome": outcome,
                    "tokens": usage["total_tokens"] if usage["calls"] else None,
                    "degraded": True if status >= 500 else None,
                },
            )
            reset_trace_id(token)


def attach_obs(payload: dict[str, Any]) -> dict[str, Any]:
    """给响应体挂上 trace_id 与本次请求的 token 用量（T2-4）。

    响应头已有 X-Trace-Id，这里再放一份是因为前端报错截图时更容易带上；
    两者同源，不存在不一致。
    """
    payload["trace_id"] = current_trace_id()
    payload["usage"] = current_request_usage()
    return payload
