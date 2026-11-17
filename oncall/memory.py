"""事故记忆（F15）：让「上次同类事故怎么解决的」下次自动出现在上下文里。

只做程序性记忆（事故 → 结论 → 动作 的映射），不做全库向量检索历史对话——
检索噪声会污染上下文，比没有记忆更糟。三条写入纪律：

1. **只写已验证的**：api 层只在 status=ok（过了传感器）时调 record()；
   needs_human 的未验证结论写进去会变成下次的"经验"；
2. **坐标与检索同源**：cluster = root_cause_class（KB 的簇），不另造一套相似度体系；
3. **记 outcome**：outcome=failed 的记录（人工/外部写入）同样保留——
   「这条动作对此现象无效」也是知识，注入时必须带「未解决」标记。

召回为什么允许 bigram 指纹兜底：run 开始时簇还不知道（分类是 run 的产出），
所以装配期先按「现象指纹」在同 domain 内找同现象的历史；簇字段随记录落库，
供将来「已知簇」的场景精确索引。跨 domain 永不召回——credit 的问题不注入 ssrm 的记忆。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# 召回条数与体量上限（F15 §5）：整段 ≤200 token。中文按 2 字符≈1 token 的保守估计，
# 段落正文超过 400 字符即超预算——测试按字符数断言，不假装能精确数 token。
RECALL_LIMIT = 3
MAX_ITEM_CHARS = 60          # 每条记忆正文 ≤60 字
MAX_BLOCK_CHARS = 400        # 整段 ≤200 token 的字符上界
_MIN_OVERLAP = 0.12          # 现象指纹 bigram 重叠的下限，低于它不算「同现象」

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain TEXT, cluster TEXT DEFAULT '', fingerprint TEXT DEFAULT '',
  conclusion TEXT DEFAULT '', action TEXT DEFAULT '',
  outcome TEXT DEFAULT 'ok', run_id TEXT DEFAULT '', created_at REAL);
"""


def _bigrams(text: str) -> set[str]:
    t = "".join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else {t} if t else set()


def overlap(a: str, b: str) -> float:
    """两个现象描述的 bigram 重叠率（Jaccard）。复用 v1 的结论：现象相似用字符级就够了。"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


class IncidentMemory:
    """事故记忆存储（SQLite，独立文件——不与 RunStore 的表混住，互不迁移）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── 写 ──
    def record(self, *, domain: str, cluster: str, fingerprint: str,
               conclusion: str, action: str, outcome: str = "ok",
               run_id: str = "") -> int:
        """写入一条记忆。outcome 只应是 ok / failed；api 层保证自动写入只来自已验证 run。"""
        cur = self._db().execute(
            "INSERT INTO incident_memory(domain, cluster, fingerprint, conclusion, action,"
            " outcome, run_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (domain, cluster, fingerprint[:200], conclusion[:200], action[:200],
             outcome, run_id, time.time()))
        self._db().commit()
        return cur.lastrowid or 0

    # ── 读 ──
    def recall(self, goal: str, *, domain: str, limit: int = RECALL_LIMIT) -> list[dict]:
        """同 domain 内找「同现象」的历史，排序：已验证在前、时间新在前。跨 domain 永不召回。"""
        rows = self._db().execute(
            "SELECT cluster, fingerprint, conclusion, action, outcome, run_id, created_at"
            " FROM incident_memory WHERE domain=? ORDER BY created_at DESC LIMIT 200",
            (domain,)).fetchall()
        scored = []
        for r in rows:
            ov = overlap(goal, r[2]) if not r[2] else max(overlap(goal, r[2]), overlap(goal, r[3]))
            ov = max(ov, overlap(goal, r[1]))  # fingerprint 本身也是匹配面
            if ov < _MIN_OVERLAP:
                continue
            scored.append((ov, r))
        scored.sort(key=lambda x: (x[0], x[1][6]), reverse=True)
        scored.sort(key=lambda x: x[1][4] != "ok")  # 稳定排序：ok 仍在每组内保持时间序
        return [{
            "cluster": r[0], "fingerprint": r[1], "conclusion": r[2],
            "action": r[3], "outcome": r[4], "run_id": r[5], "created_at": r[6],
        } for _, r in scored[:limit]]

    # ── 渲染 ──
    def render_block(self, goal: str, *, domain: str) -> str:
        """装配进上下文的「历史同类」段。空召回返回空串（不占上下文）。

        注入的是「结论 + 动作 + 结果」，不是原文——给模型看原文等于把上次的噪声再灌一遍。
        failed 的记忆必须带「未解决」标记（F15 §7.2 的验收点）。
        """
        items = self.recall(goal, domain=domain)
        if not items:
            return ""
        lines = ["历史同类（供参考，不得作为证据引用）："]
        for it in items:
            mark = "已验证" if it["outcome"] == "ok" else f"未解决({it['outcome']})"
            body = f"现象 {it['fingerprint']} → 结论 {it['conclusion']} → 动作 {it['action']}（{mark}）"
            lines.append("- " + body[:MAX_ITEM_CHARS])
        block = "\n".join(lines)
        if len(block) > MAX_BLOCK_CHARS:
            block = block[:MAX_BLOCK_CHARS]
        return block

    def stats(self) -> dict[str, Any]:
        n = self._db().execute("SELECT COUNT(*) FROM incident_memory").fetchone()[0]
        by_outcome = dict(self._db().execute(
            "SELECT outcome, COUNT(*) FROM incident_memory GROUP BY outcome").fetchall())
        return {"total": n, "by_outcome": by_outcome}

    @staticmethod
    def from_json(raw: str) -> dict[str, Any]:
        return json.loads(raw)
