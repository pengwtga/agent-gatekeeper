"""OTel 协议出口（T2-07）单测：traceparent 往返、trace_id 映射、OTLP 载荷、失败隔离

全部零依赖、零网络：导出器用假 client，不启真 collector。
"""

import asyncio
import json

import pytest

from app.observability import otel
from app.observability.otel import (
    OtlpExporter,
    build_payload,
    format_traceparent,
    is_sampled_out,
    normalize_trace_id,
    parse_traceparent,
    to_local_trace_id,
)

TRACE32 = "4bf92f3577b34da6a3ce929d0e0e4736"  # W3C 规范示例
SPAN16 = "00f067aa0ba902b7"


def _record(trace_id="abc123def4567890", spans=None, usage=None):
    return {
        "trace_id": trace_id,
        "ts_epoch": 1_700_000_000.0,
        "method": "POST", "path": "/assist", "status": 200,
        "duration_ms": 1000.0, "outcome": "ok",
        "usage": usage if usage is not None else {
            "prompt_tokens": 400, "completion_tokens": 200, "total_tokens": 600, "calls": 2,
        },
        "spans": spans if spans is not None else [
            {"name": "http", "start_ms": 0.0, "duration_ms": 1000.0, "status": "ok",
             "depth": 0, "parent": None, "span_id": "aaaa000000000001", "parent_span_id": None,
             "attrs": {"method": "POST", "path": "/assist", "status": 200}},
            {"name": "rag.answer", "start_ms": 5.0, "duration_ms": 900.0, "status": "ok",
             "depth": 0, "parent": None, "span_id": "aaaa000000000002", "parent_span_id": None,
             "attrs": {"provider": "zhipu", "model": "glm-4-flash"}},
            {"name": "llm.try", "start_ms": 6.0, "duration_ms": 800.0, "status": "ok",
             "depth": 1, "parent": "rag.answer",
             "span_id": "aaaa000000000003", "parent_span_id": "aaaa000000000002",
             "attrs": {"provider": "zhipu", "attempt": 1, "timeout_ms": 7000}},
        ],
    }


# ── traceparent ──
def test_parse_traceparent_ok():
    ctx = parse_traceparent(f"00-{TRACE32}-{SPAN16}-01")
    assert ctx == {"trace_id": TRACE32, "span_id": SPAN16, "sampled": True}


def test_parse_traceparent_rejects_bad_input():
    bad = [
        None, "", "00-abc-123-01",                      # 长度不符
        f"00-{TRACE32}-{SPAN16}-0g",                    # 非 hex
        f"ff-{TRACE32}-{SPAN16}-01",                    # version=ff 是保留值
        f"00-{'0' * 32}-{SPAN16}-01",                   # traceId 全零
        f"00-{TRACE32}-{'0' * 16}-01",                  # spanId 全零
    ]
    for v in bad:
        assert parse_traceparent(v) is None, v


def test_parse_traceparent_unsampled_flag():
    assert parse_traceparent(f"00-{TRACE32}-{SPAN16}-00")["sampled"] is False


def test_format_traceparent_keeps_upstream_id():
    """上游给的 32 位 id 必须原样回写：一旦被截断再补零，就变成另一个 id，链路就断了"""
    header = format_traceparent(TRACE32, SPAN16)
    assert parse_traceparent(header)["trace_id"] == TRACE32
    # 本地 16 位短号 → 32 位 → 回到 16 位，可逆
    assert to_local_trace_id(normalize_trace_id("abc123def4567890")) == "abc123def4567890"


# ── trace_id 映射 ──
def test_trace_id_pad_and_strip():
    assert normalize_trace_id("abc") == "0" * 29 + "abc"
    assert to_local_trace_id("0" * 29 + "abc") == "abc"
    # 已经是 32 位的原样返回（前端若直接传 OTel ID）
    assert normalize_trace_id(TRACE32) == TRACE32


def test_trace_id_rejects_non_hex():
    got = normalize_trace_id("not-a-trace!!")
    assert len(got) == 32 and "." not in got


# ── 采样 ──
def test_sampling_is_deterministic_per_trace():
    ids = [f"{i:016x}" for i in range(50)]
    first = [is_sampled_out(t, 0.5) for t in ids]
    second = [is_sampled_out(t, 0.5) for t in ids]
    assert first == second
    # 同一条 trace 的所有 span 共用 trace_id，所以判定天然一致
    assert is_sampled_out(ids[0], 1.0) is False
    assert is_sampled_out(ids[0], 0.0) is True


