"""F11 Sub-agent 并行取证：把「互不依赖的几路取证」并行化，并把噪声关在各自的上下文里。

先回答一个必然会被问的问题：日志/指标/变更明明是工具，为什么还要 agent？
——工具回答「这一步怎么查」，子 agent 回答「这一路怎么查」。一路取证往往不是
一次调用（先查 A，看到结果才知道该查 B），承载这个「看结果再决定下一步」的
循环体就是一个 loop；子 agent 只是把这个 loop 关进「只读 + 限步 + 独立上下文」
的小盒子里，主 loop 只收结构化 Finding。

为什么默认只有两路（kb / topology）：
- 并行成立的前提是**互不依赖**。history（query_incidents）必须先有类名才能查，
  它天然依赖 kb 的结论——把它放进并行组，等于让子 agent 猜类名，
  所以它留给 F17 的确定性补齐，不在这里假装独立。
- 真实 SRE 部署里，日志/指标/变更是另外三条独立的线，只需要在
  `EVIDENCE_LINES` 里加配置，机制不变——**线是数据，不是代码**。

冲突纪律（多 agent 最容易被问的地方）：两路结论矛盾时**不投票**。
两路一致不代表对（可能看的是同一个坏数据源）；把矛盾本身作为信息交给人，
比选一个「看起来更合理」的结论安全。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.core.tools import ToolRegistry
from oncall.tools import SideEffect, call_tool, side_effect_of, specs_for_prompt

# 子任务比主任务窄：3 步足够「查一次 → 看结果 → 换个参数再查一次」
DEFAULT_MAX_STEPS = 3
DEFAULT_TIMEOUT_MS = 20_000

# 自动触发的多源线索词：出现这类词，说明结论需要串联两个以上信息源
MULTI_SOURCE_HINTS = (
    "为什么", "影响", "上下游", "链路", "对比", "分别", "同时",
    "哪个组件", "哪个服务", "影响面", "波及", "根因",
)


@dataclass(frozen=True)
class EvidenceLine:
    """一路取证 = 一个只读工具子集 + 一句取证目标。"""

    name: str
    field: str          # 证据落进事实段时用的 field（kb/topology/…）
    tools: tuple[str, ...]
    instruction: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "field": self.field, "tools": list(self.tools)}


# ssrm 的默认两路。加一路 = 在这里加一条（真实部署里就是 logs/metrics/changes）
EVIDENCE_LINES: tuple[EvidenceLine, ...] = (
    EvidenceLine(
        name="kb",
        field="kb",
        tools=("rag_search",),
        instruction="用现象原话在知识库检索，找出：①命中的根因类名 ②可执行的处置动作。"
                    "只报检索结果里真实出现的内容，不要补常识。",
    ),
    EvidenceLine(
        name="topology",
        field="topology",
        tools=("system_topology",),
        instruction="说明该系统链路上与本次现象相关的组件（xFlow/EA/Dora 等）及调用关系，"
                    "指出最可能出问题的一环。只依据拓扑与事实，不要猜根因类名。",
    ),
)


@dataclass
class Finding:
    """子 agent 必须回传的结构化结论——不接受自由文本（合并时会二次失真）。"""

    line: str
    summary: str = ""
    evidence: list[dict] = field(default_factory=list)   # [{field, quote}]
    class_guess: str = ""
    confidence: float = 0.0
    unresolved: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    error: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "summary": self.summary, "evidence": self.evidence,
                "class_guess": self.class_guess, "confidence": self.confidence,
                "unresolved": self.unresolved, "steps": self.steps,
                "error": self.error}


@dataclass
class GatherResult:
    findings: list[Finding] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings],
                "conflicts": self.conflicts, "usage": self.usage}


SUBAGENT_SYSTEM = """你是 SSRM 值班排障的**只读取证**子 agent，只负责分配给你的一条取证线路。

