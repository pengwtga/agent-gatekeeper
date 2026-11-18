"""F11 触发判据：从「用户措辞」换成「知识库根因簇是否分裂」。

要钉死的事（对应 I8 的判据改造）：

1. **主判据是客观信号**：goal 完全不含「为什么/影响面/链路」这类词，
   只要路由探针说命中了 ≥2 个根因簇，就必须并行——这正是旧判据 0.5% 触发率的解药；
2. **信号不可得时不硬凑**：只有 1 个簇、又没有措辞线索 → 不并行，
   且要给出一句可读的理由（否则跑完只知道「没触发」，不知道为什么）；
3. **开关优先于判据**：mode=off 在任何信号下都不并行；线路不足 2 路时 force 也不并行；
4. **贯通到 Outcome**：spawn 决策要进 Outcome.spawn，报告与 CLI 才能归因。
"""

import asyncio

from oncall.loop import run_loop
from oncall.router import route_request
from oncall.subagent import decide_spawn, lines_for_repair, should_spawn
from tests.v2.stubs import CountingTool, FakeClock, build_registry
from tests.v2.test_f11 import KB_FINISH, TOPO_FINISH, LineLLM


def _registry():
    rag = CountingTool({"enough": True, "rounds": 1,
                        "hits": [{"id": "KB-1", "class": "ea_sync_timeout"}]})
    topo = CountingTool({"topology": "xFlow → SSRM → EA"})
    return build_registry(
        ("rag_search", {"query": {"type": "string", "required": True}}, rag),
        ("system_topology", {}, topo),
    ), rag, topo


# ── 判据本身（纯函数，不调 LLM） ─────────────────────────────────────────
def test_class_spread_triggers_even_without_any_wording_hint():
    """核心行为变化：措辞不再是必要条件，簇分裂就够。"""
    reg, _, _ = _registry()
    goal = "EA 供应商主数据同步超时，注册卡在 register 阶段"   # 无「为什么/影响面」
    old = decide_spawn(goal, reg, "auto")
    assert old.should is False and old.signal == "no-signal"
    new = decide_spawn(goal, reg, "auto", class_spread=2)
    assert new.should is True and new.signal == "split"
    assert "根因簇不唯一" in new.why
    assert [line.name for line in new.lines] == ["kb", "topology"]


def test_cls_split_flag_also_triggers():
    reg, _, _ = _registry()
    d = decide_spawn("注册卡单", reg, "auto", cls_split=True)
    assert d.should is True and d.signal == "split"


def test_single_cluster_without_wording_does_not_spawn_and_explains_why():
    reg, _, _ = _registry()
    d = decide_spawn("EA 同步超时注册卡单", reg, "auto", class_spread=1)
    assert d.should is False
    assert d.signal == "no-signal"
    assert d.why and d.lines == ()


def test_off_wins_over_any_signal():
    reg, _, _ = _registry()
    d = decide_spawn("EA 同步超时", reg, "off", class_spread=3, cls_split=True)
    assert d.should is False and d.signal == "off"


def test_force_beats_signal_absence():
    reg, _, _ = _registry()
    assert should_spawn("注册卡单", reg, "force") is True
    assert should_spawn("注册卡单", reg, "auto", class_spread=0) is False


def test_insufficient_lines_never_spawns():
    """只有一路只读工具时，即使 force 也不并行（一路并行没有意义）。"""
    solo = build_registry(
        ("rag_search", {"query": {"type": "string", "required": True}},
         CountingTool({"hits": []})),
    )
    d = decide_spawn("为什么卡单", solo, "force")
    assert d.should is False and d.signal == "insufficient-lines"


# ── 贯通：spawn_signal → run_loop → Outcome.spawn ────────────────────────
def _run(goal: str, signal: dict):
    reg, rag, topo = _registry()
    llm = LineLLM({
        "kb": [{"action": "tool", "tool": "rag_search", "args": {"query": "EA 同步超时"}}, KB_FINISH],
        "topology": [{"action": "tool", "tool": "system_topology", "args": {}}, TOPO_FINISH],
        "main": [{"action": "finish", "conclusion": "根因是 EA 同步超时导致注册卡单（已双源印证）",
                  "root_cause_class": "ea_sync_timeout",
                  "evidence": [{"field": "kb", "ref": "r#sub:kb:1", "quote": "EA 供应商主数据同步超时"},
                               {"field": "history", "ref": "r#step:1", "quote": "同类工单 3 条"}],
                  "confidence": 0.9}],
    })
    return asyncio.run(run_loop(goal, llm=llm, registry=reg, clock=FakeClock(), run_id="r",
                                subagents="auto", spawn_signal=signal)), rag, topo


