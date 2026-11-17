"""run 级持久化：SQLite + WAL（F05 审批状态机 + F06 checkpoint）。

为什么是 SQLite 而不是框架的 checkpointer：
- Temporal 要额外服务，本项目单机部署；LangGraph 的 checkpointer 恢复点 ≠ 业务幂等点，
  反而要写适配层（「已发通知、未落库」的中间态重放会重复副作用）
- 这张表总共 5 张，单文件 + WAL 就够——v1 的 trace_store 已验证过这套参数
  （0.11ms/条、7 天 5.6MB）

写入时机（F06 §4，少一步就丢数据）：
每步**执行后**、下一步**装配前** checkpoint 一次（steps + facts 一个事务）。
事务提交后才允许下一步 → 崩溃最多丢一步，不会出现「step 记了但事实没记」。

审批状态机（F05 §3）：
pending ──approve──→ approved ──→ executing ──→ done
   │                    │
   ├──deny──→ denied    └──exec_failed──→ failed（可重试一次）
   └──timeout──→ expired（**默认拒绝**，不是默认允许）

DESTRUCTIVE 永不自动重放：requires=2 时单人 approve 只记 1/2，状态仍 pending。
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from oncall.tools import ToolCallRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, goal TEXT DEFAULT '', status TEXT DEFAULT 'running',
  conclusion TEXT DEFAULT '', reason TEXT DEFAULT '',
  created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS steps(
  run_id TEXT, i INTEGER, tool TEXT DEFAULT '', args_json TEXT DEFAULT '{}',
  ok INTEGER DEFAULT 0, error TEXT DEFAULT '', ms INTEGER DEFAULT 0,
  truncated INTEGER DEFAULT 0, skipped TEXT,
  PRIMARY KEY(run_id, i));
CREATE TABLE IF NOT EXISTS facts(
  run_id TEXT, ord INTEGER, field TEXT, value TEXT DEFAULT '', ref TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, run_id TEXT, tool TEXT, args_json TEXT DEFAULT '{}',
  why TEXT DEFAULT '', risk TEXT DEFAULT '', requires INTEGER DEFAULT 1,
  status TEXT DEFAULT 'pending', approved_count INTEGER DEFAULT 0,
  decided_by TEXT DEFAULT '', decided_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS applied_keys(
  key TEXT PRIMARY KEY, run_id TEXT, result_json TEXT DEFAULT '{}', created_at REAL);
"""


def idempotency_key(run_id: str, tool: str, args: dict) -> str:
    """写操作的幂等键：run_id + tool + 参数哈希（F05 §5）。

    为什么不只用 run_id：一个 run 可能执行多个写操作；为什么加 args：
    同 run 同工具不同参数是两次不同的动作。
    """
    raw = f"{tool}|{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
    return f"{run_id}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


