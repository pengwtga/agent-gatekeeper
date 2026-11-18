"""I5/F17：finish 被门禁退回后的两个收尾优化。

要钉死的四件事：
1. 退回 ≠ 转人工：门禁错误要变成下一轮的输入（不是只写进 reason 就完事）；
2. 自动补齐只补**事实**、不补结论：evidence 仍必须由模型自己引用（否则证据自证）；
3. 修正有上限：模型改不动就老实转人工，不能变成无限重试；
4. 关闭开关时行为回到 I4（退回即转人工），消融组与主线只差这一个开关。
"""

import asyncio

from oncall.api import Deps, RunOptions, run_incident
from tests.v2.stubs import CountingTool, FakeClock, ScriptedLLM, build_registry

WEAK_FINISH = {
    "action": "finish",
    "conclusion": "xflow 回调地址仍是旧域名，更新配置后重放回调",
    "root_cause_class": "xflow_callback_lost",
    "evidence": [{"field": "kb", "ref": "r#step:1", "quote": "回调 POST 404"}],
    "confidence": 0.9,
}

GOOD_FINISH = {
    **WEAK_FINISH,
    "evidence": [
        {"field": "kb", "ref": "r#step:1", "quote": "回调 POST 404"},
        {"field": "history", "ref": "r#step:2", "quote": "同簇工单：旧域名残留"},
    ],
}

RAG_CALL = {"action": "tool", "tool": "rag_search", "args": {"query": "xflow 回调 404"},
            "thought": "先查知识库", "confidence": 0.9}


def _registry(*, with_history: bool = True):
    kb = CountingTool({"enough": True, "rounds": 1, "hits": [{"id": "KB-1", "class": "xflow_callback_lost"}]})
    hist = CountingTool({"incidents": [{"ticket_id": "T-9", "symptom": "旧域名残留"}]})
    entries = [("rag_search", {"query": {"type": "string", "required": True}}, kb)]
    if with_history:
        entries.append((
            "query_incidents",
            {"root_cause_class": {"type": "string", "required": True},
             "limit": {"type": "integer", "required": False}},
            hist,
        ))
    return build_registry(*entries), kb, hist


def _run(script, *, reg=None, **opts):
    registry = reg or _registry()[0]
    deps = Deps(llm=ScriptedLLM(script), registry=registry, clock=FakeClock())
    return asyncio.run(run_incident("供应商反馈创建后 xFlow 回调 404", deps=deps,
                                    opts=RunOptions(**opts)))


def test_repair_round_fills_history_and_finishes_ok():
    """主线：模型只查了 KB 就断言根因 → 门禁退回 → harness 代查 history → 重新 finish 通过。"""
    reg, kb, hist = _registry()
    result = _run([RAG_CALL, WEAK_FINISH, GOOD_FINISH], reg=reg)

    assert result.status == "ok"
    assert kb.n == 1 and hist.n == 1, "history 由 harness 代查，且只查一次"
    assert [s.tool for s in result.outcome.steps] == ["rag_search", "query_incidents"]
    assert result.outcome.facts[-1]["field"] == "query_incidents"
    assert len(result.outcome.repairs) == 1
    assert result.outcome.repairs[0]["auto_filled"] is True


def test_feedback_is_written_into_next_prompt_and_cleared_after():
    """退回原因必须出现在下一轮上下文里；模型一旦改用工具，反馈就不该继续占位。"""
    reg, _, _ = _registry()
    llm = ScriptedLLM([RAG_CALL, WEAK_FINISH, RAG_CALL, GOOD_FINISH])
    deps = Deps(llm=llm, registry=reg, clock=FakeClock())

    asyncio.run(run_incident("xflow 回调 404", deps=deps))

    assert len(llm.payloads) == 4
    assert "上一轮被门禁退回" not in llm.payloads[1]      # 第一次 finish 前没有反馈
    assert "上一轮被门禁退回" in llm.payloads[2]          # 退回后立刻能看到原因
    assert "query_incidents" in llm.payloads[2]           # 且告知已代查历史工单
    assert "上一轮被门禁退回" not in llm.payloads[3]      # 已经用工具了，反馈不再重复喂


def test_repair_is_capped_and_falls_back_to_needs_human():
    """模型改不动（还是同一份单源结论）→ 只给一轮，之后转人工，不无限重试。"""
    reg, _, hist = _registry()
    llm = ScriptedLLM([WEAK_FINISH])  # 脚本用完后重复最后一条
    deps = Deps(llm=llm, registry=reg, clock=FakeClock())

    result = asyncio.run(run_incident("xflow 回调 404", deps=deps))

    assert result.status == "needs_human"
    assert llm.calls == 2, "一次原始 finish + 一次修正机会，不能更多"
    assert len(result.outcome.repairs) == 1
    assert any("S2" in e for e in result.outcome.sensors["errors"])
    assert result.outcome.evidence, "被退回的结论仍要保留，供人复核"


