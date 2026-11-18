"""唯一的运行入口：`run_incident()`。

CLI、进程内调用、MCP server、将来的 HTTP 都调这一个函数，谁都不许在入口层
自己写一遍循环。这样「业务层怎么用 CLI」这个问题就不存在了——业务层不经过 CLI，
它直接调这里；CLI 只是一层 argv → 参数 → 打印。

依赖全部从 `Deps` 注入（LLM / 工具注册表 / 时钟）。不读全局单例的原因：
1. 业务方要把审批通道换成 IM 审批、把 LLM 换成内部网关，改参数就行
2. 测试要换脚本化 LLM 和假时钟，读 `os.environ` 的写法根本做不到
"""

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.tools import ToolRegistry
from oncall.clock import Clock, SystemClock
from oncall.loop import BUDGET_EXHAUSTED, FAILED, NEEDS_HUMAN, OK, Outcome, run_loop
from oncall.store import RunStore, idempotency_key
from oncall.tools import ToolCallRecord, call_tool

# 退出码语义（F01 §5）：把「需人工」和「失败」分开是产品语义，不是技术细节
EXIT_CODES = {OK: 0, NEEDS_HUMAN: 2, BUDGET_EXHAUSTED: 3, FAILED: 1}

# run 的终态：resume 拒绝重跑已完成的 run（结论已出，续跑只会烧 token）
_TERMINAL = {OK, FAILED}


@dataclass
class RunOptions:
    max_steps: int = 12
    budget_ms: int = 90_000
    # token 预算：本次 run 的 prompt+completion 总量上限。12 步 × 每步约 4~6K，
    # 正常排障 30~50K 就够，100K 是兜底不是常态（到顶说明在打转，步数/无进展会先拦）。
    max_tokens: int = 100_000
    min_confidence: float = 0.7
    domain: str = "ssrm"
    # F17：finish 被门禁退回后给几轮修正（0 = 关闭，退回即转人工，即 I4 之前的行为）
    max_finish_repairs: int = 1
    # F17：缺口只差 history 时，由 harness 确定性补一次 query_incidents（不放宽任何门禁）
    auto_evidence: bool = True
    # F10：技能索引常驻、命中才注入正文（关掉 = 消融组，回到纯 prompt 行为）
    use_skills: bool = True
    # F11：并行取证触发模式。auto=多源线索才 spawn；off=关；force=总是（消融/调试用）
    subagents: str = "auto"


@dataclass
class Deps:
    llm: Any
    registry: ToolRegistry
    clock: Clock = field(default_factory=SystemClock)
    # 观测钩子：CLI 的 --verbose 用它把每一步的原始输出打到 stderr。
    # 用回调而不是 print：loop 不该知道「输出到哪」，测试里也可以收进列表断言。
    observer: Optional[Callable[[str, dict], None]] = None
    # 持久化（F05/F06）：None = 不落库（I1 兼容）。审批与 resume 必须有它。
    store: Optional[RunStore] = None
    # 事故记忆（F15）：None = 关闭。写入纪律在 _remember()，不在调用方。
    memory: Any = None
    # 技能库（F10）：None = 关闭（无索引、无 load_skill 工具）
    skills: Any = None


@dataclass
class RunResult:
    run_id: str
    goal: str
    outcome: Outcome

    @property
    def status(self) -> str:
        return self.outcome.status

    def to_dict(self) -> dict[str, Any]:
        o = self.outcome
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": o.status,
            "conclusion": o.conclusion,
            "root_cause_class": o.root_cause_class,
            "evidence": o.evidence,
            "reason": o.reason,
            "steps": [s.to_dict() for s in o.steps],
            "usage": o.usage,
            "sensors": o.sensors,
            "pending_approval": o.pending_approval,
            "repairs": o.repairs,
            "subagents": o.subagents,
        }


def new_run_id() -> str:
    return f"r-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


