"""F11：Sub-agent 并行取证。

要钉死的五件事：
1. 只读是机制不是口号：子 agent 拿不到写工具，连尝试的机会都没有；
2. 并行不串行：两路各睡 60ms，总耗时明显小于 120ms；
3. 结构化解：子 agent 的引用落成带原始 field + ref 的事实，主 loop 的
   门禁口径完全不用改；
4. 冲突不投票：两路 class_guess 不同 → 落 conflict 事实 + 禁止断言根因；
5. 单路超时/失败不拖垮整组（结果里如实记录）。
"""

import asyncio
import re
import time

from oncall.api import Deps, RunOptions, run_incident
from oncall.loop import run_loop
from oncall.subagent import (
    DEFAULT_TIMEOUT_MS,
    EVIDENCE_LINES,
    EvidenceLine,
    Finding,
    available_lines,
    gather_evidence,
    merge_findings,
    should_spawn,
)
from tests.v2.stubs import CountingTool, FakeClock, build_registry

# ── 测试替身：按「线路名」返回脚本，不受并发顺序影响 ────────────────────
_LINE_RE = re.compile(r"## 你负责的线路：(\w+)")


class LineLLM:
    """从 payload 里认出是哪一路，再吐该路的脚本。gather 并发下依然确定。"""

    is_configured = True

    def __init__(self, by_line: dict[str, list[dict]], *, delay_ms: int = 0):
        self.by_line = by_line
        self.delay_ms = delay_ms
        self.calls = 0
        self.counts: dict[str, int] = {}
        self.payloads: list[str] = []

    async def chat_json(self, system_prompt: str, user_payload: str, budget=None) -> dict:
        self.calls += 1
        self.payloads.append(user_payload)
        line = _LINE_RE.search(user_payload)
        name = line.group(1) if line else "main"
        n = self.counts.get(name, 0) + 1
        self.counts[name] = n
        script = self.by_line.get(name) or [{"action": "finish", "summary": ""}]
        if self.delay_ms and name != "main":
            await asyncio.sleep(self.delay_ms / 1000)
        return script[min(n - 1, len(script) - 1)]


def _registry():
    rag = CountingTool({"enough": True, "rounds": 1,
                        "hits": [{"id": "KB-1", "class": "ea_sync_timeout"}]})
    topo = CountingTool({"topology": "xFlow → SSRM → EA"})
    write = CountingTool({"ok": True})
    return build_registry(
        ("rag_search", {"query": {"type": "string", "required": True}}, rag),
        ("system_topology", {}, topo),
        ("delete_vendor", {"target": {"type": "string", "required": True}}, write),
    ), rag, topo, write


KB_FINISH = {"action": "finish", "summary": "知识库命中 ea_sync_timeout",
             "evidence": [{"field": "kb", "quote": "EA 供应商主数据同步超时"}],
             "class_guess": "ea_sync_timeout", "confidence": 0.9}
TOPO_FINISH = {"action": "finish", "summary": "EA 与 SSRM 的同步链路最可能出问题",
               "evidence": [{"field": "topology", "quote": "xFlow → SSRM → EA"}],
               "confidence": 0.6}


# ── 单测：线路选择与触发 ────────────────────────────────────────────────
def test_write_tools_are_never_offered_to_subagents():
    """只读是机制：一条线里混进写工具，整条线不可用（不是过滤掉写工具后照跑）。"""
    reg, _, _, _ = _registry()
    bad = [EvidenceLine(name="all", field="kb",
                        tools=("rag_search", "delete_vendor"), instruction="查")]
    assert available_lines(reg, bad) == []
    assert [line.name for line in available_lines(reg)] == ["kb", "topology"]


def test_should_spawn_needs_two_lines_and_multi_source_hint():
    reg, _, _, _ = _registry()
    assert should_spawn("注册卡单了", reg, "auto") is False          # 无线索词
    assert should_spawn("为什么注册会卡单？影响面多大", reg, "auto") is True
    assert should_spawn("注册卡单了", reg, "force") is True
    assert should_spawn("为什么卡单", reg, "off") is False
    # 只有一路可用时，强制也没意义（并行至少要两路）
    reg1 = build_registry(("rag_search", {"query": {"type": "string", "required": True}},
                           CountingTool({})))
    assert should_spawn("为什么卡单", reg1, "force") is False