def test_spawn_signal_reaches_the_loop_and_is_recorded():
    """路由探针给 split → loop 首轮就并行，且决策进了 outcome.spawn。"""
    outcome, rag, topo = _run("EA 供应商主数据同步超时，注册卡在 register 阶段",
                              {"class_spread": 2, "candidate_classes": ["ea_sync_timeout", "xflow_callback_lost"]})
    assert outcome.spawn["should"] is True
    assert outcome.spawn["signal"] == "split"
    assert outcome.spawn["lines"] == ["kb", "topology"]
    assert len(outcome.subagents) == 2
    assert rag.n == 1 and topo.n == 1        # 两路各查一次，不是主 loop 代查


def test_without_signal_loop_stays_serial_and_records_reason():
    outcome, rag, topo = _run("EA 供应商主数据同步超时，注册卡在 register 阶段", {})
    assert outcome.spawn["should"] is False
    assert outcome.spawn["why"]
    assert outcome.subagents == []
    assert rag.n == 0 and topo.n == 0


# ── 上游：路由探针确实把簇数算出来 ──────────────────────────────────────
class _StubRag:
    """探针替身：直接给定 top 命中，用来钉死「簇数怎么算」而不是检索质量。"""

    class _Spec:
        class_field = "root_cause_class"

    spec = _Spec()

    def __init__(self, classes: list[str], base_score: float = 0.30):
        self._classes = classes
        self._base = base_score

    def _keyword_search(self, query: str, top_k: int = 5):
        return [{"score": self._base - 0.02 * i, "root_cause_class": c}
                for i, c in enumerate(self._classes[:top_k])]


def test_router_reports_class_spread_for_agent_route():
    # 三类同票 → 方向分裂，路由必须落 agent，并把簇数与候选类带出来（F11 直接用）
    rag = _StubRag(["ea_sync_timeout", "xflow_callback_lost", "duplicate_submit"])
    d = asyncio.run(route_request("供应商注册失败，请排查", rag))
    assert d.route == "agent"
    assert d.signals["class_spread"] == 3
    assert d.signals["candidate_classes"] == [
        "ea_sync_timeout", "xflow_callback_lost", "duplicate_submit"]


def test_router_reports_spread_one_when_direction_is_clear():
    rag = _StubRag(["ea_sync_timeout"] * 3)
    d = asyncio.run(route_request("EA 同步超时注册卡单", rag))
    assert d.route == "pipeline"
    assert d.signals["class_spread"] == 1


def test_router_marks_weak_cover_so_split_cannot_misfire():
    """弱覆盖带的多簇 ≠ 多解（数据集实测：agent 路径上 3/3 都是库外拒答题）。"""
    rag = _StubRag(["ea_sync_timeout", "xflow_callback_lost", "duplicate_submit"],
                   base_score=0.11)
    d = asyncio.run(route_request("停车场车牌识别道闸不抬杆", rag))
    # 无论路由落哪，strong_cover 都必须是 False，下游据此否掉 split
    assert d.signals.get("strong_cover") is False
    assert decide_spawn("停车场车牌识别道闸不抬杆", _repair_registry()[0], "auto",
                        class_spread=d.signals["class_spread"],
                        strong_cover=d.signals["strong_cover"]).should is False


# ── 门禁退回时的并行取证（SSRM 域里真正会生效的触发点） ────────────────
def _repair_registry():
    rag = CountingTool({"enough": True, "rounds": 1,
                        "hits": [{"id": "KB-1", "class": "ea_sync_timeout"}]})
    topo = CountingTool({"topology": "xFlow → SSRM → EA"})
    hist = CountingTool({"tickets": [{"id": "INC-1", "root_cause_class": "ea_sync_timeout",
                                      "symptom": "EA 同步超时导致注册卡单"}]})
    return build_registry(
        ("rag_search", {"query": {"type": "string", "required": True}}, rag),
        ("system_topology", {}, topo),
        ("query_incidents", {"root_cause_class": {"type": "string", "required": True},
                             "limit": {"type": "number", "required": False}}, hist),
    ), rag, topo, hist


