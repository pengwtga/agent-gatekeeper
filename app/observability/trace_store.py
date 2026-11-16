"""Trace 落库（T2-05）：把「最近 100 条内存链路」变成「可按时间检索的证据」

`tracing.py` 的 `TraceStore` 是热缓存：进程内、100 条、重启归零。它回答的是
「刚才那条慢请求慢在哪」；回答不了「昨天下午那批超时是什么样」——而后者才是
值班复盘和诊断平台的日常。本模块负责后者。

设计要点（选型理由见 docs/optimization/T2-05-trace-persist.md）：

1. **SQLite(WAL)**：单实例零运维，与 escalation.py 同一套做法；点查、时间范围扫描、
   按 path/状态过滤三种模式加对索引都能满足。多实例时换 Postgres，方法签名不变。
2. **两张表**：traces（一请求一行）+ spans（一 span 一行）。不塞成一个大 JSON，
   是为了支持 span 级聚合（「llm.try 失败率」「按 span 名的 P95」）。
3. **写入不在事件循环里做**：中间件用 `asyncio.to_thread` 调 `put`，避免 SQLite
   的写锁和 commit 拖住事件循环。写入失败只记日志，绝不影响主请求——观测是旁路。
4. **保留期靠写时摊薄**：每 500 次写入清一次过期数据，不在每个请求上做 DELETE。
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("app.trace_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id     TEXT PRIMARY KEY,
    ts_epoch     REAL NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    duration_ms  REAL NOT NULL,
    span_count   INTEGER NOT NULL,
    has_error    INTEGER NOT NULL,
    usage_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(ts_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_traces_path_ts ON traces(path, ts_epoch DESC);

CREATE TABLE IF NOT EXISTS spans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
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
"""

# 每写多少次清一次过期数据。500 是「体积可控」与「清理成本可忽略」的折中：
# 按每天 1000 请求算，约 12 小时清一次，单次 DELETE 只碰很小一片索引。
_PRUNE_EVERY = 500


