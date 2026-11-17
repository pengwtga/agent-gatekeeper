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
from oncall.context import build_messages
from oncall.policy import ALLOW, APPROVAL, decide
from oncall.sensors import check_finish
from oncall.tools import ToolCallRecord, call_tool, side_effect_of, specs_for_prompt

# 判定「无进展」前允许的重复次数：1 次可能是模型在换个参数试，2 次基本就是打转
MAX_REPEATS = 2

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
    """
    budget = RunBudget(budget_ms, clock, token_budget=token_budget)
    started = clock.now()
    facts: list[dict] = list(initial_facts or [])
    steps: list[ToolCallRecord] = list(initial_steps or [])
    seen: set[str] = {_fingerprint(s.tool, s.args) for s in steps if not s.skipped}
    repeats = 0
    tokens = {"prompt": 0, "completion": 0}
    tool_specs = specs_for_prompt(registry)

    def _ckpt(record: ToolCallRecord) -> None:
        """每步执行后、下一步装配前落库；崩溃最多丢一步。"""
        steps.append(record)
        if store is not None:
            store.checkpoint(run_id, steps, facts)

    if store is not None:
        store.ensure_run(run_id, goal)

    for i in range(start_i, start_i + max_steps):
        if budget.exhausted() or budget.token_exhausted():
            return _wrap(BUDGET_EXHAUSTED, steps, facts, started, clock, tokens,
                         reason=_budget_reason(budget, budget_ms, i))

        system, user = build_messages(goal, facts, steps, tool_specs,
                                      memory_block=memory_block)
        try:
            # RunBudget 与 v1 Budget 的接口同名（remaining_ms/slice/exhausted），
            # 直接传进去让 LLM 客户端把「剩余预算」当超时上界
            out = await llm.chat_json(system, user, budget=budget)
        except Exception as exc:  # noqa: BLE001 —— 容灾链全挂才会到这里
            return _wrap(FAILED, steps, facts, started, clock, tokens,
                         reason=f"LLM 调用失败: {type(exc).__name__}: {exc}")

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
            return _finish(out, steps, facts, started, clock, tokens)
        if action == "ask_human":
            return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                         reason=str(out.get("reason") or "模型主动转人工"))

        name = str(out.get("tool") or "").strip()
        args = out.get("args") if isinstance(out.get("args"), dict) else {}
        if not name:
            return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                         reason="模型没有给出工具名")

        fp = _fingerprint(name, args)
        if fp in seen:
            repeats += 1
            _ckpt(ToolCallRecord(i=i, tool=name, args=args, skipped="repeat"))
            if repeats >= MAX_REPEATS:
                return _wrap(NEEDS_HUMAN, steps, facts, started, clock, tokens,
                             reason=f"无进展：连续 {repeats + 1} 次调用 {name} 且参数相同")
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
                 reason=f"步数用完（{max_steps} 步）")


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


def _finish(out: dict, steps, facts, started, clock, tokens) -> Outcome:
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
    )


def _wrap(status, steps, facts, started, clock, tokens, *,
          conclusion="", root_cause_class="unknown", evidence=None,
          reason="", sensors=None, pending=None) -> Outcome:
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
    )
