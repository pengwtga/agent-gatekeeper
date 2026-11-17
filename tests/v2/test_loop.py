"""loop 的六个必测场景（对应六类线上事故），零网络、毫秒级。

用 `asyncio.run` 而不是 pytest.mark.asyncio：与 v1 的测试风格一致（tests/test_evidence_gate.py），
也避免异步插件版本差异带来的隐性行为。
"""

import asyncio

from oncall.api import Deps, RunOptions, run_incident
from oncall.tools import SIDE_EFFECTS, SideEffect
from tests.v2.stubs import FINISH_OK, CountingTool, FakeClock, ScriptedLLM, build_registry


def _run(goal: str, *, llm, registry, clock=None, **opts):
    deps = Deps(llm=llm, registry=registry, clock=clock or FakeClock())
    return asyncio.run(run_incident(goal, deps=deps, opts=RunOptions(**opts)))


def test_single_hop_finish_is_ok():
    """正常路径：查一次 → 给结论 → 传感器通过。"""
    llm = ScriptedLLM([{"action": "tool", "tool": "kb_stats", "args": {}, "confidence": 0.9}, FINISH_OK])
    reg = build_registry(("kb_stats", {}, CountingTool({"total": 150})))

    result = _run("知识库有多少条", llm=llm, registry=reg)

    assert result.status == "ok"
    assert len(result.outcome.steps) == 1
    assert result.outcome.facts[0]["field"] == "kb_stats"
    assert result.outcome.facts[0]["ref"].endswith("#step:1")
    assert result.to_dict()["usage"]["steps"] == 1


def test_repeated_call_aborts_as_no_progress():
    """打转：同工具同参数 → 不重复执行，第 3 步判定无进展退出。"""
    repeat = {"action": "tool", "tool": "kb_stats", "args": {}, "confidence": 0.9}
    llm = ScriptedLLM([repeat, repeat, repeat])
    tool = CountingTool({"total": 150})
    reg = build_registry(("kb_stats", {}, tool))

    result = _run("某问题", llm=llm, registry=reg)

    assert result.status == "needs_human"
    assert "无进展" in result.outcome.reason
    assert tool.n == 1, "重复调用不应该被执行第二次"
    assert [s.skipped for s in result.outcome.steps[1:]] == ["repeat", "repeat"]


def test_budget_exhausted_stops_before_next_llm_call():
    """预算耗尽后不再发起 LLM 调用（真等 90s 的测试是没法跑的，所以时钟注入）。"""
    clock = FakeClock()
    tool = CountingTool({"ok": True}, clock=clock, cost_ms=60_000)
    llm = ScriptedLLM([{"action": "tool", "tool": "kb_stats", "args": {}, "confidence": 0.9}])
    reg = build_registry(("kb_stats", {}, tool))

    result = _run("某问题", llm=llm, registry=reg, clock=clock, budget_ms=30_000)

    assert result.status == "budget_exhausted"
    assert llm.calls == 1, "预算耗尽后不能再调 LLM"
    assert "预算" in result.outcome.reason


def test_write_tool_requires_approval_and_is_not_executed():
    """写操作：挂起等审批，handler 一次都不许被调。"""
    llm = ScriptedLLM([{"action": "tool", "tool": "restart_service", "args": {"svc": "ssrm-api"},
                        "confidence": 0.95}])
    tool = CountingTool({"restarted": True})
    reg = build_registry(("restart_service", {"svc": {"type": "string", "required": True}}, tool))

    result = _run("把 ssrm-api 重启一下", llm=llm, registry=reg)

    assert result.status == "needs_human"
    assert tool.n == 0, "危险动作绝不能被执行"
    assert result.outcome.pending_approval is not None
    assert result.outcome.pending_approval["tool"] == "restart_service"
    assert result.outcome.steps[0].skipped == "approval"


def test_low_confidence_is_denied_then_finishes():
    """低置信度的工具选择不执行，模型可以改用别的说法（这里直接结束）。"""
    llm = ScriptedLLM([
        {"action": "tool", "tool": "kb_stats", "args": {}, "confidence": 0.2},
        FINISH_OK,
    ])
    tool = CountingTool({"total": 150})
    reg = build_registry(("kb_stats", {}, tool))

    result = _run("某问题", llm=llm, registry=reg)

    assert tool.n == 0
    assert result.outcome.steps[0].skipped == "denied"
    assert result.status == "ok"


def test_single_field_evidence_is_not_ok():
    """证据只有一个字段 → 传感器 S2 拦截，结论标记为不可信。"""
    weak = dict(FINISH_OK, evidence=[{"field": "log", "ref": "a", "quote": "404"}])
    llm = ScriptedLLM([weak])
    reg = build_registry(("kb_stats", {}, CountingTool()))

    result = _run("某问题", llm=llm, registry=reg)

    assert result.status == "needs_human"
    assert any("S2" in e for e in result.outcome.sensors["errors"])
    assert result.outcome.evidence, "结论仍要保留，供人复核"