# ── gather：并行、合并、冲突、超时 ──────────────────────────────────────
def test_gather_runs_lines_in_parallel_and_merges_facts():
    reg, rag, topo, _ = _registry()
    llm = LineLLM({"kb": [KB_FINISH], "topology": [TOPO_FINISH]}, delay_ms=80)

    started = time.perf_counter()
    got = asyncio.run(gather_evidence("为什么同步会超时，影响哪些链路", llm=llm,
                                      registry=reg, run_id="r1"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15, f"两路应并行（各 80ms），实际 {elapsed:.2f}s"
    assert [f.line for f in got.findings] == ["kb", "topology"]
    assert got.conflicts == []

    fields = [f["field"] for f in got.facts]
    assert "kb" in fields and "topology" in fields        # 原始 field，主 loop 门禁不用改
    assert any(f["ref"] == "r1#sub:kb" for f in got.facts)
    assert any(f["ref"] == "r1#sub:kb:1" and "同步超时" in f["value"] for f in got.facts)
    assert rag.n == 0 and topo.n == 0, "子 agent 只回传结论，不再重复调工具（脚本里直接 finish）"


def test_conflict_is_recorded_not_voted():
    reg, _, _, _ = _registry()
    llm = LineLLM({"kb": [KB_FINISH],
                   "topology": [{**TOPO_FINISH, "class_guess": "duplicate_submit"}]})

    got = asyncio.run(gather_evidence("为什么卡单，影响面多大", llm=llm, registry=reg, run_id="r2"))

    assert got.conflicts and "不一致" in got.conflicts[0]
    assert any(f["field"] == "conflict" for f in got.facts)


def test_single_line_timeout_does_not_kill_the_group():
    reg, _, _, _ = _registry()
    llm = LineLLM({"kb": [KB_FINISH], "topology": [TOPO_FINISH]}, delay_ms=300)

    got = asyncio.run(gather_evidence("为什么卡单", llm=llm, registry=reg, run_id="r3",
                                      timeout_ms=80))

    by_line = {f.line: f for f in got.findings}
    assert all("超时" in f.error for f in by_line.values())
    assert got.facts, "超时也要留痕（失败本身是信息）"


def test_subagent_cannot_call_tools_outside_its_line():
    reg, rag, _, write = _registry()
    sneaky = {"action": "tool", "tool": "delete_vendor", "args": {"target": "x"}}
    llm = LineLLM({"kb": [sneaky, KB_FINISH], "topology": [TOPO_FINISH]})

    got = asyncio.run(gather_evidence("为什么卡单", llm=llm, registry=reg, run_id="r4"))

    assert write.n == 0, "写工具一次都不能被执行（策略门禁之外的第二道保险）"
    kb = next(f for f in got.findings if f.line == "kb")
    assert "不在本路可用范围" in str(kb.steps[0]["error"])


def test_merge_marks_failed_line_instead_of_dropping_it():
    facts, conflicts = merge_findings([
        Finding(line="kb", summary="命中", evidence=[{"field": "kb", "quote": "原文"}]),
        Finding(line="topology", error="超时（>8000ms）"),
    ], "r5")

    assert conflicts == []
    assert any("取证失败" in f["value"] for f in facts)


# ── loop 集成：F11 在真实链路里的两个作用点 ─────────────────────────────
def _deps(script_by_line: dict[str, list[dict]], reg=None):
    reg = reg or _registry()[0]
    return Deps(llm=LineLLM(script_by_line), registry=reg, clock=FakeClock())


def test_loop_uses_subagent_facts_then_model_finishes_on_them():
    """主 loop 拿到的就是普通事实（field/ref/quote 齐全），门禁口径零改动。"""
    finish = {
        "action": "finish",
        "conclusion": "EA 同步队列积压导致注册卡单，先扩容同步 worker 再重放积压",
        "root_cause_class": "ea_sync_timeout",
        "evidence": [
            {"field": "kb", "ref": "r#sub:kb:1", "quote": "EA 供应商主数据同步超时"},
            {"field": "topology", "ref": "r#sub:topology:1", "quote": "xFlow → SSRM → EA"},
        ],
        "confidence": 0.9,
    }
    deps = _deps({"kb": [KB_FINISH], "topology": [TOPO_FINISH], "main": [finish]})

    result = asyncio.run(run_incident("为什么同步会超时，影响哪些链路", deps=deps,
                                      opts=RunOptions(subagents="force")))

    assert result.status == "ok"
    assert [s["line"] for s in result.outcome.subagents] == ["kb", "topology"]
    assert any(f["field"] == "kb" and f["ref"].endswith("#sub:kb:1")
               for f in result.outcome.facts)


def test_conflict_blocks_root_cause_assertion():
    """两路结论矛盾时，模型仍想给类名 → 门禁退回并转人工，结论保留供复核。"""
    conflict_line = {**TOPO_FINISH, "class_guess": "duplicate_submit"}
    finish = {
        "action": "finish",
        "conclusion": "EA 同步超时导致注册卡单，建议扩容 worker",
        "root_cause_class": "ea_sync_timeout",
        "evidence": [
            {"field": "kb", "ref": "r#sub:kb:1", "quote": "EA 供应商主数据同步超时"},
            {"field": "topology", "ref": "r#sub:topology:1", "quote": "xFlow → SSRM → EA"},
        ],
        "confidence": 0.9,
    }
    deps = _deps({"kb": [KB_FINISH], "topology": [conflict_line], "main": [finish]})

    result = asyncio.run(run_incident("为什么卡单，影响面多大", deps=deps,
                                      opts=RunOptions(subagents="force", max_finish_repairs=0)))

    assert result.status == "needs_human"
    assert any("取证冲突" in e for e in result.outcome.sensors["errors"])
    assert result.outcome.evidence, "被退回的结论仍要保留"


def test_resume_does_not_respawn_subagents():
    """断点续跑时事实已在库里：再 spawn 一遍等于重复烧钱，也违背 resume 语义。"""
    reg, _, _, _ = _registry()
    llm = LineLLM({"kb": [KB_FINISH], "topology": [TOPO_FINISH],
                   "main": [{"action": "ask_human", "reason": "信息不足"}]})

    out = asyncio.run(run_loop(
        "为什么卡单", llm=llm, registry=reg, clock=FakeClock(), run_id="r6",
        max_steps=2, subagents="force",
        initial_facts=[{"field": "kb", "value": "已有事实", "ref": "r6#step:1"}],
        initial_steps=[],
    ))

    assert out.subagents == []
    assert llm.calls == 1


def test_repeated_tool_call_is_blocked_and_line_can_still_finish():
    """真模型实测的坏轨迹：同一条线反复「再查一次确认」，把 3 步烧完还不给结论。

    修法不是加步数，而是拦住重复 + 最后一步强制 finish：单路取证的边际收益
    在第一次成功调用后已经接近 0。
    """
    reg, rag, _, _ = _registry()
    again = {"action": "tool", "tool": "rag_search", "args": {"query": "换个说法再查"}}
    llm = LineLLM({"kb": [again, again, KB_FINISH], "topology": [TOPO_FINISH]})

    got = asyncio.run(gather_evidence("为什么卡单，影响面多大", llm=llm, registry=reg, run_id="r7"))

    kb = next(f for f in got.findings if f.line == "kb")
    assert rag.n == 1, "同一工具只执行一次，重复调用被拦"
    assert any(s.get("error") == "重复调用同一工具" for s in kb.steps)
    assert kb.summary, "被拦之后仍然给出了结论（不是整条线作废）"


def test_line_that_never_finishes_is_recorded_as_failed():
    reg, _, _, _ = _registry()
    again = {"action": "tool", "tool": "rag_search", "args": {"query": "再查"}}
    llm = LineLLM({"kb": [again], "topology": [again]})  # 永远只调工具

    got = asyncio.run(gather_evidence("为什么卡单", llm=llm, registry=reg, run_id="r8"))

    assert all(("未给出" in f.error or "重复调用" in f.error or "步数用完" in f.error)
               for f in got.findings)


def test_default_timeout_is_sane():
    assert DEFAULT_TIMEOUT_MS >= 5000 and len(EVIDENCE_LINES) >= 2
