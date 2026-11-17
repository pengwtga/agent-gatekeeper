"""I2 的核心验收（F05 §7 / F06 §8）：审批状态机、幂等重放、断点续跑。

全部桩 LLM + 临时 SQLite（tmp_path），零网络毫秒级；风格与 v1 一致用 asyncio.run。
三条设计断言贯穿所有用例：
1. 危险动作 handler 一次都不许被调，直到人批
2. 重复执行不重复副作用（applied_keys）——恢复点 ≠ 幂等点
3. resume 后 steps 连续无空洞，facts 不丢
"""

import asyncio

import pytest

from oncall.api import Deps, RunOptions, _execute_approved, approve_incident, resume_incident, run_incident
from oncall.store import RunStore, idempotency_key
from oncall.tools import SIDE_EFFECTS, SideEffect, ToolCallRecord
from tests.v2.stubs import FINISH_OK, CountingTool, FakeClock, ScriptedLLM, build_registry

# 未在 SIDE_EFFECTS 声明的工具按 WRITE 处理（fail-closed），正好当测试用的写操作
WRITE_TOOL = "update_config"
WRITE_PARAMS = {"k": {"type": "string", "required": True}}
WRITE_INTENT = {"action": "tool", "tool": WRITE_TOOL, "args": {"k": "callback_url"},
                "thought": "旧域名导致回调 404", "confidence": 0.95}


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.sqlite3").init()
    yield s
    s.close()


def _deps(llm, registry, store, clock=None):
    return Deps(llm=llm, registry=registry, clock=clock or FakeClock(), store=store)


def test_write_tool_persists_approval_and_approve_resumes(store):
    """挂起落库 → 人批 → resume：handler 恰好执行一次，steps 连续。"""
    llm = ScriptedLLM([WRITE_INTENT, FINISH_OK])
    tool = CountingTool({"updated": True})
    reg = build_registry((WRITE_TOOL, WRITE_PARAMS, tool))

    first = asyncio.run(run_incident("把回调地址改掉", deps=_deps(llm, reg, store)))

    assert first.status == "needs_human"
    assert tool.n == 0, "人没批之前绝不能执行"
    assert first.outcome.pending_approval["requires"] == 1
    run_id = first.run_id
    assert store.load_run(run_id)["status"] == "pending_approval"

    result = asyncio.run(approve_incident(run_id, "allow", deps=_deps(llm, reg, store)))

    assert result.status == "ok"
    assert tool.n == 1, "批准后恰好执行一次"
    is_ = [s.i for s in result.outcome.steps]
    assert is_ == sorted(is_) and len(set(is_)) == len(is_), "steps 连续无空洞"
    assert store.load_run(run_id)["status"] == "ok"


def test_deny_records_rejection_and_run_completes_without_execution(store):
    """deny → 决策留痕（who/status），续跑后正常收尾，handler 仍为 0。"""
    llm = ScriptedLLM([WRITE_INTENT, FINISH_OK])
    tool = CountingTool()
    reg = build_registry((WRITE_TOOL, WRITE_PARAMS, tool))

    first = asyncio.run(run_incident("把回调地址改掉", deps=_deps(llm, reg, store)))
    result = asyncio.run(approve_incident(first.run_id, "deny",
                                          deps=_deps(llm, reg, store), decided_by="oncall-zhang"))

    assert result.status == "ok"
    assert tool.n == 0
    denied = [s for s in result.outcome.steps if s.skipped == "denied"]
    assert denied and "未执行" in denied[0].error
    row = store._db().execute("SELECT status, decided_by FROM approvals WHERE run_id=?",
                              (first.run_id,)).fetchone()
    assert row == ("denied", "oncall-zhang"), "who / what 决策必须留痕"


def test_approval_timeout_defaults_to_deny(tmp_path):
    """超时默认拒绝，不是默认允许（F05 §3）。timeout=0 → 立即过期。"""
    store = RunStore(tmp_path / "t.sqlite3", approval_timeout_s=0).init()
    llm = ScriptedLLM([WRITE_INTENT, FINISH_OK])
    tool = CountingTool()
    reg = build_registry((WRITE_TOOL, WRITE_PARAMS, tool))

    first = asyncio.run(run_incident("把回调地址改掉", deps=_deps(llm, reg, store)))
    result = asyncio.run(resume_incident(first.run_id, deps=_deps(llm, reg, store)))

    assert tool.n == 0
    assert store.current_approval(first.run_id)["status"] == "expired"
    assert result.status == "ok"


def test_destructive_requires_two_approvers(store, monkeypatch):
    """DESTRUCTIVE：单人 approve 只记 1/2 仍挂起，第二人批了才执行。"""
    monkeypatch.setitem(SIDE_EFFECTS, "purge_queue", SideEffect.DESTRUCTIVE)
    intent = {"action": "tool", "tool": "purge_queue", "args": {"q": "dead"},
              "confidence": 0.95}
    llm = ScriptedLLM([intent, FINISH_OK])
    tool = CountingTool({"purged": True})
    reg = build_registry(("purge_queue", {"q": {"type": "string"}}, tool))

    first = asyncio.run(run_incident("清理死队列", deps=_deps(llm, reg, store)))
    assert first.outcome.pending_approval["requires"] == 2

    half = asyncio.run(approve_incident(first.run_id, "allow", deps=_deps(llm, reg, store)))
    assert half.status == "needs_human", "只有 1/2 票，必须继续挂起"
    assert tool.n == 0

    done = asyncio.run(approve_incident(first.run_id, "allow",
                                        deps=_deps(llm, reg, store), decided_by="second-reviewer"))
    assert done.status == "ok"
    assert tool.n == 1


