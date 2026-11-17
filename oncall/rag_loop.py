"""F12 Agentic RAG：检索 → 自评证据够不够 → 改写 → 再检索（≤3 轮）。

为什么检索值得加 loop 而分类不值得（F12 §2，面试高频对照）：
- 检索的失败原因是「用户说法与 KB 说法不一致」，换个说法往往就能召回——下一轮有新信息可用；
- 分类重跑一遍还是同一个答案，加环收益为 0。

与 v1 的关系（这点必须讲清，v1 文档里纠正过一次串行错误）：
v1 的 `_retrieve` 按 mode 分叉（hybrid 不调 LLM ∥ keyword+rerank 调 LLM），
RRF 与 LLM 重排是互斥路径。v2 不改这个分叉，只在外层加循环——每轮调一次 `_retrieve`。

改写是规则化的（不引入新组件、不调 LLM）：问题不在召回器不够强，在措辞不一致。
换更强的检索器是碰运气，还会让评测不可比。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.tools import Tool, ToolRegistry
from oncall.budget import RunBudget
from oncall.clock import Clock, SystemClock

MAX_ROUNDS = 3
# 每轮检索的预算（F12 §3）：检索本身不贵，贵的是 LLM 重排——按轮切，防止多轮把总预算吃光
PER_ROUND_MS = 8_000

# 内部单号（XF-885 / ONCALL-042）与日期/长数字时间戳：bigram 检索对它们敏感，
# 但它们对语义零贡献——召回为空时先删掉（F12 §4 第一行）
_NOISE_RE = re.compile(
    r"\b[A-Z]{1,6}-\d{2,8}\b"          # 内部单号
    r"|\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?\b"  # 日期
    r"|\b\d{10,}\b"                     # 长数字（时间戳/流水号）
)

# 现象/动作/根因三类字段（F12 §3 的「≥2 个不同字段」口径）
_FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "现象": ("title", "symptom"),
    "动作": ("resolution", "standard_action"),
    "根因": ("root_cause_class", "category"),
}


def strip_noise(query: str) -> str:
    """删掉单号/日期等噪声词并压平空白。检索对噪声词敏感但它们不承载语义。"""
    return re.sub(r"\s+", " ", _NOISE_RE.sub(" ", query)).strip()


def _fields_covered(hits: list[dict]) -> int:
    """top 命中共覆盖几个证据字段组。

    已知简化（诚实写明）：当前 KB 三域的每条都有 title/symptom/resolution，
    所以这个检查实际退化为「有命中」；它的真正价值是对**稀疏 schema** 的前向兼容
    ——字段可缺的领域（如只沉淀了现象、没沉淀处置）不会因为一条残缺命中就自信作答。
    """
    keys: set[str] = set()
    for h in hits:
        keys.update(k for k, v in h.items() if v)
    return sum(1 for group in _FIELD_GROUPS.values() if set(group) & keys)


def _default_admitted(h: dict, threshold: float) -> bool:
    """与 v1 `_admitted` 同口径（score 或 keyword_score 过阈值即采信）。

    优先用 rag 自带的实现（保持单一来源）；stub/外部实现没有时用这份兜底。
    """
    return (h.get("score") or 0) >= threshold or (h.get("keyword_score") or 0) >= threshold


def _enough(hits: list[dict], threshold: float, admitted: Callable) -> bool:
    if not hits:
        return False
    if not admitted(hits[0], threshold):
        return False
    return _fields_covered(hits) >= 2


def _rewrite(query: str, hits: list[dict]) -> tuple[str, str]:
    """按 F12 §4 的规则表改写，返回 (新 query, 改写说明)。无可改写时说明为空。"""
    if not hits:
        stripped = strip_noise(query)
        if stripped and stripped != query:
            return stripped, "删除内部单号/时间戳噪声词"
        return query, ""
    top = hits[0]
    # 用命中条目的标准措辞重问：title+symptom 是簇里最标准的描述，
    # 等价于「用 KB 的说法把问题重问一遍」（F12 §4 第三/四行）
    std = f"{top.get('title', '')} {top.get('symptom', '')}".strip()
    if std and std != query:
        return std, "用命中条目的标准措辞重问（对齐 KB 母本描述）"
    return query, ""


@dataclass
class RagLoopResult:
    hits: list[dict] = field(default_factory=list)
    rounds: int = 0
    rewrites: list[str] = field(default_factory=list)
    enough: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"rounds": self.rounds, "rewrites": self.rewrites,
                "enough": self.enough, "hits": self.hits}


async def agentic_retrieve(
    rag: Any,
    question: str,
    *,
    max_rounds: int = MAX_ROUNDS,
    top_k: int = 3,
    clock: Optional[Clock] = None,
    per_round_ms: int = PER_ROUND_MS,
) -> RagLoopResult:
    """检索循环主体。每轮从独立预算切 8s，直到证据够或改写不动了。

    两轮都不行就停（返回 enough=False + 最接近的几条），不硬答——
    库外问题「拼一个最接近的答案」比承认没查到更危险。
    """
    clock = clock or SystemClock()
    admitted = getattr(rag, "_admitted", None) or _default_admitted
    threshold = getattr(getattr(rag, "spec", None), "threshold", 0.4)

    q = question
    rewrites: list[str] = []
    last_hits: list[dict] = []
    rounds_used = 0

    for i in range(1, max_rounds + 1):
        rounds_used = i
        last_hits = await rag._retrieve(q, top_k, budget=RunBudget(per_round_ms, clock))
        if _enough(last_hits, threshold, admitted):
            return RagLoopResult(last_hits, i, rewrites, True)
        if i == max_rounds:
            break
        q2, reason = _rewrite(q, last_hits)
        if not reason:
            break  # 无可改写 → 不空转
        rewrites.append(reason)
        q = q2

    return RagLoopResult(last_hits, rounds_used, rewrites, False)


def register_rag_tool(registry: ToolRegistry, rag: Any, *, clock: Optional[Clock] = None) -> None:
    """把 agentic RAG 注册为 harness 的确定性工具 `rag_search`。

    对主 loop 而言它就是一个只读工具；「多轮改写」发生在工具内部，
    不占用主 loop 的步数预算——检索细节不属于 agent 的决策面。
    """
    id_field = getattr(getattr(rag, "spec", None), "id_field", "ticket_id")

    async def rag_search(query: str, top_k: int = 3) -> dict[str, Any]:
        r = await agentic_retrieve(rag, query, top_k=max(1, min(top_k, 5)), clock=clock)
        return {
            "enough": r.enough,
            "rounds": r.rounds,
            "rewrites": r.rewrites,
            "hits": [{
                "id": h.get(id_field),
                "title": h.get("title"),
                "class": h.get("root_cause_class") or h.get("category"),
                "resolution": (h.get("resolution") or "")[:120],
                "score": h.get("score"),
            } for h in r.hits],
        }

    registry.register(Tool(
        name="rag_search",
        description="按自然语言现象检索值班知识库，返回最相关的历史工单（含根因类别与处置）。"
                    "证据不足时会自动改写重试（最多 3 轮）；返回 enough=false 表示库内没有可信依据，不要据此编造结论",
        params={
            "query": {"type": "string", "description": "现象描述（可含报错关键词）", "required": True},
            "top_k": {"type": "integer", "description": "返回条数，默认 3，上限 5", "required": False},
        },
        handler=rag_search,
    ))