def test_single_field_evidence_is_ok_for_plain_lookup():
    """纯查询（没断言根因）只要求一个来源：唯一权威就是那一个工具，
    硬凑第二个字段只会逼模型编证据。"""
    lookup = {
        "action": "finish",
        "conclusion": "知识库共 150 条工单，五类各 30 条",
        "root_cause_class": "unknown",
        "evidence": [{"field": "kb_stats", "ref": "r#step:1", "quote": "total=150"}],
        "confidence": 0.8,
    }
    llm = ScriptedLLM([lookup])
    reg = build_registry(("kb_stats", {}, CountingTool()))

    result = _run("知识库有多少条", llm=llm, registry=reg)

    assert result.status == "ok"


def test_missing_confidence_on_read_tool_still_runs():
    """实测 glm-4-flash 会漏掉 confidence 字段：这是格式问题，不是低置信。
    当成 0 会让弱模型永远执行不了任何工具。"""
    llm = ScriptedLLM([{"action": "tool", "tool": "kb_stats", "args": {}}, FINISH_OK])
    tool = CountingTool({"total": 150})
    reg = build_registry(("kb_stats", {}, tool))

    result = _run("某问题", llm=llm, registry=reg)

    assert tool.n == 1
    assert result.status == "ok"


def test_unknown_tool_is_skipped_not_fatal():
    """白名单关：模型编造工具名 → 记一步 skipped，它还能改。"""
    llm = ScriptedLLM([{"action": "tool", "tool": "drop_database", "args": {}, "confidence": 0.99}, FINISH_OK])
    reg = build_registry(("kb_stats", {}, CountingTool()))

    result = _run("某问题", llm=llm, registry=reg)

    assert result.outcome.steps[0].skipped == "unknown_tool"
    assert result.status == "ok"


def test_token_budget_stops_loop_and_no_more_llm_calls():
    """token 预算：每次调用后记账，超过上限后不再发起下一次 LLM 调用。

    时间预算管「用户等多久」，token 预算管「这次 run 花多少钱」——
    便宜的模型跑很多步和贵的模型跑三步是两种不同的失控，一个上限兜不住。
    每步换查询词，排除无进展检测抢先触发：这里唯一该停的原因是 token。
    """
    def step(q: str) -> dict:
        return {"action": "tool", "tool": "kb_stats", "args": {"q": q}, "confidence": 0.9,
                "_usage": {"prompt_tokens": 5000, "completion_tokens": 100}}

    llm = ScriptedLLM([step("a"), step("b"), step("c")])
    reg = build_registry(("kb_stats", {"q": {"type": "string"}}, CountingTool({"total": 150})))

    result = _run("某问题", llm=llm, registry=reg, max_tokens=12_000)

    assert result.status == "budget_exhausted"
    assert llm.calls == 3, "跨过 token 上限的那次调用允许完成，之后必须停"
    assert "token" in result.outcome.reason
    assert result.outcome.usage["prompt_tokens"] == 15_000
    # 已查到的事实保留：预算耗尽 ≠ 白跑
    assert result.outcome.facts


def test_token_budget_ignores_usage_when_llm_reports_none():
    """桩 LLM 不报 _usage（部分网关不回 usage）→ 不应误判为超支。"""
    repeat = {"action": "tool", "tool": "kb_stats", "args": {}, "confidence": 0.9}
    llm = ScriptedLLM([repeat, repeat, repeat])
    reg = build_registry(("kb_stats", {}, CountingTool({"total": 150})))

    result = _run("某问题", llm=llm, registry=reg, max_tokens=100)

    # 无进展检测先拦（第 3 步 needs_human），而不是被 token 预算误杀
    assert result.status == "needs_human"
    assert "无进展" in result.outcome.reason


def test_default_registry_tools_are_all_declared():
    """fail-closed 的代价：新工具忘了声明副作用 = 多问一次人。

    所以用这条测试把「忘了声明」变成红的：注册表里每个工具都必须在 SIDE_EFFECTS 里。
    """
    from types import SimpleNamespace

    from app.core.builtin_tools import build_default_registry

    rag = SimpleNamespace(docs=[{"root_cause_class": "x"}], mode="keyword")
    names = {s["name"] for s in build_default_registry(rag).specs()}

    assert names <= set(SIDE_EFFECTS), f"未声明副作用的工具: {names - set(SIDE_EFFECTS)}"
    assert SIDE_EFFECTS["kb_stats"] is SideEffect.READ