async def run_incident(goal: str, *, deps: Deps, opts: Optional[RunOptions] = None) -> RunResult:
    opts = opts or RunOptions()
    run_id = new_run_id()
    # F15：run 开始时召回「历史同类」装配进上下文（此时簇还不知道，按现象指纹匹配）。
    # getattr 兼容鸭子类型的 Deps（测试用 SimpleNamespace 造依赖时可以不带 memory 字段）
    memory = getattr(deps, "memory", None)
    memory_block = ""
    if memory is not None:
        memory_block = memory.render_block(goal, domain=opts.domain)
    outcome = await run_loop(
        goal,
        llm=deps.llm,
        registry=deps.registry,
        clock=deps.clock,
        run_id=run_id,
        max_steps=opts.max_steps,
        budget_ms=opts.budget_ms,
        token_budget=opts.max_tokens,
        min_confidence=opts.min_confidence,
        max_finish_repairs=opts.max_finish_repairs,
        auto_evidence=opts.auto_evidence,
        observer=deps.observer,
        store=deps.store,
        memory_block=memory_block,
        skills=getattr(deps, "skills", None),
        use_skills=opts.use_skills,
        subagents=opts.subagents,
    )
    if deps.store is not None:
        _persist_status(deps.store, run_id, outcome)
    _remember(memory, opts.domain, goal, run_id, outcome)
    return RunResult(run_id=run_id, goal=goal, outcome=outcome)


def _remember(memory: Any, domain: str, goal: str, run_id: str, outcome: Outcome) -> None:
    """事故记忆的写入闸门（F15 §3 纪律 1）：只写已验证的。

    status=ok 意味着模型 finish 且过了传感器（S1/S2/S3）——这才是「经验」；
    needs_human / failed / budget_exhausted 一律不写，未验证结论进记忆比没记忆更糟。
    """
    if memory is None:
        return
    if outcome.status != OK or outcome.root_cause_class in ("", "unknown"):
        return
    memory.record(
        domain=domain,
        cluster=outcome.root_cause_class,
        fingerprint=goal,
        conclusion=outcome.conclusion,
        action=outcome.conclusion,  # v2 的结论 = 根因 + 建议动作一句话，先合写；拆字段留给 F04 扩展
        outcome="ok",
        run_id=run_id,
    )


def _persist_status(store: RunStore, run_id: str, outcome: Outcome) -> None:
    """run 状态的落库点统一放这里，loop 只管跑——状态机不要散在循环体里。"""
    status = "pending_approval" if outcome.pending_approval else outcome.status
    # usage 一并落库（F14）：token/步数是评测与面板的主指标，不能只活在内存里
    store.set_status(run_id, status, conclusion=outcome.conclusion,
                     reason=outcome.reason, usage=outcome.usage)


def _pending_outcome(run: dict, approval: Optional[dict]) -> Outcome:
    """从存储重建「仍在等审批」的 Outcome，不发起任何 LLM 调用。"""
    steps: list[ToolCallRecord] = run["steps"]
    pending = None
    if approval is not None:
        pending = {"id": approval["id"], "run_id": run["run_id"], "tool": approval["tool"],
                   "args": approval["args"], "why": approval["why"],
                   "risk": approval["risk"], "requires": approval["requires"]}
    return Outcome(
        status=NEEDS_HUMAN,
        conclusion=run.get("conclusion", ""),
        steps=steps,
        facts=run["facts"],
        pending_approval=pending,
        reason="等待审批" if pending else "审批已过期（默认拒绝），可 resume 继续",
        usage={"steps": len(steps), "tool_calls": sum(1 for s in steps if not s.skipped)},
    )


def _runnable_run_id(deps: Deps, run_id: str) -> dict:
    if deps.store is None:
        raise RuntimeError("resume/approve 需要持久化存储（Deps.store）")
    run = deps.store.load_run(run_id)
    if run is None:
        raise RuntimeError(f"run {run_id} 不存在")
    if run["status"] in _TERMINAL:
        raise RuntimeError(f"run {run_id} 已是终态（{run['status']}），无需 resume")
    return run


def _fact_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