规则：
1. 只能用工具清单里给你的工具，且只做分配给你的那条线，不要越界查别的线；
2. 每次只调一个工具；同一工具+相同参数不要重复调；
3. 拿到足够信息后立刻 finish，不要为了「更全面」多查一步；
4. 证据 quote 必须是工具返回的原文片段（10~40 字），不要改写、不要编造；
5. **args 里只填参数值**，例如 `{"query":"注册卡单"}`——不要复制工具清单里的
   字段定义（把 {"type":"string"} 当参数值是真实模型踩过的坑，参数校验会直接拒绝）；
6. 这条线拿不到结论就如实 finish，unresolved 里写清缺什么——不要猜。

输出必须是 JSON，二选一：
调工具：{"action":"tool","tool":"名称","args":{},"thought":"一句话"}
结束：  {"action":"finish","summary":"这条线的结论（一句话）",
        "evidence":[{"field":"kb","quote":"工具返回原文片段"}],
        "class_guess":"根因类名，不确定就留空","confidence":0.0,
        "unresolved":["缺什么"]}"""


def _facts_block(facts: Sequence[dict]) -> str:
    """子 agent 自己的上下文：事实可以显示得更完整（噪声只污染它自己）。

    主 loop 的事实段压到 200 字是为了省上下文；这里压太狠会让模型看不见
    检索结果的类名/命中条目，直接给出「不足以判断」——真模型实测踩过。
    """
    rows = []
    for f in facts:
        value = str(f.get("value", ""))
        rows.append(f"- {f.get('field')} = {value[:600]}")
    return "\n".join(rows) or "- （暂无）"


def _sub_messages(goal: str, line: EvidenceLine, facts: Sequence[dict],
                  tool_specs: Sequence[dict], *, last_step: bool = False) -> tuple[str, str]:
    """last_step 是必须的：真模型实测子 agent 会「再查一次确认一下」查满 3 步。

    单路取证的边际收益在第一次成功调用后就接近 0——它该做的是把看到的东西
    结构化交回去，而不是在主 loop 之外再演一遍「反复确认」。
    """
    last = ("\n## 最后一步\n这是本路最后一次机会：**必须输出 finish**（结论可以写「已取到，"
            "但不足以判断」，不要再调工具）。\n" if last_step else "")
    payload = (
        f"## 总任务\n{goal}\n\n"
        f"## 你负责的线路：{line.name}\n{line.instruction}\n\n"
        f"## 本路已取回的内容\n{_facts_block(facts)}\n\n"
        f"## 本路可用工具\n{json.dumps(list(tool_specs), ensure_ascii=False)}\n"
        f"{last}"
    )
    return SUBAGENT_SYSTEM, payload


def available_lines(registry: ToolRegistry,
                    lines: Optional[Sequence[EvidenceLine]] = None) -> list[EvidenceLine]:
    """只保留「工具全在注册表里且都是只读」的线路。

    只读不是靠 prompt 要求的，是**机制上不给写工具**：子 agent 连尝试的机会都没有。
    """
    out: list[EvidenceLine] = []
    for line in (lines or EVIDENCE_LINES):
        if not line.tools:
            continue
        if any(registry.get(t) is None for t in line.tools):
            continue
        if any(side_effect_of(t) is not SideEffect.READ for t in line.tools):
            continue
        out.append(line)
    return out


def should_spawn(goal: str, registry: ToolRegistry, mode: str = "auto",
                 *, lines: Optional[Sequence[EvidenceLine]] = None) -> bool:
    """触发判定。三个条件同时成立才并行——不默认 spawn。

    1. 至少两路可用（一路并行没有意义，串行调工具更便宜）；
    2. auto 模式下 goal 里有多源线索（「为什么/影响面/链路…」）；
    3. 不重复造预算：调用方保证只在首轮、facts 为空时调本函数。
    """
    if mode == "off":
        return False
    usable = available_lines(registry, lines)
    if len(usable) < 2:
        return False
    if mode == "force":
        return True
    return any(h in goal for h in MULTI_SOURCE_HINTS)


async def run_subagent(
    line: EvidenceLine,
    goal: str,
    *,
    llm: Any,
    registry: ToolRegistry,
    max_steps: int = DEFAULT_MAX_STEPS,
    budget: Any = None,
    observer: Optional[Any] = None,
) -> Finding:
    """一路取证的小 loop：只看见自己的事实、只能用本路的只读工具。"""
    specs = [s for s in specs_for_prompt(registry) if s.get("name") in line.tools]
    facts: list[dict] = []
    finding = Finding(line=line.name)
    tokens = {"prompt": 0, "completion": 0}
    used: dict[str, int] = {}

    for i in range(1, max_steps + 1):
        last_step = i == max_steps
        system, user = _sub_messages(goal, line, facts, specs, last_step=last_step)
        try:
            out = await llm.chat_json(system, user, budget=budget)
        except Exception as exc:  # noqa: BLE001 —— 单路失败不拖垮整组
            finding.error = f"LLM 失败: {type(exc).__name__}: {exc}"
            return finding

        usage = out.get("_usage") if isinstance(out, dict) else None
        if isinstance(usage, dict):
            tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
            tokens["completion"] += int(usage.get("completion_tokens") or 0)

        action = str(out.get("action") or "").strip().lower()
        if action == "finish" or out.get("summary"):
            finding.summary = str(out.get("summary") or "")
            raw_ev = out.get("evidence") if isinstance(out.get("evidence"), list) else []
            finding.evidence = [
                {"field": str(e.get("field") or line.field), "quote": str(e.get("quote") or "")}
                for e in raw_ev if isinstance(e, dict)
            ]
            finding.class_guess = str(out.get("class_guess") or "").strip()
            try:
                finding.confidence = max(0.0, min(1.0, float(out.get("confidence") or 0)))
            except (TypeError, ValueError):
                finding.confidence = 0.0
            raw_un = out.get("unresolved")
            finding.unresolved = [str(x) for x in raw_un] if isinstance(raw_un, list) else []
            finding.usage = dict(tokens)
            if observer:
                observer("subagent", finding.to_dict())
            return finding

        name = str(out.get("tool") or "").strip()
        args = out.get("args") if isinstance(out.get("args"), dict) else {}
        if not name:
            finding.error = "子 agent 既没给工具也没 finish"
            break
        if name not in line.tools:
            finding.steps.append({"i": i, "tool": name, "args": args, "ok": False,
                                  "error": "不在本路可用范围"})
            facts.append({"field": "note", "value": f"工具 {name} 不在本路可用范围，"
                                                    f"只能用 {list(line.tools)}"})
            continue
        if name in used:
            # 单路取证第二次调同一工具，边际收益≈0：拦下并催 finish（真模型实测会这样打转）
            used[name] += 1
            finding.steps.append({"i": i, "tool": name, "args": args, "ok": False,
                                  "error": "重复调用同一工具"})
            facts.append({"field": "note", "value": f"{name} 已调过，结果在上方；"
                                                    f"不要重复确认，请直接 finish"})
            if last_step:
                return _fallback(finding, tokens, observer, "重复调用同一工具")
            continue
        used[name] = 1

        result = await call_tool(registry, name, args, full_ref=f"sub:{line.name}#step:{i}")
        finding.steps.append({"i": i, "tool": name, "args": args, "ok": result.ok,
                              "error": result.error, "ms": result.ms})
        if result.ok:
            facts.append({"field": name, "value": json.dumps(result.value, ensure_ascii=False,
                                                             default=str)})
            if last_step:
                return _fallback(finding, tokens, observer, "取到结果但未给出结构化结论")
        else:
            facts.append({"field": "note", "value": f"{name} 失败：{result.error[:80]}"})

    return _fallback(finding, tokens, observer, f"步数用完（{max_steps} 步）仍未给出结论")


def _fallback(finding: Finding, tokens: dict, observer: Optional[Any], why: str) -> Finding:
    """子 agent 没按协议 finish 时的兜底：如实标记未完成，而不是丢掉这条路。

    为什么不做「从工具结果里自动摘证据」：摘出来的片段可能正好是无关 JSON，
    交给主模型当证据用比没有更糟。这条路的价值由它调过的工具留在 steps 里。
    """
    finding.error = why
    finding.unresolved = list(finding.unresolved) + ["未形成结构化结论"]
    finding.usage = dict(tokens)
    if observer:
        observer("subagent", finding.to_dict())
    return finding


def merge_findings(findings: Sequence[Finding], run_id: str) -> tuple[list[dict], list[str]]:
    """合并成主 loop 能直接吃的事实段 + 冲突清单。

    - 每条 Finding 的 summary 落一条 field=subagent 的事实（回答「这路查到什么」）；
    - 每条 evidence 落一条**带原始 field** 的事实（kb/topology…），
      这样主模型引用证据时字段口径和单 agent 路径完全一致，门禁不用改；
    - 冲突（两路 class_guess 不同）不投票，落一条 field=conflict 的事实。
    """
    facts: list[dict] = []
    for f in findings:
        ref = f"{run_id}#sub:{f.line}"
        if f.error and not f.summary:
            facts.append({"field": "subagent", "value": f"{f.line}: 取证失败（{f.error}）",
                          "ref": ref})
            continue
        value = f"{f.line}: {f.summary}"
        if f.unresolved:
            value += f"（未解：{'；'.join(f.unresolved[:2])}）"
        facts.append({"field": "subagent", "value": value, "ref": ref})
        for j, ev in enumerate(f.evidence):
            quote = str(ev.get("quote") or "").strip()
            if not quote:
                continue
            facts.append({"field": str(ev.get("field") or f.line), "value": quote,
                          "ref": f"{ref}:{j + 1}"})

    guesses = {f.line: f.class_guess for f in findings if f.class_guess}
    conflicts: list[str] = []
    if len(set(guesses.values())) > 1:
        detail = "；".join(f"{line}={cls}" for line, cls in guesses.items())
        conflicts.append(f"多路取证结论不一致：{detail}")
        facts.append({"field": "conflict", "value": conflicts[-1],
                      "ref": f"{run_id}#conflict"})
    return facts, conflicts


async def gather_evidence(
    goal: str,
    *,
    llm: Any,
    registry: ToolRegistry,
    run_id: str,
    lines: Optional[Sequence[EvidenceLine]] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    budget: Any = None,
    observer: Optional[Any] = None,
) -> GatherResult:
    """并行跑所有可用线路；单路超时/失败不拖垮整组（结果里如实记录）。"""
    usable = available_lines(registry, lines)
    if len(usable) < 2:
        return GatherResult()

    async def _one(line: EvidenceLine) -> Finding:
        try:
            return await asyncio.wait_for(
                run_subagent(line, goal, llm=llm, registry=registry,
                             max_steps=max_steps, budget=budget, observer=observer),
                timeout=timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            f = Finding(line=line.name, error=f"超时（>{timeout_ms}ms）")
            if observer:
                observer("subagent", f.to_dict())
            return f

    findings = list(await asyncio.gather(*(_one(line) for line in usable)))

    # 注意：v1 的 tracing 用 contextvar 收集 span，gather 起的并发子任务里 span 会丢。
    # v2 不再依赖 contextvar——子 agent 的记录直接挂在 Finding 上随结果回传，
    # 由调用方合并，这是「span 会丢」这条老坑的结构性解法。
    facts, conflicts = merge_findings(findings, run_id)
    usage = {"prompt": 0, "completion": 0}
    for f in findings:
        usage["prompt"] += int(f.usage.get("prompt") or 0)
        usage["completion"] += int(f.usage.get("completion") or 0)
    return GatherResult(findings=findings, facts=facts, conflicts=conflicts, usage=usage)