# ── OTLP 载荷 ──
def test_build_payload_structure():
    payload = build_payload([_record()], service_name="ssrm-test")
    rs = payload["resourceSpans"][0]
    res = {a["key"]: a["value"] for a in rs["resource"]["attributes"]}
    assert res["service.name"] == {"stringValue": "ssrm-test"}

    spans = {s["name"]: s for s in rs["scopeSpans"][0]["spans"]}
    assert len(spans) == 3
    http_span = spans["http"]
    assert http_span["traceId"] == normalize_trace_id("abc123def4567890")
    assert len(http_span["traceId"]) == 32
    assert http_span["kind"] == 2  # SERVER
    assert "parentSpanId" not in http_span  # 根 span 没有父
    # 时间戳是纳秒字符串，且结束晚于开始
    assert isinstance(http_span["startTimeUnixNano"], str)
    assert int(http_span["endTimeUnixNano"]) > int(http_span["startTimeUnixNano"])


def test_orphan_spans_attached_to_root():
    """自研 schema 里业务 span 的 parent 为空（中间件没预置根 span），
    导出时必须挂到根 span 下，否则 Jaeger 会画成并列的多条根链。"""
    spans = {s["name"]: s for s in
             build_payload([_record()])["resourceSpans"][0]["scopeSpans"][0]["spans"]}
    root_id = spans["http"]["spanId"]
    assert spans["rag.answer"]["parentSpanId"] == root_id
    # 已有父的 span 保持原父，不能被改写
    assert spans["llm.try"]["parentSpanId"] == "aaaa000000000002"


def test_genai_semconv_aliases():
    spans = {s["name"]: s for s in
             build_payload([_record()])["resourceSpans"][0]["scopeSpans"][0]["spans"]}
    attrs = {a["key"]: a["value"] for a in spans["llm.try"]["attributes"]}
    assert attrs["gen_ai.system"] == {"stringValue": "zhipu"}
    assert attrs["gen_ai.operation.name"] == {"stringValue": "chat"}
    # 原始属性保留（加别名而不是改名）
    assert attrs["provider"] == {"stringValue": "zhipu"}
    # 整数必须编码成字符串：JSON number 只有 53 位精度
    assert attrs["attempt"] == {"intValue": "1"}

    http_attrs = {a["key"]: a["value"] for a in spans["http"]["attributes"]}
    assert http_attrs["gen_ai.usage.total_tokens"] == {"intValue": "600"}
    assert http_attrs["url.path"] == {"stringValue": "/assist"}


def test_error_status_mapping():
    rec = _record()
    rec["spans"][1]["status"] = "error"
    rec["spans"][1]["attrs"]["error"] = "ReadTimeout: 7000ms"
    spans = {s["name"]: s for s in
             build_payload([rec])["resourceSpans"][0]["scopeSpans"][0]["spans"]}
    assert spans["rag.answer"]["status"]["code"] == 2
    assert "ReadTimeout" in spans["rag.answer"]["status"]["message"]
    assert spans["http"]["status"]["code"] == 1


def test_build_payload_skips_empty_trace():
    payload = build_payload([{"trace_id": "x", "ts_epoch": 1.0, "duration_ms": 1.0, "spans": []}])
    assert payload["resourceSpans"][0]["scopeSpans"][0]["spans"] == []


# ── 导出器：有界队列 + 失败隔离 ──
class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, boom: Exception | None = None, status=200):
        self.calls = []
        self.boom = boom
        self.status = status

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "body": json.loads(content), "headers": headers})
        if self.boom:
            raise self.boom
        return _FakeResp(self.status)


def _new_exporter(**kw):
    opts = {"endpoint": "http://127.0.0.1:4318", "batch_size": 4, "max_queue": 4}
    opts.update(kw)
    return OtlpExporter(**opts)


def test_export_posts_otlp_json():
    exp = _new_exporter()
    exp._client = _FakeClient()
    exp.enqueue(_record())
    sent = asyncio.run(exp.flush())
    assert sent == 3
    call = exp._client.calls[0]
    assert call["url"] == "http://127.0.0.1:4318/v1/traces"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_export_failure_is_swallowed():
    """collector 挂了不能影响主请求：flush 只记账，不抛异常"""
    exp = _new_exporter()
    exp._client = _FakeClient(boom=ConnectionError("collector down"))
    exp.enqueue(_record())
    assert asyncio.run(exp.flush()) == 0
    assert exp.failed == 1
    assert exp.stats()["failed_batches"] == 1