async def _execute_approved(run_id: str, approval: dict, deps: Deps,
                            i: int) -> tuple[ToolCallRecord, Optional[dict]]:
    """执行已批准的写操作，带幂等键（F05 §5）：恢复点 ≠ 幂等点。

    返回 (步骤记录, 事实)。replayed/失败时事实为 None——只有成功执行才产生事实。
    """
    record = ToolCallRecord(i=i, tool=approval["tool"], args=approval["args"])
    key = idempotency_key(run_id, approval["tool"], approval["args"])
    prev = deps.store.applied(key)
    if prev is not None:
        # 崩溃前已执行过：直接跳过，绝不重复副作用
        record.skipped = "replayed"
        record.error = "幂等键命中，本次未重复执行"
        return record, None

    result = await call_tool(deps.registry, approval["tool"], approval["args"],
                             full_ref=f"{run_id}#step:{i}")
    record.ok, record.error, record.ms = result.ok, result.error, result.ms
    record.truncated = result.truncated
    if not result.ok:
        return record, None
    fact = {"field": approval["tool"], "value": _fact_text(result.value),
            "ref": f"{run_id}#step:{i}"}
    deps.store.mark_applied(key, run_id, {"ok": True, "value": fact["value"]})
    return record, fact


async def resume_incident(run_id: str, *, deps: Deps, opts: Optional[RunOptions] = None) -> RunResult:
    """三种断点三种处理（F06 §5）：审批挂起 / 预算耗尽 / 进程崩溃后重放。"""
    run = _runnable_run_id(deps, run_id)
    opts = opts or RunOptions()
    store = deps.store
    steps: list[ToolCallRecord] = run["steps"]
    facts: list[dict] = run["facts"]
    start_i = (steps[-1].i + 1) if steps else 1

    approval = store.current_approval(run_id) if run["status"] == "pending_approval" else None

    if run["status"] == "pending_approval" and (approval is None or approval["status"] == "pending"):
        # 未决审批：不动，也不烧 token——人没批之前什么都不要做
        if deps.observer:
            deps.observer("resume", {"event": "still_pending"})
        return RunResult(run_id=run_id, goal=run["goal"],
                         outcome=_pending_outcome(run, approval))

    if approval is not None and approval["status"] in ("approved", "denied", "expired"):
        if approval["status"] == "approved":
            record, fact = await _execute_approved(run_id, approval, deps, start_i)
            if fact is not None:
                facts.append(fact)
        else:
            record = ToolCallRecord(i=start_i, tool=approval["tool"], args=approval["args"],
                                    skipped="denied",
                                    error=f"审批结果 {approval['status']}，未执行")
        steps.append(record)
        store.checkpoint(run_id, steps, facts)
        start_i += 1

    outcome = await run_loop(
        run["goal"],
        llm=deps.llm,
        registry=deps.registry,
        clock=deps.clock,
        run_id=run_id,
        max_steps=opts.max_steps,
        budget_ms=opts.budget_ms,
        token_budget=opts.max_tokens,
        min_confidence=opts.min_confidence,
        max_finish_repairs=opts.max_finish_repairs,
        auto_evidence=opts.auto_evidence,
        observer=deps.observer,
        store=store,
        start_i=start_i,
        initial_facts=facts,
        initial_steps=steps,
        memory_block=getattr(deps, "memory", None).render_block(run["goal"], domain=opts.domain)
        if getattr(deps, "memory", None) is not None else "",
        skills=getattr(deps, "skills", None),
        use_skills=opts.use_skills,
        subagents=opts.subagents,
    )
    _persist_status(store, run_id, outcome)
    _remember(getattr(deps, "memory", None), opts.domain, run["goal"], run_id, outcome)
    return RunResult(run_id=run_id, goal=run["goal"], outcome=outcome)


async def approve_incident(run_id: str, decision: str, *, deps: Deps,
                           opts: Optional[RunOptions] = None,
                           decided_by: str = "cli") -> RunResult:
    """记录审批决定，然后按 F06 的 resume 语义续跑。"""
    run = _runnable_run_id(deps, run_id)
    store = deps.store
    ap = store.current_approval(run_id)
    if ap is None or ap["status"] != "pending":
        raise RuntimeError(f"run {run_id} 没有待决审批（当前：{ap['status'] if ap else '无记录'}）")

    ap = store.decide(run_id, decision, decided_by)
    if ap is not None and ap["status"] == "pending":
        # 双人确认（requires=2）：单人 approve 只记 1/2，不执行
        return RunResult(run_id=run_id, goal=run["goal"],
                         outcome=_pending_outcome(run, ap))
    return await resume_incident(run_id, deps=deps, opts=opts)


def exit_code(result: RunResult) -> int:
    return EXIT_CODES.get(result.status, 1)
