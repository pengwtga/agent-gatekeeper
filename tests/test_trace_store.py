"""Trace 落库（T2-05）单测：往返一致性、列表过滤、保留期清理

不碰真实 data/ 目录：每个用例用 tmp_path 建独立库，
也不依赖 FastAPI/LLM，纯存储层测试（零 token）。
"""

import time
import uuid
from pathlib import Path

import pytest

from app.observability.trace_store import TraceStore


def _record(trace_id="abc123", path="/classify", status=200, duration_ms=120.0,
            outcome="ok", ts_epoch=None, spans=None):
    return {
        "trace_id": trace_id,
        "ts": "12:00:00",
        "ts_epoch": ts_epoch if ts_epoch is not None else time.time(),
        "method": "POST", "path": path, "status": status,
        "duration_ms": duration_ms, "outcome": outcome,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "calls": 1},
        "spans": spans if spans is not None else [
            {"name": "http", "start_ms": 0.0, "duration_ms": duration_ms,
             "status": "ok", "depth": 0, "parent": None,
             "attrs": {"method": "POST", "path": path, "status": status}},
            {"name": "classify", "start_ms": 3.0, "duration_ms": duration_ms - 3,
             "status": "ok", "depth": 0, "parent": None, "attrs": {"budget_ms": 12000}},
            {"name": "llm.try", "start_ms": 5.0, "duration_ms": 80.0,
             "status": "ok", "depth": 1, "parent": "classify",
             "attrs": {"provider": "zhipu", "attempt": 1, "timeout_ms": 7000}},
        ],
    }


@pytest.fixture()
def store():
    """每个用例一个独立库。

    不用 pytest 的 tmp_path：部分受限环境（沙箱/容器）不允许在系统临时目录建目录，
    且 pytest 清理 basetemp 时可能被安全策略拦下。放仓库内的 .pytest_data/ 更可控，
    用例结束尽力清理（清理失败不影响断言，目录已在 .gitignore 里）。
    """
    base = Path(__file__).resolve().parents[1] / ".pytest_data"
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"traces_{uuid.uuid4().hex[:8]}.db"
    s = TraceStore(db_path, retention_days=7)
    yield s
    s.close()
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            (base / f"{db_path.name}{suffix}").unlink()
        except OSError:
            pass


def test_roundtrip_keeps_shape_and_span_order(store):
    """写入→读出：结构、span 顺序、attrs、usage 全部一致（前端零改动的前提）"""
    store.put(_record())
    got = store.get("abc123")

    assert got is not None
    assert got["path"] == "/classify" and got["status"] == 200
    assert [s["name"] for s in got["spans"]] == ["http", "classify", "llm.try"]
    assert got["spans"][2]["attrs"]["provider"] == "zhipu"
    assert got["spans"][2]["depth"] == 1 and got["spans"][2]["parent"] == "classify"
    assert got["usage"]["total_tokens"] == 15
    # 内存版有的字段这里都要有，否则前端读 ts 会拿到 undefined
    assert got["ts"].count(":") == 2 and got["ts_iso"].startswith("20")


def test_get_missing_returns_none(store):
    assert store.get("not-exist") is None


def test_put_is_idempotent_per_trace_id(store):
    """同一个 trace_id 重复写（重试/补齐）不应留下重复 span"""
    store.put(_record())
    store.put(_record(duration_ms=200.0))
    got = store.get("abc123")
    assert got["duration_ms"] == 200.0
    assert len(got["spans"]) == 3
    assert len(store) == 1


def test_list_filters_and_order(store):
    now = time.time()
    store.put(_record(trace_id="t-old", path="/classify", duration_ms=100.0, ts_epoch=now - 300))
    store.put(_record(trace_id="t-slow", path="/assist", duration_ms=9000.0, ts_epoch=now - 120))
    store.put(_record(trace_id="t-err", path="/classify", status=500,
                      outcome="server_error", ts_epoch=now - 60))

    rows = store.list(limit=10)
    assert [r["trace_id"] for r in rows] == ["t-err", "t-slow", "t-old"]  # 时间倒序

    assert [r["trace_id"] for r in store.list(path="/classify")] == ["t-err", "t-old"]
    assert [r["trace_id"] for r in store.list(min_duration_ms=1000)] == ["t-slow"]
    assert [r["trace_id"] for r in store.list(only_errors=True)] == ["t-err"]
    # since_minutes 过滤：只有最近 3 分钟内
    assert [r["trace_id"] for r in store.list(since_minutes=3)] == ["t-err", "t-slow"]
    assert store.list(limit=2) and len(store.list(limit=2)) == 2


def test_prune_removes_old_traces_and_spans(store):
    store.put(_record(trace_id="t-new", ts_epoch=time.time()))
    store.put(_record(trace_id="t-old", ts_epoch=time.time() - 8 * 86400))

    deleted = store.prune()

    assert deleted == 1
    assert store.get("t-old") is None
    assert store.get("t-new") is not None
    # 子表不能留孤儿 span
    orphans = store._conn.execute(
        "SELECT COUNT(*) FROM spans WHERE trace_id = 't-old'").fetchone()[0]
    assert orphans == 0


def test_put_survives_unserializable_attrs(store):
    """attrs 里混进非 JSON 类型不该让整条链路丢失（default=str 兜底）"""
    spans = [{"name": "http", "start_ms": 0.0, "duration_ms": 1.0, "status": "ok",
              "depth": 0, "parent": None, "attrs": {"obj": object()}}]
    store.put(_record(spans=spans))
    got = store.get("abc123")
    assert got["spans"][0]["attrs"]["obj"].startswith("<object object at")


def test_middleware_survives_store_failure(monkeypatch):
    """落库抛异常时请求仍 200：观测是旁路，不能把 200 变 500"""
    import asyncio

    from app.observability import obs as obs_mod
    from app.observability.tracing import span

    class Boom:
        def put(self, record):
            raise RuntimeError("disk is on fire")

    monkeypatch.setattr(obs_mod, "get_trace_store", lambda: Boom())

    sent = []

    async def fake_app(scope, receive, send):
        with span("demo") as s:      # 造一个有 span 的业务请求，才会走到落库分支
            s["attrs"]["ok"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    mw = obs_mod.ObservabilityMiddleware(fake_app)
    scope = {"type": "http", "path": "/classify", "method": "POST", "headers": []}
    asyncio.run(mw(scope, receive, send))

    start = [m for m in sent if m["type"] == "http.response.start"]
    assert start and start[0]["status"] == 200