class RunStore:
    def __init__(self, path: str | Path, *, approval_timeout_s: int = 7200) -> None:
        self.path = Path(path)
        # 超时默认拒绝（F05 §3）：没人管的审批不能变成全放行。测试里设 0 即「立刻过期」。
        self.approval_timeout_s = approval_timeout_s
        self._conn: Optional[sqlite3.Connection] = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def init(self) -> "RunStore":
        self._db()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── runs ──
    def ensure_run(self, run_id: str, goal: str) -> None:
        now = time.time()
        self._db().execute(
            "INSERT OR IGNORE INTO runs(run_id, goal, status, created_at, updated_at)"
            " VALUES(?,?, 'running', ?, ?)", (run_id, goal, now, now))
        self._db().commit()

    def set_status(self, run_id: str, status: str, *, conclusion: str = "",
                   reason: str = "") -> None:
        conn = self._db()
        conn.execute(
            "UPDATE runs SET status=?, conclusion=?, reason=?, updated_at=? WHERE run_id=?",
            (status, conclusion, reason, time.time(), run_id))
        conn.commit()

    # ── checkpoint：steps + facts 一个事务（少一步就丢数据）──
    def checkpoint(self, run_id: str, steps: list[ToolCallRecord], facts: list[dict]) -> None:
        conn = self._db()
        with conn:
            for s in steps:
                conn.execute(
                    "INSERT OR REPLACE INTO steps(run_id, i, tool, args_json, ok, error,"
                    " ms, truncated, skipped) VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, s.i, s.tool, json.dumps(s.args, ensure_ascii=False, default=str),
                     int(s.ok), s.error, s.ms, int(s.truncated), s.skipped))
            conn.execute("DELETE FROM facts WHERE run_id=?", (run_id,))
            conn.executemany(
                "INSERT INTO facts(run_id, ord, field, value, ref) VALUES(?,?,?,?,?)",
                [(run_id, k, str(f.get("field", "")), str(f.get("value", "")),
                  str(f.get("ref", ""))) for k, f in enumerate(facts)])
            conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (time.time(), run_id))

    def load_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """读回完整 run 状态（steps 连续无空洞由 checkpoint 的 PK(run_id,i) 保证）。"""
        conn = self._db()
        row = conn.execute(
            "SELECT goal, status, conclusion FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        step_rows = conn.execute(
            "SELECT i, tool, args_json, ok, error, ms, truncated, skipped"
            " FROM steps WHERE run_id=? ORDER BY i", (run_id,)).fetchall()
        fact_rows = conn.execute(
            "SELECT field, value, ref FROM facts WHERE run_id=? ORDER BY ord",
            (run_id,)).fetchall()
        return {
            "run_id": run_id, "goal": row[0], "status": row[1], "conclusion": row[2],
            "steps": [ToolCallRecord(
                i=r[0], tool=r[1], args=json.loads(r[2] or "{}"), ok=bool(r[3]),
                error=r[4], ms=r[5], truncated=bool(r[6]), skipped=r[7]) for r in step_rows],
            "facts": [{"field": r[0], "value": r[1], "ref": r[2]} for r in fact_rows],
        }

    # ── 审批状态机 ──
    def record_approval(self, approval_id: str, run_id: str, tool: str, args: dict,
                        why: str, risk: str, requires: int) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO approvals(id, run_id, tool, args_json, why, risk,"
            " requires, status, created_at) VALUES(?,?,?,?,?,?,?, 'pending', ?)",
            (approval_id, run_id, tool, json.dumps(args, ensure_ascii=False, default=str),
             why, risk, requires, time.time()))
        conn.commit()

    def current_approval(self, run_id: str) -> Optional[dict[str, Any]]:
        """run 的最新审批；pending 且超过时限 → 置 expired（默认拒绝）。"""
        conn = self._db()
        row = conn.execute(
            "SELECT id, tool, args_json, why, risk, requires, status, approved_count,"
            " created_at FROM approvals WHERE run_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (run_id,)).fetchone()
        if row is None:
            return None
        ap = {"id": row[0], "tool": row[1], "args": json.loads(row[2] or "{}"), "why": row[3],
              "risk": row[4], "requires": row[5], "status": row[6],
              "approved_count": row[7], "created_at": row[8]}
        if ap["status"] == "pending" and time.time() - ap["created_at"] > self.approval_timeout_s:
            conn.execute("UPDATE approvals SET status='expired', decided_at=? WHERE id=?",
                         (time.time(), ap["id"]))
            conn.commit()
            ap["status"] = "expired"
        return ap

    def decide(self, run_id: str, decision: str, decided_by: str) -> Optional[dict[str, Any]]:
        """处理待决审批。allow 且 requires=2 时只记 1/2，状态仍 pending（双人确认）。"""
        conn = self._db()
        ap = self.current_approval(run_id)
        if ap is None or ap["status"] != "pending":
            return ap
        now = time.time()
        if decision == "deny":
            conn.execute("UPDATE approvals SET status='denied', decided_by=?, decided_at=?"
                         " WHERE id=?", (decided_by, now, ap["id"]))
            ap["status"] = "denied"
        else:
            count = ap["approved_count"] + 1
            status = "approved" if count >= ap["requires"] else "pending"
            conn.execute("UPDATE approvals SET approved_count=?, status=?, decided_by=?,"
                         " decided_at=? WHERE id=?",
                         (count, status, decided_by, now, ap["id"]))
            ap["approved_count"] = count
            ap["status"] = status
        conn.commit()
        return ap

    # ── 幂等（F05 §5）：恢复点 ≠ 幂等点，这里就是那个幂等点 ──
    def applied(self, key: str) -> Optional[str]:
        row = self._db().execute(
            "SELECT result_json FROM applied_keys WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def mark_applied(self, key: str, run_id: str, result: Any) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR IGNORE INTO applied_keys(key, run_id, result_json, created_at)"
            " VALUES(?,?,?,?)",
            (key, run_id, json.dumps(result, ensure_ascii=False, default=str), time.time()))
        conn.commit()
