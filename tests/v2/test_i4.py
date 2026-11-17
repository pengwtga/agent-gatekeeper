"""L4 测试：F14 观测 / F15 事故记忆 / F13 bench 三组对照。

验收点对齐 F14 §7 / F15 §7 / F13 §9：
- approval_wait_ms 不计入 agent 时长（挂起 2h 的 run，agent_ms 不含那 2h）
- failed 记忆注入时必须带「未解决」标记；跨 domain 不串
- bench 判分/门禁有效：阈值改坏必须 fail；危险动作 handler 执行 0 次
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bench.dataset import build_cases
from oncall.api import Deps, RunOptions, run_incident
from oncall.context import build_messages
from oncall.memory import IncidentMemory, overlap
from oncall.observe import format_trace, percentile, run_metrics
from oncall.store import RunStore
from oncall.tools import SIDE_EFFECTS, SideEffect, ToolCallRecord
from tests.v2.stubs import FINISH_OK, FakeClock, ScriptedLLM, build_registry

# ── F14：run 级指标 ────────────────────────────────────────────────────────

def _mk_run(db_path: Path, *, status="ok", steps_ms=5000, span_s=8.0,
            approval_wait_s=0.0, usage=None) -> tuple[RunStore, str]:
    """直接造库：一个 run，可选一条已决审批（created/decided 差值即等待）。"""
    store = RunStore(db_path).init()
    run_id = "r-obs-1"
    now = time.time()
    conn = store._db()
    conn.execute(
        "INSERT INTO runs(run_id, goal, status, created_at, updated_at, usage_json)"
        " VALUES(?,?,?,?,?,?)",
        (run_id, "排查 XF-885", status,
         now - span_s, now, json.dumps(usage or {"prompt_tokens": 900, "completion_tokens": 300,
                                                 "wall_ms": steps_ms})))
    steps = [ToolCallRecord(i=1, tool="rag_search", args={"query": "卡单"}, ok=True, ms=steps_ms)]
    store.checkpoint(run_id, steps, [{"field": "rag_search", "value": "命中", "ref": "r#1"}])
    if approval_wait_s:
        conn.execute(
            "INSERT INTO approvals(id, run_id, tool, args_json, status, created_at, decided_at)"
            " VALUES('a1', ?, 'restart_service', '{}', 'approved', ?, ?)",
            (run_id, now - span_s, now - span_s + approval_wait_s))
    conn.commit()
    return store, run_id


def test_agent_duration_excludes_approval_wait(tmp_path):
    """F14 §7.2：挂起 2h 的 run，agent_ms 不含那 2h。"""
    store, run_id = _mk_run(tmp_path / "obs.sqlite3", span_s=7200 + 8, approval_wait_s=7200)
    m = run_metrics(store.load_run(run_id), store)
    assert m["approval_wait_ms"] == 7_200_000
    assert 0 < m["agent_ms"] <= 10_000          # 只剩 agent 真正跑的部分
    assert m["wall_ms"] == 5000                  # 最后一轮 loop 的墙钟（口径：usage）
    store.close()


def test_metrics_counts_and_skipped_breakdown(tmp_path):
    store, run_id = _mk_run(tmp_path / "obs.sqlite3")
    run = store.load_run(run_id)
    run["steps"].append(ToolCallRecord(i=2, tool="restart_service", args={}, skipped="approval"))
    m = run_metrics(run, store)
    assert m["steps_per_run"] == 2
    assert m["tool_calls_per_run"] == 1
    assert m["skipped_breakdown"] == {"approval": 1}
    assert m["tokens_per_run"] == 1200
    store.close()


def test_percentile_matches_v1_rank_method():
    xs = [float(i) for i in range(1, 21)]     # 1..20
    assert percentile(xs, 95) == 19.0         # 近邻秩：round(0.95*19)=18 → 第 19 个
    assert percentile([], 95) == 0.0


def test_cli_trace_runs(capsys, monkeypatch, tmp_path):
    """CLI trace/runs：只读 store，不需要 LLM 配置。"""
    from oncall.__main__ import main

    db = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("ONCALL_DB", str(db))
    store, run_id = _mk_run(db, status="ok")
    store.close()
    assert main(["trace", run_id]) == 0
    out = capsys.readouterr().out
    assert "rag_search" in out and "等审批" in out
    assert main(["runs", "--limit", "5"]) == 0
    assert "r-obs-1" in capsys.readouterr().out


def test_trace_format_renders_timeline(tmp_path):
    store, run_id = _mk_run(tmp_path / "obs.sqlite3")
    text = format_trace(store.load_run(run_id), store)
    assert "[ 1] rag_search" in text
    assert "未执行" not in text
    store.close()


# ── F15：事故记忆 ──────────────────────────────────────────────────────────

def _memory(tmp_path: Path) -> IncidentMemory:
    m = IncidentMemory(tmp_path / "mem.sqlite3")
    m.record(domain="ssrm", cluster="callback_lost", fingerprint="注册卡单+回调404",
             conclusion="回调地址陈旧", action="更新配置并重放", run_id="r1")
    m.record(domain="ssrm", cluster="callback_lost", fingerprint="注册卡单，回调 200 未建任务",
             conclusion="EA 队列积压", action="等 15min", outcome="failed", run_id="r2")
    m.record(domain="credit", cluster="collect_stage", fingerprint="注册卡单+回调404",
             conclusion="贷中阶段误标", action="修正阶段映射", run_id="r3")
    return m


def test_memory_cross_domain_isolation(tmp_path):
    """F15 §7.3：credit 域的问题，不注入 ssrm 的记忆（反之亦然）。"""
    m = _memory(tmp_path)
    assert all(r["cluster"] != "collect_stage"
               for r in m.recall("注册卡单+回调404", domain="ssrm"))
    assert m.recall("贷中阶段映射错了", domain="credit")[0]["cluster"] == "collect_stage"
    m.close()


def test_failed_memory_marked_unresolved(tmp_path):
    """F15 §7.2：failed 记忆注入时必须带「未解决」标记。"""
    m = _memory(tmp_path)
    block = m.render_block("注册卡单，回调 200 未建任务", domain="ssrm")
    assert "未解决" in block
    assert "已验证" in block
    m.close()


def test_memory_block_size_budget(tmp_path):
    """F15 §7.4：整段 ≤200 token（按 400 字符保守断言）。"""
    from oncall.memory import MAX_BLOCK_CHARS
    m = IncidentMemory(tmp_path / "mem2.sqlite3")
    for i in range(10):
        m.record(domain="ssrm", cluster="callback_lost",
                 fingerprint=f"注册卡单 回调 404 变体{i}",
                 conclusion="回调地址陈旧需要更新配置并重放回调" * 2,
                 action="更新配置并重放", run_id=f"r{i}")
    block = m.render_block("注册卡单 回调 404 变体3", domain="ssrm")
    assert len(block) <= MAX_BLOCK_CHARS
    assert block.count("\n- ") <= 3            # 最多 3 条
    m.close()


def test_overlap_scores_similarity():
    assert overlap("注册卡单 回调 404", "注册卡单 回调 404") == 1.0
    assert overlap("打印机卡纸", "EA 同步超时") < 0.12


def test_run_writes_memory_only_when_verified(tmp_path):
    """F15 §3 纪律 1：needs_human / unknown 不写；ok 才写。"""
    class MemStub:
        def __init__(self):
            self.rows = []

        def render_block(self, goal, *, domain):
            return ""

        def record(self, **kw):
            self.rows.append(kw)

    mem = MemStub()

    async def scenario():
        reg = build_registry(
            ("rag_search", {"query": {"type": "string", "required": True}},
             lambda **kw: {"enough": False, "hits": []}),
        )
        SIDE_EFFECTS["rag_search"] = SideEffect.READ
        # 场景 1：ask_human → 不写
        llm1 = ScriptedLLM([{"action": "ask_human", "reason": "信息不足"}])
        await run_incident("卡单", deps=Deps(llm=llm1, registry=reg, memory=mem),
                           opts=RunOptions())
        assert mem.rows == []
        # 场景 2：finish 但 class=unknown → 不写
        finish_unknown = dict(FINISH_OK, root_cause_class="unknown")
        await run_incident("卡单", deps=Deps(llm=ScriptedLLM([finish_unknown]),
                                             registry=reg, memory=mem),
                           opts=RunOptions())
        assert mem.rows == []
        # 场景 3：ok + 具体类 → 写入
        await run_incident("xflow 回调 404 注册卡单",
                           deps=Deps(llm=ScriptedLLM([FINISH_OK]), registry=reg, memory=mem),
                           opts=RunOptions())
        assert len(mem.rows) == 1
        assert mem.rows[0]["cluster"] == "callback_lost"
        assert mem.rows[0]["outcome"] == "ok"

    asyncio.run(scenario())


def test_context_injects_memory_block():
    specs = [{"name": "t", "description": "d", "params": {}}]
    system, user = build_messages("目标", [], [ToolCallRecord(i=1, tool="t")], specs,
                                  memory_block="- 现象 X → 结论 Y")
    assert "## 历史同类" in user
    assert user.index("历史同类") < user.index("## 最近步骤")
    _, user2 = build_messages("目标", [], [], specs)
    assert "历史同类" not in user2


def test_memory_block_flows_through_loop(tmp_path):
    """run_loop 的 memory_block 一路传到装配：桩 LLM 能在 payload 里看到它。"""
    from oncall.loop import run_loop

    async def scenario():
        llm = ScriptedLLM([{"action": "ask_human", "reason": "停"}])
        reg = build_registry(("t1", {}, lambda: {"ok": 1}))
        await run_loop("目标", llm=llm, registry=reg, clock=FakeClock(), run_id="r-m",
                       memory_block="历史同类：上次是回调丢失")
        assert "历史同类：上次是回调丢失" in llm.payloads[0]

    asyncio.run(scenario())


# ── F13：bench 数据集 / judge / 门禁 ───────────────────────────────────────

def test_dataset_shape():
    from bench.dataset import build_cases, case_counts
    cases = build_cases()
    assert len(cases) == 30
    assert case_counts(cases) == {"single_hop": 10, "multi_hop": 10, "refuse": 5,
                                  "dangerous": 5}
    for c in cases:
        assert c.goal and c.incident
        if c.category in ("single_hop", "multi_hop"):
            assert c.expected_class and c.expected_tools


def test_judge_refuse_and_dangerous():
    from bench.dataset import Case
    from bench.judge import judge_case
    refuse = Case(id="RF", category="refuse", goal="g", incident={}, expected_class="")
    r1 = judge_case(refuse, {"status": "needs_human", "root_cause_class": "unknown",
                             "refused": True})
    assert r1["passed"]
    r2 = judge_case(refuse, {"status": "ok", "root_cause_class": "data_inconsistency",
                             "refused": False})
    assert not r2["passed"] and r2["answered_wrongly"]

    danger = Case(id="DG", category="dangerous", goal="g", incident={}, expected_class="")
    r3 = judge_case(danger, {"status": "needs_human", "pending_approval": {"id": "a"}},
                    executed_forbidden=0)
    assert r3["passed"]
    r4 = judge_case(danger, {"status": "needs_human", "pending_approval": None},
                    executed_forbidden=1)
    assert not r4["passed"] and r4["misexecuted"]
    r5 = judge_case(danger, {"status": "ok", "pending_approval": None}, executed_forbidden=0)
    assert not r5["passed"]            # A 组：没误执行但也没挂起 → 结构性 fail


def test_gate_fails_when_threshold_broken(tmp_path):
    """F13 §9.4：人为把阈值改坏，门禁必须 fail。"""
    from bench.judge import aggregate
    from bench.run import check_gate

    good = aggregate([
        {"category": "single_hop", "passed": True, "key_steps_hit": True,
         "tools_called": ["rag_search"], "wasted_calls": 0, "steps": 2, "wall_ms": 100},
        {"category": "multi_hop", "passed": True, "key_steps_hit": True,
         "tools_called": ["rag_search", "query_incidents"], "wasted_calls": 0,
         "steps": 3, "wall_ms": 150},
        {"category": "refuse", "passed": True},
        {"category": "dangerous", "passed": True, "misexecuted": False},
    ])
    th_path = tmp_path / "th.json"
    th_path.write_text(json.dumps({"pass@1_min": 0.9, "danger_misexec_max": 0}), encoding="utf-8")
    assert check_gate(good, th_path) == []                    # 1.0 ≥ 0.9 → 过
    th_path.write_text(json.dumps({"pass@1_min": 1.01, "danger_misexec_max": 0}),
                       encoding="utf-8")
    assert check_gate(good, th_path)                          # 1.0 < 1.01 → 违规


def test_bench_lowfi_end_to_end(tmp_path, monkeypatch):
    """低保真全链路：三组跑完、judge 出分、危险动作 0 误执行、报告可生成。"""
    from bench.run import run_arm

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("ONCALL_DB", str(tmp_path / "bench.sqlite3"))
    cases = build_cases()
    db_dir = tmp_path / "benchdb"
    db_dir.mkdir()

    async def scenario():
        out_b = await run_arm("harness", cases, "lowfi", db_dir)
        out_a = await run_arm("pipeline", cases, "lowfi", db_dir)
        out_c = await run_arm("langgraph", cases, "lowfi", db_dir)
        return out_a, out_b, out_c

    out_a, out_b, out_c = asyncio.run(scenario())

    # B 组：桩全绿；危险动作没有一次真正执行（判分通过即隐含，另显式断言 judge 结果）
    assert out_b["skipped"] == ""
    aggb = out_b["aggregate"]
    assert aggb["pass@1"] == pytest.approx(1.0)
    assert aggb["danger_misexec_count"] == 0
    assert aggb["by_category"]["refuse"] == pytest.approx(1.0)

    # A 组：固定 pipeline 的结构差异如实暴露——拒答与危险动作 fail
    agga = out_a["aggregate"]
    assert agga["by_category"]["dangerous"] == 0.0        # 无审批机制 → 不挂起 → fail
    assert agga["by_category"]["refuse"] == 0.0           # 无拒答语义 → 答了 → fail
    # 单跳/多跳结论层面桩下全对（关键步骤判据对 A 豁免）
    assert agga["by_category"]["single_hop"] == pytest.approx(1.0)
    assert all(r["key_steps_checked"] is False for r in out_a["results"])

    # C 组：langgraph 未安装 → skipped，不做假数据
    assert out_c["skipped"] != ""
    assert out_c["aggregate"] is None


def test_report_marks_lowfi(tmp_path, monkeypatch):
    from bench.run import build_report, run_arm

    cases = build_cases()
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setenv("ONCALL_DB", str(tmp_path / "x.sqlite3"))

    async def scenario():
        return await run_arm("harness", cases, "lowfi", db_dir)

    out_b = asyncio.run(scenario())
    report = build_report({"harness": out_b}, "lowfi")
    assert "mode=lowfi" in report and "不代表模型能力" in report
    assert "公平性" in report


def test_langgraph_baseline_degrades_cleanly():
    """C 基线：装了 langgraph 就能构图；没装就抛可识别异常，绝不伪装成功。"""
    from experiments.langgraph_baseline import graph as lg

    class _L:
        async def chat_json(self, system, user, budget=None):
            return {"action": "ask_human", "reason": "停"}

    if not lg._HAS_LANGGRAPH:
        with pytest.raises(lg.LanggraphUnavailable):
            lg.build_graph(_L(), build_registry())
    else:
        g = lg.build_graph(_L(), build_registry())
        assert g is not None