def test_replay_does_not_repeat_side_effect(store):
    """幂等键（F05 §5）：同一批准执行两次，handler 只调 1 次，重放有痕迹。

    直接打 _execute_approved——这模拟的正是「执行成功但 checkpoint 前崩溃，
    resume 后同一审批被再次执行」的场景。
    """
    llm = ScriptedLLM([WRITE_INTENT])
    tool = CountingTool({"updated": True})
    reg = build_registry((WRITE_TOOL, WRITE_PARAMS, tool))
    deps = _deps(llm, reg, store)

    first = asyncio.run(run_incident("把回调地址改掉", deps=deps))
    ap = store.current_approval(first.run_id)
    store.decide(first.run_id, "allow", "cli")

    rec1, fact1 = asyncio.run(_execute_approved(first.run_id, ap, deps, 2))
    rec2, fact2 = asyncio.run(_execute_approved(first.run_id, ap, deps, 2))

    assert rec1.skipped is None and rec1.ok and fact1 is not None
    assert rec2.skipped == "replayed" and fact2 is None, "重放必须被幂等键拦下"
    assert tool.n == 1
    key = idempotency_key(first.run_id, WRITE_TOOL, WRITE_INTENT["args"])
    assert store.applied(key) is not None


def test_resume_after_budget_exhaustion_keeps_facts_and_step_order(store):
    """预算耗尽 ≠ 白跑：resume 后 facts 保留、步号接着排、结论正常产出。"""
    clock = FakeClock()
    step_tool = {"action": "tool", "tool": "kb_stats", "args": {"q": "callback"},
                 "confidence": 0.9, "_usage": {"prompt_tokens": 4000, "completion_tokens": 100}}
    llm = ScriptedLLM([step_tool, FINISH_OK])
    tool = CountingTool({"total": 150}, clock=clock, cost_ms=60_000)
    reg = build_registry(("kb_stats", {"q": {"type": "string"}}, tool))

    first = asyncio.run(run_incident("排查回调丢失", deps=_deps(llm, reg, store, clock),
                                     opts=RunOptions(budget_ms=30_000)))
    assert first.status == "budget_exhausted"
    assert len(first.outcome.facts) == 1

    result = asyncio.run(resume_incident(first.run_id, deps=_deps(llm, reg, store, clock),
                                         opts=RunOptions(budget_ms=90_000)))

    assert result.status == "ok"
    assert tool.n == 1, "resume 不能重查已确认过的事实"
    # finish 不产生 step 记录；resume 的贡献是「接着跑完」，不是再查一遍
    assert [s.i for s in result.outcome.steps] == [1]
    assert llm.calls == 2, "resume 确实发起了新的 LLM 决策（带着旧事实）"
    assert result.outcome.facts[0]["ref"].endswith("#step:1")


# ── F04 压缩：早期步骤移出上下文，事实表兜底 ──

def test_compaction_keeps_recent_steps_and_fact_table():
    from oncall.context import build_messages

    steps = [ToolCallRecord(i=i, tool=f"tool_{i}", args={}) for i in range(1, 9)]
    facts = [{"field": "kb_stats", "value": "total=150", "ref": "r#step:1"}]
    system, user = build_messages("排查", facts, steps, build_registry().specs())

    assert "[8]" in user and "[5]" not in user, "只保留最近 3 步"
    assert "已压缩" in user, "被移出的步骤要说明去向"
    assert "total=150" in user, "事实表全量保留——压缩不丢结论"
    assert "SSRM" in system, "规则段钉在头部，任何一轮都不能少"


# ── 存储层 roundtrip ──

def test_store_roundtrip_preserves_records(tmp_path):
    """checkpoint → load：steps/facts 原样回来，这是 resume 正确性的前提。"""
    store = RunStore(tmp_path / "rt.sqlite3").init()
    store.ensure_run("r-1", "排查")
    steps = [
        ToolCallRecord(i=1, tool="kb_stats", args={"q": "a"}, ok=True, ms=5),
        ToolCallRecord(i=2, tool="update_config", args={"k": "v"}, skipped="approval"),
    ]
    facts = [{"field": "kb_stats", "value": "total=150", "ref": "r-1#step:1"}]
    store.checkpoint("r-1", steps, facts)

    run = store.load_run("r-1")
    assert run["goal"] == "排查"
    assert [(s.i, s.tool, s.skipped) for s in run["steps"]] == [(1, "kb_stats", None),
                                                               (2, "update_config", "approval")]
    assert run["facts"] == facts
