"""升级队列（T5-3）：人工接管的持久化与续跑

为什么需要它：编排层 decide 出 human_review 时，请求就结束了——但人工不可能是
同步的（值班同学可能一小时后才看）。如果中断状态只留在 HTTP 响应里，人工确认后
就无从续跑。所以中断即落库：incident + 分类结果 + 编排留痕整体快照进 SQLite，
人工批准后从快照续跑 decide，全过程可审计。

为什么 SQLite 而不是内存 dict：
1. 中断到人工确认之间服务可能重启/发版——内存队列等于把「人工接管」挂在进程生命
   周期上，重启一次丢一批待办；
2. 单实例部署下 SQLite（WAL 模式）零运维成本；多实例时换 Postgres，接口不变。

写路径只有两处：human_review 落库（pending）、人工决议（resolved）。
读路径：列表页 + 单条取回。不做更新/删除——升级记录是审计线索，只追加决议，不篡改。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    domain TEXT NOT NULL,
    incident_id TEXT,
    predicted_class TEXT,
    disposition TEXT NOT NULL,
    reason TEXT,
    state_json TEXT NOT NULL,      -- 中断快照：incident + classify_result + pipeline
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | resolved
    decision TEXT,                 -- 人工决议摘要（续跑后回填）
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON escalations(status, created_at DESC);
"""


class EscalationStore:
    """SQLite 升级队列。线程锁保护：FastAPI 单实例下可能有并发请求写。"""

    def __init__(self, path: Path):
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(self, domain: str, result: dict[str, Any]) -> int:
        """human_review 落库。state_json 是完整中断快照，续跑只需要它。"""
        state = {
            "incident": result.get("incident") or {},
            "classify_result": {k: v for k, v in result.items() if k != "incident"},
            "saved_at": time.time(),
        }
        incident_id = (result.get("incident") or {}).get("incident_id")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO escalations (created_at, domain, incident_id, predicted_class,"
                " disposition, reason, state_json) VALUES (?,?,?,?,?,?,?)",
                (time.time(), domain, incident_id, result.get("root_cause_class"),
                 result.get("disposition"), result.get("disposition_reason", "")[:300],
                 json.dumps(state, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get(self, escalation_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at, domain, incident_id, predicted_class, disposition,"
                " reason, state_json, status, decision, decided_at"
                " FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
        return self._row(row)

    def resolve(self, escalation_id: int, decision: str) -> bool:
        """回填人工决议。已决议的再次决议返回 False（审计线索不可覆盖）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE escalations SET status='resolved', decision=?, decided_at=?"
                " WHERE id=? AND status='pending'",
                (decision[:500], time.time(), escalation_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list(self, status: str = "pending", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, domain, incident_id, predicted_class, disposition,"
                " reason, state_json, status, decision, decided_at"
                " FROM escalations WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        return [self._row(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            (n,) = self._conn.execute(
                "SELECT COUNT(*) FROM escalations WHERE status='pending'").fetchone()
        return n

    @staticmethod
    def _row(row) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        d = dict(zip(["id", "created_at", "domain", "incident_id", "predicted_class",
                      "disposition", "reason", "state_json", "status", "decision",
                      "decided_at"], row))
        d["state"] = json.loads(d.pop("state_json"))
        return d

    def close(self):
        self._conn.close()
