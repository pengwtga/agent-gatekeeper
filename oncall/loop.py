"""agent loop：think → act → observe，四个独立的终止条件。

为什么四个条件都必要：
- 只靠「模型说完成」：它会自信地编一个结论（v1 早已验证置信度不校准）
- 只靠「步数上限」：把所有失败都归因成「跑太久」，分不清「打转」和「任务真复杂」
- 只靠「时间预算」：预算耗尽时可能已经查到关键事实，却被当成失败
- 无进展检测：最容易被漏掉，而它对应的正是最常见的事故——模型反复查同一样东西

设计取舍：状态里存**结构化事实**而不是消息列表。事实有 field + ref，
压缩、传感器、证据门禁都依赖这两样；消息列表只能靠再读一遍原文。
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.tools import ToolRegistry
from oncall.budget import RunBudget
from oncall.clock import Clock
from oncall.context import build_messages, default_class_catalog
from oncall.policy import ALLOW, APPROVAL, decide
from oncall.sensors import check_finish
from oncall.tools import (
    SideEffect,
    ToolCallRecord,
    call_tool,
    side_effect_of,
    specs_for_prompt,
)

# 判定「无进展」前允许的重复次数：1 次可能是模型在换个参数试，2 次基本就是打转
MAX_REPEATS = 2

# 证据来源的两种写法（prompt 约定 kb/history，历史数据里也有直接写工具名的）
KB_FIELDS = frozenset({"kb", "rag_search"})
HISTORY_FIELDS = frozenset({"history", "query_incidents"})

OK, NEEDS_HUMAN, BUDGET_EXHAUSTED, FAILED = "ok", "needs_human", "budget_exhausted", "failed"


@dataclass
class Outcome:
    status: str
    conclusion: str = ""
    root_cause_class: str = "unknown"
    evidence: list[dict] = field(default_factory=list)
    pending_approval: Optional[dict] = None
    reason: str = ""
    steps: list[ToolCallRecord] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    sensors: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    # F17：每次 finish 被门禁退回的记录（第几轮、错在哪、是否做了确定性补齐）
    repairs: list[dict] = field(default_factory=list)


def _fingerprint(name: str, args: dict) -> str:
    raw = f"{name}|{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _confidence(value: Any) -> Optional[float]:
    """模型自报的 0~1；缺失/非法返回 None（是格式问题，不是低置信）。"""
    if value is None or value == "":
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _fact_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _kb_supports_class(facts: list[dict], cls: str) -> bool:
    """KB 的命中条目里是否真的出现过这个根因类。

    为什么必须卡这一关（真模型消融跑出来的）：不加限制时，模型编一个类名、
    harness 就去查该类的历史工单——等于**给一个 KB 从未支持的断言制造第二来源**，
    门禁被"补齐"这件事本身绕过了。补齐的定位只能是「KB 已指向该类，再拿 history 印证」，
    不是「给任意断言找背书」。
    """
    for f in facts:
        if f.get("field") not in KB_FIELDS:
            continue
        value = f.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                # 被截断的长结果：退回字面匹配（宁可少补，不可错补）
                if cls and cls in f.get("value", ""):
                    return True
                continue
        if not isinstance(value, dict):
            continue
        for hit in value.get("hits") or []:
            if isinstance(hit, dict) and str(hit.get("class") or "") == cls:
                return True
    return False


async def run_loop(
    goal: str,
    *,
    llm: Any,
    registry: ToolRegistry,
    clock: Clock,
    run_id: str,
    max_steps: int = 12,
    budget_ms: int = 90_000,
    token_budget: Optional[int] = None,
    min_confidence: float = 0.7,
    max_finish_repairs: int = 1,
    auto_evidence: bool = True,
    observer: Optional[Callable[[str, dict], None]] = None,
    store: Any = None,
    start_i: int = 1,
    initial_facts: Optional[list[dict]] = None,
    initial_steps: Optional[list[ToolCallRecord]] = None,
    memory_block: str = "",
) -> Outcome:
    """store 传入时每步落库（F06 checkpoint）；initial_* 由 resume 重建状态。

    resume 语义（F06 §5）：从 facts 重建上下文继续跑到新预算耗尽；
    步号从 start_i 连续编号，steps 表 PK(run_id, i) 保证无空洞。
    无进展检测的 seen 从已执行过的步骤重建——resume 后重复同样的查询照样拦。

    F17 的两个收尾优化（只作用于「模型主动 finish 但没过门禁」这一种情形）：
    - max_finish_repairs：给 N 轮修正机会，把门禁错误当反馈写回上下文；
    - auto_evidence：缺口是「只差 history」时，harness 直接补一次 query_incidents。
    两者都不放宽门禁——证据条数、ref/quote、跨源要求一个字没改。
    """
    budget = RunBudget(budget_ms, clock, token_budget=token_budget)
    started = clock.now()
    facts: list[dict] = list(initial_facts or [])
    steps: list[ToolCallRecord] = list(initial_steps or [])
    seen: set[str] = {_fingerprint(s.tool, s.args) for s in steps if not s.skipped}
    repeats = 0
    repairs: list[dict] = []
    auto_filled = False
    feedback = ""
    tokens = {"prompt": 0, "completion": 0}
    tool_specs = specs_for_prompt(registry)

    def _ckpt(record: ToolCallRecord) -> None:
        """每步执行后、下一步装配前落库；崩溃最多丢一步。"""
        steps.append(record)
        if store is not None:
            store.checkpoint(run_id, steps, facts)

    async def _fill_history(cls_name: str, at_i: int) -> str:
        """确定性补齐：断言了根因却只有一个来源 → harness 自己补 history。

        为什么不让模型补：实测弱模型被门禁退回后再交的还是同一份单源结论
        （glm-4-flash 连续 4 轮如此）。「断言根因必须有第二个独立来源」是**规则**，
        规则就该由 harness 执行，而不是指望模型在下一次采样里碰对。

        注意边界：harness 只补**事实**，不替模型写 evidence。
        引用必须由模型自己给出——否则「证据」就成了系统自证。
        """
        tool = registry.get("query_incidents")
        if tool is None or side_effect_of("query_incidents") is not SideEffect.READ:
            return ""  # 没有这个工具 / 不是只读 → 不越权执行，退回纯反馈模式
        args = {"root_cause_class": cls_name, "limit": 3}
        record = ToolCallRecord(i=at_i, tool="query_incidents", args=args)
        result = await call_tool(registry, "query_incidents", args,
                                 full_ref=f"{run_id}#step:{at_i}")
        record.ok, record.error, record.ms = result.ok, result.error, result.ms
        record.truncated = result.truncated
        if result.ok:
            facts.append({"field": "query_incidents", "value": _fact_text(result.value),
                          "ref": f"{run_id}#step:{at_i}"})
        _ckpt(record)
        if observer:
            observer("tool", record.to_dict())
        if not result.ok:
            return f"（尝试自动补 history 失败：{record.error[:80]}，请换个方式取证）"
        return (f"harness 已自动调用 query_incidents(root_cause_class={cls_name})，"
                f"结果在事实段中 field=query_incidents、ref={run_id}#step:{at_i} 的那一条。"
                f"重新 finish 时补一条 evidence：field 写 history、ref 写这个 ref、"
                f"**quote 必须从该条事实的内容里摘 10~40 字原文**"
                f"（不要照抄这句说明，也不要写占位符——会把结论打成不可信）。")

    if store is not None:
        store.ensure_run(run_id, goal)

    for i in range(start_i, start_i + max_steps):
        if budget.exhausted() or budget.token_exhausted():
            return _wrap(BUDGET_EXHAUSTED, steps, facts, started, clock, tokens,
                         reason=_budget_reason(budget, budget_ms, i), repairs=repairs)

        system, user = build_messages(goal, facts, steps, tool_specs,
                                      memory_block=memory_block,
                                      class_catalog=default_class_catalog(),
                                      feedback=feedback)
        feedback = ""  # 反馈只喂一轮：模型没有据此改，就是它改不动，继续留着只会污染上下文
        try:
            # RunBudget 与 v1 Budget 的接口同名（remaining_ms/slice/exhausted），
            # 直接传进去让 LLM 客户端把「剩余预算」当超时上界
            out = await llm.chat_json(system, user, budget=budget)
        except Exception as exc:  # noqa: BLE001 —— 容灾链全挂才会到这里
            return _wrap(FAILED, steps, facts, started, clock, tokens,
                         reason=f"LLM 调用失败: {type(exc).__name__}: {exc}", repairs=repairs)

        usage = out.get("_usage") if isinstance(out, dict) else None
        if isinstance(usage, dict):
            p_tok, c_tok = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
            tokens["prompt"] += p_tok
            tokens["completion"] += c_tok
            budget.add_usage(p_tok, c_tok)  # 每次调用后记账；下一步开始时统一检查

        if observer:
            observer("llm", out if isinstance(out, dict) else {"raw": str(out)})

        action = _action_of(out)
        if action == "finish":
            verdict = _finish(out, steps, facts, started, clock, tokens, repairs=repairs)
            errors = "；".join(verdict.sensors.get("errors") or []) or verdict.reason
            fields = set(verdict.sensors.get("evidence_fields") or [])
            # 补齐与反馈是**两个独立预算**：补齐是确定性动作（成本固定、只做一次），
            # 反馈轮是给模型的采样机会（有上限）。混用一个预算会把「先乱答一次 → 查了
            # 一次 → 仍是单源」这种最常见的真实轨迹挡在门外（SH-01 就死在这里）。
            can_fill = (auto_evidence and not auto_filled
                        and verdict.root_cause_class not in ("", "unknown")
                        and (fields & KB_FIELDS) and not (fields & HISTORY_FIELDS)
                        and _kb_supports_class(facts, str(verdict.root_cause_class)))
            if verdict.status == OK or (len(repairs) >= max_finish_repairs and not can_fill):
                return verdict

            # ── 门禁退回：先做确定性补齐，再把缺口当反馈喂回去 ──
            filled = ""
            if can_fill:
                auto_filled = True
                filled = await _fill_history(str(verdict.root_cause_class), i)
            repairs.append({
                "attempt": len(repairs) + 1,
                "at_step": i,
                "errors": verdict.sensors.get("errors") or [verdict.reason],
                "auto_filled": bool(filled),
                "root_cause_class": verdict.root_cause_class,
            })
            if observer:
                observer("repair", repairs[-1])
            feedback = (
                f"上一次 finish 被判不合格：{errors}。"
                f"{filled}"
                "请基于「已确认事实」重新 finish：断言根因时 evidence 必须同时含 "
                "kb（rag_search）与 history（query_incidents）两条，quote 用事实段里的原文；"
                "确实补不齐就 finish 且 root_cause_class=unknown，并在结论里说明缺什么。"
            )
            if budget.exhausted() or budget.token_exhausted():
                return verdict  # 没预算修了：把这份「过门禁失败」的结论原样交出，供人复核
            continue
        if action == "ask_human":
            return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                         reason=str(out.get("reason") or "模型主动转人工"), repairs=repairs)

        name = str(out.get("tool") or "").strip()
        args = out.get("args") if isinstance(out.get("args"), dict) else {}
        if not name:
            return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                         reason="模型没有给出工具名", repairs=repairs)

        fp = _fingerprint(name, args)
        if fp in seen:
            repeats += 1
            _ckpt(ToolCallRecord(i=i, tool=name, args=args, skipped="repeat"))
            if repeats >= MAX_REPEATS:
                return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                             reason=f"无进展：连续 {repeats + 1} 次调用 {name} 且参数相同",
                             repairs=repairs)
        else:
            seen.add(fp)
            repeats = 0

            record = ToolCallRecord(i=i, tool=name, args=args)
            if registry.get(name) is None:
                # 白名单关：模型编造的工具名到此为止，但不终止——它可以改
                record.skipped = "unknown_tool"
                record.error = f"工具 {name} 不在注册表中"
                _ckpt(record)
                continue

            decision = decide(name, side_effect_of(name), _confidence(out.get("confidence")),
                              min_confidence=min_confidence)
            if decision.kind == APPROVAL:
                record.skipped = "approval"
                _ckpt(record)
                approval_id = f"{run_id}:{i}"
                if store is not None:
                    # 状态落库不落内存（F05 §3）：人可能几小时后才批，进程不能挂着等
                    store.record_approval(approval_id, run_id, name, args,
                                          why=str(out.get("thought") or ""),
                                          risk=decision.risk, requires=decision.requires)
                return _wrap(
                    NEEDS_HUMAN, steps, facts, started, clock, tokens,
                    reason=decision.reason,
                    pending={
                        "id": approval_id, "run_id": run_id, "tool": name, "args": args,
                        "why": str(out.get("thought") or ""), "risk": decision.risk,
                        "requires": decision.requires,
                    },
                    repairs=repairs,
                )
            if decision.kind != ALLOW:
                record.skipped = "denied"
                record.error = decision.reason
                _ckpt(record)
                continue

            result = await call_tool(registry, name, args, full_ref=f"{run_id}#step:{i}")
            record.ok, record.error, record.ms = result.ok, result.error, result.ms
            record.truncated = result.truncated

            if result.ok:
                facts.append({
                    "field": name,
                    "value": _fact_text(result.value),
                    "ref": f"{run_id}#step:{i}",
                })
            # 先记事实再落 checkpoint：保证「step 记了但事实没记」不会发生
            _ckpt(record)
            if observer:
                observer("tool", record.to_dict())

    return _wrap(BUDGET_EXHAUSTED, steps, facts, started, clock, tokens,
                 reason=f"步数用完（{max_steps} 步）", repairs=repairs)


def _budget_reason(budget: RunBudget, budget_ms: int, step: int) -> str:
    if budget.token_exhausted():
        return (f"token 预算 {budget.token_budget} 用尽"
                f"（累计 {budget.tokens_used}，第 {step} 步前）")
    return f"预算 {budget_ms}ms 耗尽（第 {step} 步前）"


def _action_of(out: dict) -> str:
    action = str(out.get("action") or "").strip().lower()
    if action in ("tool", "finish", "ask_human"):
        return action
    # 模型偶尔忘记写 action：按内容推断，比直接失败更有用
    if out.get("conclusion"):
        return "finish"
    if out.get("tool"):
        return "tool"
    return "ask_human"


def _finish(out: dict, steps, facts, started, clock, tokens, *, repairs=None) -> Outcome:
    evidence = out.get("evidence") if isinstance(out.get("evidence"), list) else []
    root_cause_class = str(out.get("root_cause_class") or "unknown")
    report = check_finish(str(out.get("conclusion") or ""), evidence,
                          requires_cross_source=root_cause_class != "unknown")
    status = OK if report.ok else NEEDS_HUMAN
    return _wrap(
        status, steps, facts, started, clock, tokens,
        conclusion=str(out.get("conclusion") or ""),
        root_cause_class=root_cause_class,
        evidence=evidence,
        reason="" if report.ok else "；".join(report.errors),
        sensors=report.to_dict(),
        repairs=repairs,
    )


def _wrap(status, steps, facts, started, clock, tokens, *,
          conclusion="", root_cause_class="unknown", evidence=None,
          reason="", sensors=None, pending=None, repairs=None) -> Outcome:
    return Outcome(
        status=status,
        conclusion=conclusion,
        root_cause_class=root_cause_class,
        evidence=list(evidence or []),
        pending_approval=pending,
        reason=reason,
        steps=steps,
        facts=facts,
        sensors=sensors or {},
        usage={
            "steps": len(steps),
            "tool_calls": sum(1 for s in steps if not s.skipped),
            "wall_ms": int((clock.now() - started) * 1000),
            "prompt_tokens": tokens["prompt"],
            "completion_tokens": tokens["completion"],
        },
        repairs=list(repairs or []),
    )