def test_queue_is_bounded_and_counts_drops():
    exp = _new_exporter(max_queue=2)
    exp._client = _FakeClient()
    for _ in range(5):
        exp.enqueue(_record())
    assert len(exp._queue) == 2
    assert exp.dropped == 3


def test_enqueue_after_stop_is_noop():
    """停机后不再收新数据：避免「已 flush 完又被塞进一批，然后永远发不出去」"""
    exp = _new_exporter()
    exp._client = _FakeClient()
    exp._closing = True
    exp.enqueue(_record())
    assert len(exp._queue) == 0


def test_http_error_status_counted_as_failure():
    exp = _new_exporter()
    exp._client = _FakeClient(status=503)
    exp.enqueue(_record())
    assert asyncio.run(exp.flush()) == 0
    assert exp.failed == 1


def test_module_level_export_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(otel, "_exporter", None)
    otel.export_trace(_record())          # 不应抛
    assert otel.otel_stats() == {"enabled": False}


@pytest.mark.parametrize("ratio,expect_drop", [(1.0, False), (0.0, True)])
def test_sample_ratio_extremes(ratio, expect_drop):
    exp = _new_exporter(sample_ratio=ratio)
    exp._client = _FakeClient()
    exp.enqueue(_record())
    assert (len(exp._queue) == 0) is expect_drop


# ── 中间件集成：入站 traceparent → 本地 id + 响应头回写 ──
def _run_through_middleware(headers, monkeypatch, path="/classify"):
    """跑一遍纯 ASGI 中间件，返回 (响应头 dict, 落库前的 record 列表)"""
    import asyncio

    from app.observability import obs as obs_mod
    from app.observability.tracing import span

    monkeypatch.setattr(obs_mod.settings, "trace_persist", False)  # 不碰真实 data/traces.db
    captured = []
    real_put = obs_mod.traces.put

    def spy_put(tid, record):
        captured.append(record)
        real_put(tid, record)

    monkeypatch.setattr(obs_mod.traces, "put", spy_put)

    async def fake_app(scope, receive, send):
        with span("rag.answer", provider="zhipu") as s:
            s["attrs"]["model"] = "glm-4-flash"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    out_headers = {}

    async def send(message):
        if message["type"] == "http.response.start":
            out_headers.update({k.decode(): v.decode() for k, v in message["headers"]})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = obs_mod.ObservabilityMiddleware(fake_app)
    scope = {
        "type": "http", "path": path, "method": "POST",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(mw(scope, receive, send))
    return out_headers, captured


def test_middleware_reuses_incoming_traceparent(monkeypatch):
    out, recs = _run_through_middleware(
        {"traceparent": f"00-{TRACE32}-{SPAN16}-01"}, monkeypatch
    )
    # 本地短号是上游 id 的尾部 16 位（人看），但导出用的 32 位必须原样
    assert out["x-trace-id"] == to_local_trace_id(TRACE32)
    assert recs[0]["otel_trace_id"] == TRACE32
    # 响应头回写的 traceparent 要能还原成上游那个 id
    assert parse_traceparent(out["traceparent"])["trace_id"] == TRACE32


def test_middleware_ignores_bad_traceparent(monkeypatch):
    out, recs = _run_through_middleware({"traceparent": "00-zzz-01"}, monkeypatch)
    assert len(out["x-trace-id"]) >= 8                       # 正常新建，不报错
    assert recs[0]["otel_trace_id"] == normalize_trace_id(recs[0]["trace_id"])


def test_middleware_emits_valid_traceparent_without_upstream(monkeypatch):
    out, recs = _run_through_middleware({}, monkeypatch)
    ctx = parse_traceparent(out["traceparent"])
    assert ctx is not None
    assert ctx["trace_id"] == recs[0]["otel_trace_id"]
    assert ctx["sampled"] is True


def test_exported_trace_uses_otel_trace_id(monkeypatch):
    """record 里的 otel_trace_id 要一路传到 OTLP 载荷，否则 Jaeger 里对不上上游"""
    out, recs = _run_through_middleware(
        {"traceparent": f"00-{TRACE32}-{SPAN16}-01"}, monkeypatch
    )
    spans = build_payload(recs)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert all(s["traceId"] == TRACE32 for s in spans)
    # 根 span 在中间件里生成，业务 span 的父应指向它
    root = next(s for s in spans if s["name"] == "http")
    child = next(s for s in spans if s["name"] == "rag.answer")
    assert child["parentSpanId"] == root["spanId"]
