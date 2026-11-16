"""OTel 协议出口（T2-07）：W3C traceparent 传播 + OTLP/HTTP 导出

T2-05 之后本项目的 trace 能跨重启回查，但仍然只服务自己：数据在自己的 SQLite 里，
格式是自己的（name/start_ms/depth/parent）。这带来两个边界：

1. **送不出去**：客户已有 Jaeger/Tempo/签表，不会为了看我的链路再装一套 UI；
2. **接不进来**：业务 Agent 是别的团队写的，他们的 trace_id 怎么和我的对上？

OTel 解决的就是这两件事，而它的对接点是**协议**而不是某个库：
- 数据模型：Trace = 一棵 Span 树（traceId/spanId/parentSpanId/name/kind/起止/attributes/status）
- 传播协议：W3C `traceparent: 00-<32hex>-<16hex>-<flags>`
- 语义约定：属性命名（`gen_ai.*` / `http.*`）

所以本模块不引 `opentelemetry-sdk`，自己做这两层。为什么：

- 环境限制只是次要原因（pip 被安全策略拦），判断本身也不支持引：SDK 的核心产出
  （span 树 + 属性 + 导出）我已有；它多给的是**库级自动埋点**，而本项目的链路里
  只有一个外部调用（LLM）且已经手工包了 `llm.try`，自动埋点几乎没有增量。
- 出口是标准协议，所以将来换 SDK 时**换的是实现，出口不变**——这是分层的收益。

诚实的边界：这是「协议兼容」，不是「原生 SDK 集成」。原生能拿到的 Baggage、
Span Links、Metrics 关联、库级埋点，这里都没有。见 docs/optimization/T2-07。

设计要点：
1. **双写**：SQLite 给自己看（前端瀑布图），OTLP 给别人看（Jaeger/Collector）。
2. **导入不阻塞**：中间件只做 enqueue（纯内存、微秒级），网络 IO 交给后台 task。
   观测系统自己挂掉绝不能拖死业务。
3. **有界队列 + 丢弃**：collector 挂了时无界队列会把内存吃干，满了丢最老的并计数。
4. **失败不重试**：trace 价值随时间衰减，重传还会在 collector 挂时形成堆积。
   宁可丢，不可堵——这与「业务请求必须重试」是相反策略，因为可靠性要求不同。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("app.otel")

# W3C Trace Context：version-traceid-spanid-flags，全小写 hex
_TRACEPARENT_RE = re.compile(
    r"^(?P<ver>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

SCOPE_NAME = "ssrm-aiops.observability"
SCOPE_VERSION = "0.1.0"


# ── trace_id 映射：本地 16 hex ↔ OTel 32 hex ──
def normalize_trace_id(trace_id: str) -> str:
    """任意长度的 hex trace_id → 32 位。左补零而不是重复拼接：

    `tid * 2` 会让 32 位里出现重复模式，某些后端的去重/哈希会撞；
    补零是单射，且 lstrip("0") 可逆。
    """
    tid = (trace_id or "").strip().lower()
    if not tid or not _HEX_RE.match(tid):
        return uuid.uuid4().hex
    if len(tid) >= 32:
        return tid[:32]
    return tid.rjust(32, "0")


def to_local_trace_id(trace_id32: str) -> str:
    """32 位 → 本地的 16 位展示格式。

    注意这是**有损**的（128bit → 64bit）。所以跨服务链路不能靠它：
    本地 id 只是人看的短号，与外部对齐一律用 `record["otel_trace_id"]`
    保存的原始 32 位（见 obs.py 中间件）。若拿它回写 traceparent，
    上游给的 traceId 会变成另一个值，链路就断了。
    """
    tid = (trace_id32 or "").strip().lower()
    if not _HEX_RE.match(tid or ""):
        return uuid.uuid4().hex[:16]
    local = tid.lstrip("0")
    if not local:
        return uuid.uuid4().hex[:16]
    # 超过 16 位时取尾部：本地 schema 就是这个长度，取前 16 位会丢掉随机性
    return local[-16:]


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


# ── W3C traceparent ──
def parse_traceparent(value: Optional[str]) -> Optional[dict[str, Any]]:
    """解析入站 `traceparent`。非法/缺失一律返回 None（不抛异常）。

    拒绝的情形都对应规范里的硬约束：长度不符、非 hex、traceId 全零、spanId 全零。
    version=ff 是规范保留值，也拒掉。
    """
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip().lower())
    if not m:
        return None
    if m.group("ver") == "ff":
        return None
    if m.group("trace") == _ZERO_TRACE_ID or m.group("span") == _ZERO_SPAN_ID:
        return None
    return {
        "trace_id": m.group("trace"),
        "span_id": m.group("span"),
        "sampled": bool(int(m.group("flags"), 16) & 0x01),
    }


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """生成本进程的出站 `traceparent`（用于响应头，让下游继续串）"""
    return f"00-{normalize_trace_id(trace_id)}-{span_id or new_span_id()}-{'01' if sampled else '00'}"


# ── 采样：按 trace_id 哈希，不是按请求随机 ──
def is_sampled_out(trace_id: str, ratio: float) -> bool:
    """ratio<1 时判断是否丢弃。按 trace_id 做确定性哈希，保证同一条 trace 的
    所有 span 同采同丢——否则后端会出现「有子 span 没有父 span」的碎链。
    """
    if ratio >= 1.0:
        return False
    if ratio <= 0.0:
        return True
    bucket = int(normalize_trace_id(trace_id)[:8], 16) / 0xFFFFFFFF
    return bucket >= ratio


# ── OTLP 属性编码 ──
def _attr_value(value: Any) -> Optional[dict[str, Any]]:
    """Python 值 → OTLP JSON 的 tagged union。

    两个坑：
    - 64 位整数在 JSON 里必须写成**字符串**（JSON number 只有 53 位精度），
      所以 int 一律走 intValue/字符串；
    - bool 是 int 的子类，必须先判 bool 再判 int，否则 True 会变成 intValue "1"。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    # dict / list 展开成 JSON 字符串：OTLP 的 arrayValue 只支持同构标量，
    # 嵌套对象展开容易出错，而 attrs 里出现的嵌套结构都是给人看的
    try:
        return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}
    except (TypeError, ValueError):
        return {"stringValue": str(value)[:1000]}