def test_class_unknown_skips_auto_evidence():
    """没断言根因（unknown）时不需要跨源，也不该触发代查——纯查询只有一个权威来源。"""
    reg, _, hist = _registry()
    lookup = {
        "action": "finish",
        "conclusion": "库内没有该现象的处置依据，建议人工排查 xFlow 回调配置",
        "root_cause_class": "unknown",
        "evidence": [{"field": "kb", "ref": "r#step:1", "quote": "无命中条目"}],
        "confidence": 0.4,
    }

    result = _run([RAG_CALL, lookup], reg=reg)

    assert result.status == "ok"
    assert hist.n == 0
    assert result.outcome.repairs == []


def test_history_present_means_no_auto_fill():
    """两个来源都齐了仍被退回（例如结论太短）→ 只走反馈，不重复代查。"""
    reg, _, hist = _registry()
    short = {**GOOD_FINISH, "conclusion": "配置旧"}

    result = _run([RAG_CALL, short], reg=reg)

    assert hist.n == 0
    assert result.status == "needs_human"
    assert result.outcome.repairs and result.outcome.repairs[0]["auto_filled"] is False
    assert any("S1" in e for e in result.outcome.sensors["errors"])


def test_auto_evidence_skipped_when_tool_absent():
    """注册表里没有 query_incidents → 不越权、不报错，退回纯反馈模式。"""
    reg, _, _ = _registry(with_history=False)

    result = _run([RAG_CALL, WEAK_FINISH], reg=reg)

    assert result.status == "needs_human"
    assert result.outcome.repairs and result.outcome.repairs[0]["auto_filled"] is False
    assert "query_incidents" not in [s.tool for s in result.outcome.steps]


def test_repairs_zero_restores_i4_behaviour():
    """消融开关：max_finish_repairs=0 → 退回即转人工，一次 LLM 调用都不多花。"""
    reg, _, hist = _registry()
    llm = ScriptedLLM([WEAK_FINISH])
    deps = Deps(llm=llm, registry=reg, clock=FakeClock())

    result = asyncio.run(run_incident("xflow 回调 404", deps=deps,
                                      opts=RunOptions(max_finish_repairs=0)))

    assert result.status == "needs_human"
    assert llm.calls == 1
    assert hist.n == 0
    assert result.outcome.repairs == []


def test_repairs_are_serialized_for_observability():
    """退回记录要进 to_dict：面板/CLI/评测都要能回答「这条 run 修了几轮、修了什么」。"""
    reg, _, _ = _registry()
    result = _run([RAG_CALL, WEAK_FINISH, GOOD_FINISH], reg=reg)

    payload = result.to_dict()
    assert payload["repairs"][0]["attempt"] == 1
    assert "S2" in " ".join(payload["repairs"][0]["errors"])


def test_auto_fill_has_its_own_budget_besides_feedback_round():
    """真事故轨迹（SH-01）：先空手 finish 被退回 → 查了 KB → 仍是单源。

    反馈轮（1 轮）用完不该挡住补齐：把两个预算混成一个，最常见的
    「先乱答一次、后取证、仍是单源」就永远修不回来——这是真模型夜里跑出来的形状。
    """
    reg, _, hist = _registry()
    llm = ScriptedLLM([WEAK_FINISH, RAG_CALL, WEAK_FINISH, GOOD_FINISH])
    deps = Deps(llm=llm, registry=reg, clock=FakeClock())

    result = asyncio.run(run_incident("xflow 回调 404", deps=deps))

    assert result.status == "ok"
    assert llm.calls == 4
    assert hist.n == 1
    assert [r["auto_filled"] for r in result.outcome.repairs] == [False, True]


def test_auto_fill_requires_kb_to_support_the_asserted_class():
    """模型编的类名在 KB 命中里从未出现过 → 不许代查。

    真模型消融暴露的问题：无此约束时，只要模型敢写类名，harness 就去拉该类的历史工单，
    等于给一个「KB 从未支持」的断言补第二来源——门禁被补齐动作本身绕过。
    """
    kb = CountingTool({"enough": True, "rounds": 1,
                       "hits": [{"id": "KB-9", "class": "duplicate_submit"}]})
    hist = CountingTool({"incidents": []})
    reg = build_registry(
        ("rag_search", {"query": {"type": "string", "required": True}}, kb),
        ("query_incidents", {"root_cause_class": {"type": "string", "required": True},
                             "limit": {"type": "integer", "required": False}}, hist),
    )

    result = _run([RAG_CALL, WEAK_FINISH], reg=reg)  # 断言 xflow_callback_lost，KB 只有 duplicate_submit

    assert hist.n == 0, "KB 未支持该类，不得代查历史工单"
    assert result.status == "needs_human"
    assert result.outcome.repairs[0]["auto_filled"] is False