class TraceStore:
    """Trace 的持久化存储。线程安全：单连接 + 锁（写入来自 to_thread 的线程池）。"""

    def __init__(self, path: Path, retention_days: int = 7):
        self._path = str(path)
        self._retention_days = max(1, retention_days)
        self._lock = threading.Lock()
        self._writes = 0
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 并发写时不要立刻报 database is locked：等 3s 比抛错好（写入本来就是旁路）
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ── 写 ──
    def put(self, record: dict[str, Any]) -> None:
        """落一条 trace。record 与内存 TraceStore 里的结构同构（前端零改动）。"""
        spans = record.get("spans") or []
        usage = record.get("usage") or {}
        ts_epoch = float(record.get("ts_epoch") or time.time())
        has_error = int(
            record.get("outcome") in ("error", "server_error")
            or any(s.get("status") == "error" for s in spans)
        )
        with self._lock:
            # traces 与 spans 分两条语句写（无外键级联）：进程被杀时可能留下
            # 「trace 有、spans 不全」的窗口。诊断场景下「缺 span」比「整条丢失」危害小，
            # 读侧可拿 span_count 与实际条数比对发现，暂不做两阶段提交。
            self._conn.execute(
                "INSERT OR REPLACE INTO traces"
                " (trace_id, ts_epoch, method, path, status, outcome, duration_ms,"
                "  span_count, has_error, usage_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record["trace_id"], ts_epoch, record.get("method", "-"),
                    record.get("path", "-"), int(record.get("status", 0)),
                    record.get("outcome", "ok"), float(record.get("duration_ms", 0.0)),
                    len(spans), has_error,
                    json.dumps(usage, ensure_ascii=False, default=str),
                ),
            )
            self._conn.execute("DELETE FROM spans WHERE trace_id = ?", (record["trace_id"],))
            self._conn.executemany(
                "INSERT INTO spans (trace_id, seq, name, start_ms, duration_ms, status,"
                " depth, parent, attrs_json) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        record["trace_id"], i, s.get("name", "-"),
                        float(s.get("start_ms", 0.0)), float(s.get("duration_ms", 0.0)),
                        s.get("status", "ok"), int(s.get("depth", 0)), s.get("parent"),
                        json.dumps(s.get("attrs") or {}, ensure_ascii=False, default=str),
                    )
                    for i, s in enumerate(spans)
                ],
            )
            self._conn.commit()
            self._writes += 1
            if self._writes % _PRUNE_EVERY == 0:
                self._prune_locked()

    # ── 读 ──
    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        """按 trace_id 取完整链路，返回结构与内存版完全一致（多一个 ts_iso）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT trace_id, ts_epoch, method, path, status, outcome, duration_ms,"
                " span_count, usage_json FROM traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if row is None:
                return None
            span_rows = self._conn.execute(
                "SELECT name, start_ms, duration_ms, status, depth, parent, attrs_json"
                " FROM spans WHERE trace_id = ? ORDER BY seq",
                (trace_id,),
            ).fetchall()

        spans = []
        for name, start_ms, duration_ms, status, depth, parent, attrs_json in span_rows:
            try:
                attrs = json.loads(attrs_json)
            except (TypeError, ValueError):
                attrs = {}
            spans.append({
                "name": name, "start_ms": start_ms, "duration_ms": duration_ms,
                "status": status, "depth": depth, "parent": parent, "attrs": attrs,
            })
        try:
            usage = json.loads(row[8])
        except (TypeError, ValueError):
            usage = {}
        return {
            "trace_id": row[0],
            "ts": time.strftime("%H:%M:%S", time.localtime(row[1])),
            # ts 沿用内存版格式（前端不改）；跨天回查需要日期，所以另给一个完整时间
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row[1])),
            "method": row[2], "path": row[3], "status": row[4], "outcome": row[5],
            "duration_ms": row[6], "span_count": row[7], "usage": usage, "spans": spans,
        }

    def list(
        self,
        limit: int = 20,
        path: Optional[str] = None,
        min_duration_ms: Optional[float] = None,
        only_errors: bool = False,
        since_minutes: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """列表查询：按时间倒序返回精简行（不含 spans，避免一页传几百 KB）。"""
        where: list[str] = []
        params: list[Any] = []
        if path:
            where.append("path = ?")
            params.append(path)
        if min_duration_ms is not None:
            where.append("duration_ms >= ?")
            params.append(float(min_duration_ms))
        if only_errors:
            where.append("has_error = 1")
        if since_minutes is not None:
            where.append("ts_epoch >= ?")
            params.append(time.time() - since_minutes * 60)

        sql = (
            "SELECT trace_id, ts_epoch, method, path, status, outcome, duration_ms,"
            " span_count, has_error, usage_json FROM traces"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts_epoch DESC LIMIT ?"
        params.append(max(1, min(200, limit)))

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        out = []
        for r in rows:
            try:
                usage = json.loads(r[9])
            except (TypeError, ValueError):
                usage = {}
            out.append({
                "trace_id": r[0],
                "ts": time.strftime("%H:%M:%S", time.localtime(r[1])),
                "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[1])),
                "method": r[2], "path": r[3], "status": r[4], "outcome": r[5],
                "duration_ms": r[6], "span_count": r[7],
                "has_error": bool(r[8]), "usage": usage,
            })
        return out

    # ── 保留期 ──
    def prune(self, now: Optional[float] = None) -> int:
        """删除超过保留期的 trace（连带 span），返回删除的 trace 条数。"""
        with self._lock:
            return self._prune_locked(now)

    def _prune_locked(self, now: Optional[float] = None) -> int:
        cutoff = (now if now is not None else time.time()) - self._retention_days * 86400
        # 先删子表再删主表：未开外键级联，反过来会留下孤儿 span
        self._conn.execute(
            "DELETE FROM spans WHERE trace_id IN"
            " (SELECT trace_id FROM traces WHERE ts_epoch < ?)", (cutoff,),
        )
        cur = self._conn.execute("DELETE FROM traces WHERE ts_epoch < ?", (cutoff,))
        self._conn.commit()
        deleted = cur.rowcount or 0
        if deleted:
            logger.info(
                "清理过期 trace",
                extra={"event": "trace_prune", "stage": "storage",
                       "count": deleted, "reason": f"retention={self._retention_days}d",
                       "outcome": "ok"},
            )
        return deleted

    def __len__(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ── 懒加载单例 ──
# 不在 import 时建库：测试会 import 本模块，若在 import 期就碰 data/ 目录，
# 单测会在仓库里留下 trace 文件；也避免只跑评测脚本时多建一个库。
_store: Optional[TraceStore] = None
_store_lock = threading.Lock()


def get_trace_store() -> TraceStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = TraceStore(Path(settings.trace_db), settings.trace_retention_days)
    return _store


def reset_trace_store() -> None:
    """测试用：丢掉单例（不影响已建表的文件）"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