def _attributes(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, v in (attrs or {}).items():
        encoded = _attr_value(v)
        if encoded is not None:
            out.append({"key": str(k), "value": encoded})
    return out


# ── gen_ai 语义约定 ──
# 只加别名、不改名：语义约定让别人的看板能直接查（gen_ai.* 是通用词），
# 原名保留让自己前端与历史查询不受影响。改名等于让两个月前的 SQL 全部失效。
_GENAI_SPANS = ("llm.try", "llm.call", "rag.answer", "rag.rerank")


def _genai_aliases(span_name: str, attrs: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if span_name in _GENAI_SPANS:
        extra["gen_ai.operation.name"] = "chat"
    if attrs.get("provider"):
        extra["gen_ai.system"] = attrs["provider"]
    if attrs.get("model"):
        extra["gen_ai.request.model"] = attrs["model"]
    # 请求级 token 记在 trace 上而不是 span 上，导出时由调用方补进来
    if attrs.get("prompt_tokens") is not None:
        extra["gen_ai.usage.input_tokens"] = attrs["prompt_tokens"]
    if attrs.get("completion_tokens") is not None:
        extra["gen_ai.usage.output_tokens"] = attrs["completion_tokens"]
    return extra


def _otlp_status(span: dict[str, Any]) -> dict[str, Any]:
    """OTel 的 Status：0=UNSET 1=OK 2=ERROR。自带 message 只在 ERROR 时有意义。"""
    if span.get("status") == "error":
        msg = (span.get("attrs") or {}).get("error")
        status: dict[str, Any] = {"code": 2}
        if msg:
            status["message"] = str(msg)[:300]
        return status
    return {"code": 1}


def build_payload(
    records: list[dict[str, Any]],
    service_name: str = "ssrm-aiops",
    extra_resource: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把若干条自研 trace record 转成一份 OTLP/JSON 请求体（纯函数，可单测）。

    时间换算：record["ts_epoch"] 是**中间件出口**的时刻，所以请求起点要减掉总耗时；
    每个 span 的 start_ms 是相对请求起点的毫秒偏移。
    """
    spans_out: list[dict[str, Any]] = []
    for rec in records:
        spans = rec.get("spans") or []
        if not spans:
            continue
        total_ms = float(rec.get("duration_ms") or 0.0)
        end_epoch = float(rec.get("ts_epoch") or 0.0)
        if not end_epoch:
            continue
        start_epoch = end_epoch - total_ms / 1000.0
        # 优先用 otel_trace_id：那是上游 traceparent 里的原始 32 位，与外部对齐。
        # 退回 normalize(local) 只适用于「没有上游、由本进程新建」的情况——
        # 别用 to_local_trace_id 的结果反推，16 位是截断过的，反推出来是另一个 id。
        trace_id = rec.get("otel_trace_id") or normalize_trace_id(rec.get("trace_id", ""))
        usage = rec.get("usage") or {}

        # 根 span：自研版是中间件手工构造的 "http"，且业务 span 的 parent 为空
        # （begin_spans() 没预置根 span），它们在数据上是兄弟。
        # OTLP 里一条 trace 应当是一棵树，所以这里把无父 span 挂到根 span 下，
        # 否则 Jaeger 会画成 5 条并列的根链，看着像 5 次独立请求。
        root = next((s for s in spans if s.get("name") == "http"), None)
        root_span_id = (root or {}).get("span_id") or new_span_id()

        for s in spans:
            name = s.get("name", "-")
            start_ms = float(s.get("start_ms") or 0.0)
            dur_ms = float(s.get("duration_ms") or 0.0)
            span_id = s.get("span_id") or new_span_id()
            parent_id = s.get("parent_span_id")
            if not parent_id and span_id != root_span_id:
                parent_id = root_span_id

            attrs = dict(s.get("attrs") or {})
            if name == "http":
                # 请求级 usage 挂在根 span 上：这样「这次请求花了多少 token」
                # 在 Jaeger 里点开根节点就能看到，不用翻 span 列表
                if usage.get("total_tokens"):
                    attrs.setdefault("gen_ai.usage.total_tokens", usage["total_tokens"])
                if rec.get("path"):
                    attrs.setdefault("url.path", rec["path"])
                if rec.get("method"):
                    attrs.setdefault("http.request.method", rec["method"])
                if rec.get("status"):
                    attrs.setdefault("http.response.status_code", rec["status"])
            attrs.update(_genai_aliases(name, attrs))

            start_ns = int((start_epoch + start_ms / 1000.0) * 1_000_000_000)
            span_json: dict[str, Any] = {
                "traceId": trace_id,
                "spanId": span_id,
                "name": name,
                "kind": 2 if name == "http" else 1,  # 2=SERVER 1=INTERNAL
                "startTimeUnixNano": str(start_ns),
                "endTimeUnixNano": str(start_ns + int(dur_ms * 1_000_000)),
                "attributes": _attributes(attrs),
                "status": _otlp_status(s),
            }
            if parent_id:
                span_json["parentSpanId"] = parent_id
            spans_out.append(span_json)

    resource_attrs = {
        "service.name": service_name,
        "telemetry.sdk.name": "ssrm-aiops-otlp",
        "telemetry.sdk.language": "python",
    }
    resource_attrs.update(extra_resource or {})

    return {
        "resourceSpans": [{
            "resource": {"attributes": _attributes(resource_attrs)},
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                "spans": spans_out,
            }],
        }],
    }


# ── 导出器 ──
class OtlpExporter:
    """OTLP/HTTP 批量导出。线程/协程使用约定：

    - `enqueue()` 只允许在事件循环里调用（中间件 finally 处），纯内存操作；
    - 网络 IO 全在 `_loop()` 的后台 task 里，失败只记日志不重试。
    """

    def __init__(
        self,
        endpoint: str,
        service_name: str = "ssrm-aiops",
        timeout_ms: int = 3000,
        batch_size: int = 32,
        flush_interval_s: float = 5.0,
        max_queue: int = 2000,
        sample_ratio: float = 1.0,
    ):
        self._url = endpoint.rstrip("/") + "/v1/traces"
        self._service = service_name
        self._timeout = max(1, timeout_ms) / 1000.0
        self._batch = max(1, batch_size)
        self._interval = max(0.2, flush_interval_s)
        self._queue: deque[dict[str, Any]] = deque(maxlen=max(1, max_queue))
        self._sample_ratio = sample_ratio
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._closing = False
        self.sent = 0          # 成功导出的 span 数
        self.failed = 0        # 失败的批次
        self.dropped = 0       # 队列满或采样丢弃的 trace 数

    # ── 生命周期 ──
    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=2.0))
        self._task = asyncio.create_task(self._loop(), name="otlp-exporter")

    async def stop(self) -> None:
        """停机时把队列里剩的尽量发一次：进程退出前的最后几条往往正是要复盘的那批"""
        self._closing = True
        if self._task is not None:
            self._wake.set()
            try:
                await asyncio.wait_for(self._task, timeout=self._timeout + 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._client is not None:
            try:
                await self.flush()
            finally:
                await self._client.aclose()
                self._client = None

    # ── 入队 ──
    def enqueue(self, record: dict[str, Any]) -> None:
        # 只判 closing 不判 _task：单例只在 start 成功后才会被 export_trace 看到，
        # 而「已停止」是真实的边界（停机过程中还有请求在收尾，不再往队列里塞）
        if self._closing:
            return
        if is_sampled_out(record.get("trace_id", ""), self._sample_ratio):
            self.dropped += 1
            return
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1  # deque 会自动挤掉最老的，这里只负责记账
        self._queue.append(record)
        try:
            self._wake.set()
        except RuntimeError:
            # 无运行中的事件循环（极端情况：解释器收尾阶段）：放弃唤醒，下个周期自会捞
            pass

    # ── 后台循环 ──
    async def _loop(self) -> None:
        while not self._closing:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            self._wake.clear()
            await self.flush()

    async def flush(self) -> int:
        """把当前队列里最多 batch_size 条发出去，返回成功导出的 span 数"""
        if self._client is None or not self._queue:
            return 0
        records = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
        payload = build_payload(records, self._service)
        span_count = len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
        if span_count == 0:
            return 0
        try:
            resp = await self._client.post(
                self._url, content=json.dumps(payload), headers={"Content-Type": "application/json"}
            )
            if 200 <= resp.status_code < 300:
                self.sent += span_count
                return span_count
            self.failed += 1
            reason = f"http {resp.status_code}: {resp.text[:120]}"
        except Exception as e:  # 网络异常、超时：trace 不重试，只记账
            self.failed += 1
            reason = f"{type(e).__name__}: {str(e)[:120]}"
        logger.warning(
            "OTLP 导出失败（不影响主请求）",
            extra={"event": "otlp_export_fail", "stage": "export",
                   "reason": reason, "outcome": "fail", "count": span_count},
        )
        return 0

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self._task is not None,
            "endpoint": self._url,
            "service": self._service,
            "queue": len(self._queue),
            "queue_max": self._queue.maxlen,
            "sent_spans": self.sent,
            "failed_batches": self.failed,
            "dropped_traces": self.dropped,
            "sample_ratio": self._sample_ratio,
        }


# ── 模块级单例（与 trace_store 同一套懒加载约定）──
_exporter: Optional[OtlpExporter] = None


async def start_otel() -> None:
    """lifespan 启动时调用。otel_enabled=false 时直接返回（默认关闭，因为本地没 collector）"""
    global _exporter
    if not settings.otel_enabled or _exporter is not None:
        return
    _exporter = OtlpExporter(
        endpoint=settings.otel_endpoint,
        service_name=settings.otel_service_name,
        timeout_ms=settings.otel_timeout_ms,
        batch_size=settings.otel_batch_size,
        flush_interval_s=settings.otel_flush_interval_ms / 1000.0,
        max_queue=settings.otel_queue_max,
        sample_ratio=settings.otel_sample_ratio,
    )
    await _exporter.start()
    logger.info(
        "OTLP 导出已启用",
        extra={"event": "otel_start", "stage": "boot",
               "reason": f"{settings.otel_endpoint} service={settings.otel_service_name}",
               "outcome": "ok"},
    )


async def stop_otel() -> None:
    global _exporter
    if _exporter is not None:
        await _exporter.stop()
        _exporter = None


def get_exporter() -> Optional[OtlpExporter]:
    return _exporter


def export_trace(record: dict[str, Any]) -> None:
    """中间件调用点。未启用时是空操作——所以调用方不用判空"""
    if _exporter is not None:
        _exporter.enqueue(record)


def otel_stats() -> dict[str, Any]:
    if _exporter is None:
        return {"enabled": False}
    return _exporter.stats()