def _repair_run(main_script: list[dict]):
    reg, rag, topo, hist = _repair_registry()
    llm = LineLLM({
        "history": [{"action": "tool", "tool": "query_incidents",
                     "args": {"root_cause_class": "ea_sync_timeout"}},
                    {"action": "finish", "summary": "该类历史工单存在",
                     "evidence": [{"field": "history", "quote": "EA 同步超时导致注册卡单"}],
                     "class_guess": "ea_sync_timeout", "confidence": 0.8}],
        "topology": [{"action": "tool", "tool": "system_topology", "args": {}}, TOPO_FINISH],
        "main": main_script,
    })
    out = asyncio.run(run_loop("EA 供应商主数据同步超时，注册卡在 register 阶段",
                               llm=llm, registry=reg, clock=FakeClock(), run_id="r",
                               subagents="auto"))
    return out, rag, topo, hist


SINGLE_SOURCE_HISTORY = {
    "action": "finish", "conclusion": "根因是 EA 同步超时导致注册卡单",
    "root_cause_class": "ea_sync_timeout",
    "evidence": [{"field": "history", "ref": "r#step:1", "quote": "EA 同步超时导致注册卡单"}],
    "confidence": 0.8,
}
TWO_SOURCE_FINISH = {
    "action": "finish", "conclusion": "根因是 EA 同步超时导致注册卡单，影响 xFlow/Dora 两处入口",
    "root_cause_class": "ea_sync_timeout",
    "evidence": [
        {"field": "history", "ref": "r#sub:history:1", "quote": "EA 同步超时导致注册卡单"},
        {"field": "topology", "ref": "r#sub:topology:1", "quote": "xFlow → SSRM → EA"},
    ],
    "confidence": 0.9,
}


def test_repair_spawns_history_and_topology_when_both_are_missing():
    """只引了 history、没引 kb → F17 的单点补齐不适用 → 按缺口并行两路取证。"""
    out, rag, topo, hist = _repair_run([
        {"action": "tool", "tool": "rag_search", "args": {"query": "EA 同步超时"}},
        SINGLE_SOURCE_HISTORY,
        TWO_SOURCE_FINISH,
    ])
    assert out.repairs and out.repairs[0]["spawned"] is True
    assert out.repairs[0]["auto_filled"] is True          # 并行取证也算补齐
    assert [s["line"] for s in out.subagents] == ["history", "topology"]
    assert hist.n == 1 and topo.n == 1


def test_repair_keeps_single_fill_when_only_history_is_missing():
    """只差 history → 走 F17 单点补齐（1 次工具调用），不要为它开两路 LLM。"""
    out, rag, topo, hist = _repair_run([
        {"action": "tool", "tool": "rag_search", "args": {"query": "EA 同步超时"}},
        {"action": "finish", "conclusion": "根因是 EA 同步超时导致注册卡单",
         "root_cause_class": "ea_sync_timeout",
         "evidence": [{"field": "kb", "ref": "r#step:1", "quote": "EA 供应商主数据同步超时"}],
         "confidence": 0.8},
        TWO_SOURCE_FINISH,
    ])
    assert out.repairs and out.repairs[0]["auto_filled"] is True
    assert out.repairs[0]["spawned"] is False
    assert out.subagents == []
    assert hist.n == 1 and topo.n == 0                    # 只有 harness 补的那一次 history


def test_lines_for_repair_refuses_to_guess_class_without_kb_support():
    reg, _, _, _ = _repair_registry()
    lines = lines_for_repair("ea_sync_timeout", need_history=True, need_topology=True,
                             kb_support=False, registry=reg)
    assert [line.name for line in lines] == ["topology"]   # history 需要类名，不猜


def test_lines_for_repair_pins_the_known_class_in_hint():
    reg, _, _, _ = _repair_registry()
    lines = lines_for_repair("ea_sync_timeout", need_history=True, need_topology=True,
                             kb_support=True, registry=reg)
    assert [line.name for line in lines] == ["history", "topology"]
    assert "ea_sync_timeout" in lines[0].hint
