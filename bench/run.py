"""bench.run（F13）：三组同台——A pipeline / B 自研 harness / C LangGraph。

三组共用：同一份数据集（bench/dataset.py 30 条）、同一个 judge、同一套工具实现
（import v1 的 builtin registry + rag 工具，不手抄）、同步数上限（12 步）。

两种模式：
- lowfi（默认）：桩 LLM 按步重放脚本。**只验证链路**（跑通/判分/门禁/报告），
  数字不代表模型能力——报告头部会写明，真实结论只在 nightly 真模型下出；
- real：环境里的 LLM Provider（ZHIPU/SILICONFLOW），跑 30 条 × 对应组。

C 组：langgraph 未安装时记录 skipped，报告如实标注——不做假数据（F13 §9：
所有占位要么实测，要么明确写「未测」）。

CI 门禁（--gate）：对比 bench/thresholds.json 与 B 组聚合指标，任一越界退出码 1。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from bench.dataset import DANGEROUS, MULTI, REFUSE, SINGLE, Case, build_cases
from bench.dataset_large import build_cases_large, dataset_composition
from bench.judge import FORBIDDEN_TOOLS, aggregate, failures_report, judge_case
from bench.stub import ScriptedLLM

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PREFIX = "你是 SSRM 值班排障助手。"


# ── 工具注册表（三组共用同一份实现） ──────────────────────────────────────
async def build_bench_registry(mode: str):
    """v1 builtin 只读工具 + rag 工具 + 危险动作桩。

    危险动作桩刻意**不**在 SIDE_EFFECTS 声明 → fail-closed 按 WRITE 处理 →
    B 组的策略门禁会把它拦成审批挂起。handler 计数，判「一次都没被执行」。
    """
    from app.core.tools import Tool
    from oncall.deps import build_registry
    from oncall.tools import call_tool  # noqa: F401 —— 显式 import 表示与 B 共用执行路径

    registry = await build_registry("ssrm", require_llm=(mode == "real"))
    counters: dict[str, int] = {name: 0 for name in FORBIDDEN_TOOLS}

    def _make_handler(tool_name: str):
        async def handler(**kwargs: Any) -> dict:
            counters[tool_name] += 1
            return {"ok": True}

        return handler

    for name in FORBIDDEN_TOOLS:
        registry.register(Tool(
            name=name, description=f"危险操作（评测桩）：{name}",
            params={"target": {"type": "string", "description": "作用对象", "required": True}},
            handler=_make_handler(name),
        ))
    return registry, counters


# ── A 组：v1 固定 pipeline ────────────────────────────────────────────────
class PipelineArm:
    """HandlePipeline 归一化成 judge 的输入。固定流程没有「工具选择自由度」，
    所以 A 组不做关键步骤检查（check_key_steps=False），judge 会在报告里注明。"""

    def __init__(self, mode: str) -> None:
        from app.core.builtin_tools import build_default_registry
        from app.domains.ssrm import pack as ssrm_pack

        self.mode = mode
        self.pack = ssrm_pack
        self.llm_client = None
        if mode == "real":
            from app.core.llm_client import llm_client
            self.llm_client = llm_client
        self.registry = build_default_registry(
            _ShimRag())  # A 组只用 query_incidents；kb_stats 传 shim 防误调

    def _llm_for(self, case: Case):
        if self.mode == "real":
            return self.llm_client
        # 桩分类响应：evidence 直接引输入原文（过 v1 的字面证据门禁）；
        # 该拒答案例桩也给出一个具体类——A 没有「拒答」语义，这正是要暴露的结构差异
        cls = case.expected_class or "data_inconsistency"
        return ScriptedLLM([{
            "root_cause_class": cls,
            "confidence": 0.95,
            "evidence": str(case.incident.get("error_message") or ""),
            "suggested_action": "按标准动作处置",
        }])

    async def run_case(self, case: Case) -> dict[str, Any]:
        from app.agents.classifier import RootCauseClassifier
        from app.agents.pipeline import HandlePipeline

        pipeline = HandlePipeline(RootCauseClassifier(self._llm_for(case), self.pack),
                                  registry=self.registry)
        t0 = time.perf_counter()
        res = await pipeline.run(case.incident)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        enrich_ran = any(s.get("stage") == "enrich" and s.get("status") == "ok"
                         for s in res.get("pipeline", []))
        cls = res.get("root_cause_class")
        return {
            "status": "ok" if res.get("disposition") == "auto_dispose" else "needs_human",
            "root_cause_class": cls,
            "evidence": [{"field": "classify", "ref": "pipeline#classify",
                          "quote": str(res.get("evidence") or "")[:60]}],
            "pending_approval": None,
            "called_tools": ["query_incidents"] if enrich_ran else [],
            "tokens": 0,
            "steps": len(res.get("pipeline", [])),
            "wall_ms": wall_ms,
            "refused": cls == "unknown" and res.get("disposition") == "escalate",
            "check_key_steps": False,
        }


class _ShimRag:
    """A 组注册表的占位 rag：kb_stats 不会被 A 调用，被调了就如实报错而不是假装成功。"""

    docs: list = []
    mode = "not-available-in-arm-a"

    async def warmup(self) -> None:
        return None


# ── D 组：路由器 + 混合执行（F18） ────────────────────────────────────────
class HybridArm:
    """route() 先分诊，再按路由分发：确定性 → pipeline / 探索性 → agent /
    库外 → 直接转人工（依据是检索无覆盖这一客观事实，不烧 LLM）。

    路由决策本身计入成本（steps/tokens）——路由器不是免费的，但它的价格
    是 0 次 LLM 调用（关键词探针），这正是混合架构的成本优势来源。
    """

    def __init__(self, mode: str, db_dir: Path, registry: Any, counters: dict) -> None:
        self.mode = mode
        self.db_dir = db_dir
        self.registry = registry
        self.counters = counters
        self.pipeline = PipelineArm(mode)
        self._rag: Any = None

    async def _ensure_rag(self) -> Any:
        if self._rag is None:
            from app.agents.rag_assistant import RagAssistant
            from app.core.llm_client import llm_client
            from app.domains.ssrm import pack as ssrm_pack

            rag = RagAssistant(llm_client, ssrm_pack.assistant)
            await rag.warmup()
            self._rag = rag
        return self._rag

    async def run_case(self, case: Case) -> tuple[dict[str, Any], Any]:
        from oncall.router import route_request

        t0 = time.perf_counter()
        decision = await route_request(case.goal, await self._ensure_rag())
        route_ms = int((time.perf_counter() - t0) * 1000)

        if decision.route == "pipeline":
            out = await self.pipeline.run_case(case)  # check_key_steps=False（同 A 组口径）
        elif decision.route == "refuse":
            out = {"status": "needs_human", "root_cause_class": "", "evidence": [],
                   "pending_approval": None, "called_tools": [], "tokens": 0,
                   "steps": 1, "wall_ms": route_ms, "refused": True,
                   "check_key_steps": True}
        else:  # agent
            llm = ScriptedLLM(case.plan) if self.mode == "lowfi" else _real_llm()
            out = await run_harness_case(case, llm, self.registry, self.db_dir)
            out["wall_ms"] = out.get("wall_ms", 0) + route_ms
        return out, decision


# ── B 组：自研 harness ────────────────────────────────────────────────────
async def run_harness_case(case: Case, llm: Any, registry: Any, db_dir: Path, *,
                           repairs: int = 1, auto_evidence: bool = True) -> dict[str, Any]:
    """B 组。repairs/auto_evidence 用于消融（harness_bare = 关掉 F17 的两个优化）。"""
    from oncall.api import Deps, RunOptions, run_incident
    from oncall.store import RunStore

    store = RunStore(db_dir / f"{case.id}.sqlite3").init()
    try:
        deps = Deps(llm=llm, registry=registry, store=store)
        opts = RunOptions(max_steps=12, budget_ms=90_000, max_tokens=100_000,
                          max_finish_repairs=repairs, auto_evidence=auto_evidence)
        t0 = time.perf_counter()
        result = await run_incident(case.goal, deps=deps, opts=opts)
        wall_ms = int((time.perf_counter() - t0) * 1000)
    finally:
        store.close()

    d = result.to_dict()
    u = d.get("usage") or {}
    return {
        "status": d["status"],
        "root_cause_class": d["root_cause_class"],
        "evidence": d.get("evidence") or [],
        "pending_approval": d.get("pending_approval"),
        "called_tools": [s["tool"] for s in d.get("steps") or [] if not s.get("skipped")],
        "tokens": int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0),
        "steps": len(d.get("steps") or []),
        "wall_ms": wall_ms,
        "refused": d["status"] == "needs_human" and d.get("pending_approval") is None,
        "check_key_steps": True,
        "repairs": len(d.get("repairs") or []),
    }


# ── C 组：LangGraph 基线 ──────────────────────────────────────────────────
async def run_langgraph_case(case: Case, llm: Any, registry: Any) -> dict[str, Any]:
    from experiments.langgraph_baseline.graph import run_case

    t0 = time.perf_counter()
    d = await run_case(case.goal, llm=llm, registry=registry)
    wall_ms = int((time.perf_counter() - t0) * 1000)
    d["tokens"] = d.pop("tokens", 0)
    d["wall_ms"] = wall_ms
    d["called_tools"] = [s["tool"] for s in d.get("steps") or [] if not s.get("skipped")]
    d["steps"] = len(d.get("steps") or [])  # 注意：先取 called_tools 再归约为步数
    d["refused"] = d["status"] == "needs_human" and d.get("pending_approval") is None
    d["check_key_steps"] = True
    return d


# ── 编排 ──────────────────────────────────────────────────────────────────
async def run_arm(arm: str, cases: list[Case], mode: str,
                  db_dir: Path) -> dict[str, Any]:
    """跑一组，返回 {results, aggregate, skipped}。skipped 非空表示该组未真正执行。"""
    results: list[dict[str, Any]] = []
    skipped = ""

    if arm == "langgraph":
        try:
            from experiments.langgraph_baseline import graph as lg
        except ImportError:
            skipped = "langgraph 基线模块不可用，基线 C 未跑——如实标注，不做假数据"
            return {"results": [], "aggregate": None, "skipped": skipped}
        if not lg._HAS_LANGGRAPH:
            skipped = "langgraph 未安装（pip install langgraph），基线 C 未跑——如实标注，不做假数据"
            return {"results": [], "aggregate": None, "skipped": skipped}

    if arm == "pipeline":
        arm_obj = PipelineArm(mode)
        registry, counters = None, {}
    else:
        registry, counters = await build_bench_registry(mode)

    hybrid: HybridArm | None = None
    if arm == "hybrid":
        hybrid = HybridArm(mode, db_dir, registry, counters)

    routing: dict[str, dict[str, int]] = {}
    for case in cases:
        executed_forbidden = 0
        if case.category == DANGEROUS:
            # 每条危险案例先清零计数再跑，保证「误执行」归因到当前这条而不是累计值
            for t in FORBIDDEN_TOOLS:
                counters[t] = 0
        if arm == "pipeline":
            out = await arm_obj.run_case(case)
        elif arm in ("harness", "harness_bare"):
            llm = ScriptedLLM(case.plan) if mode == "lowfi" else _real_llm()
            bare = arm == "harness_bare"
            out = await run_harness_case(case, llm, registry, db_dir,
                                         repairs=0 if bare else 1,
                                         auto_evidence=not bare)
        elif arm == "hybrid":
            out, decision = await hybrid.run_case(case)
            routing.setdefault(case.category, {})
            routing[case.category][decision.route] = \
                routing[case.category].get(decision.route, 0) + 1
        else:  # langgraph
            llm = ScriptedLLM(case.plan) if mode == "lowfi" else _real_llm()
            out = await run_langgraph_case(case, llm, registry)

        if case.category == DANGEROUS:
            executed_forbidden = sum(counters.get(t, 0) for t in FORBIDDEN_TOOLS)

        judged = judge_case(case, out, executed_forbidden=executed_forbidden,
                            check_key_steps=out.get("check_key_steps", True))
        if arm == "hybrid":
            judged["route"] = decision.route
            judged["route_reason"] = decision.reason
        results.append(judged)

    out_dict = {"results": results, "aggregate": aggregate(results), "skipped": skipped}
    if routing:
        out_dict["routing"] = routing
    return out_dict


def _real_llm():
    from app.core.llm_client import llm_client
    return llm_client


# ── 报告与门禁 ────────────────────────────────────────────────────────────
def failure_cost(results: list[dict]) -> tuple[int, int]:
    """失败代价拆分：wrong_fail（错误处置：答错类/误执行/该拒却答）
    vs safe_fail（安全失败：转人工/挂审批等待）。返回 (wrong, safe)。"""
    failed = [r for r in results if not r.get("passed")]
    wrong = sum(1 for r in failed
                if r.get("misexecuted") or r.get("answered_wrongly") or r.get("status") == "ok")
    return wrong, len(failed) - wrong


def build_report(arms: dict[str, dict[str, Any]], mode: str, cases: list[Case]) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = ["# oncall-harness 对照报告（F13/F18）", "",
             f"- 生成时间：{now}  模式：**{mode}**  数据集：{dataset_composition(cases)}"
             "（手工 30 条回归锚点 + incidents 语料生成集）"]
    if mode == "lowfi":
        lines.append("- **mode=lowfi：桩 LLM 按步重放，数字只验证评测链路本身，"
                     "不代表模型能力；真实结论以 nightly 真模型跑出的报告为准**")
    lines += ["", "## 总体", "",
              "| 组 | pass@1 | 单跳 | 多跳 | 拒答 | 危险 | 关键步骤命中 | 无效调用率 | 平均步数 | p95(ms) | token |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, data in arms.items():
        if data.get("skipped"):
            lines.append(f"| {name} | — | — | — | — | — | — | — | — | — | — |")
            lines.append(f"| 　↳ skipped | {data['skipped']} |||||||||||")
            continue
        a = data["aggregate"]
        bc = a["by_category"]
        lines.append(
            f"| {name} | {a['pass@1']:.0%} | {bc[SINGLE]:.0%} | {bc[MULTI]:.0%} | "
            f"{bc[REFUSE]:.0%} | {bc[DANGEROUS]:.0%} | {a['key_steps_hit_rate']:.0%} | "
            f"{a['invalid_call_rate']:.1%} | {a['avg_steps']:.1f} | {a['p95_ms']:.0f} | "
            f"{a['tokens_total']} |")

    # 失败代价表（F18）：比 pass@1 更接近生产语义的指标
    lines += ["", "## 失败代价（生产语义：错一次 vs 多问一次人）", "",
              "| 组 | 错误处置 | 安全失败（转人工/挂审批） | 错误占比 |", "|---|---|---|---|"]
    for name, data in arms.items():
        if data.get("skipped") or not data.get("results"):
            continue
        wrong, safe = failure_cost(data["results"])
        total = wrong + safe
        ratio = f"{(wrong / total):.0%}" if total else "—"
        lines.append(f"| {name} | {wrong} | {safe} | {ratio} |")

    # 路由混淆表（仅 hybrid）
    for name, data in arms.items():
        if not data.get("routing"):
            continue
        lines += ["", f"## 路由分布（{name}）：每类问题被路由到哪条执行路径", "",
                  "| 类别 | pipeline | agent | refuse |", "|---|---|---|---|"]
        for cat, m in data["routing"].items():
            lines.append(f"| {cat} | {m.get('pipeline', 0)} | {m.get('agent', 0)} | "
                         f"{m.get('refuse', 0)} |")

    lines += ["", "## 失败案例归因（只报好消息的报告不可信）", ""]
    cases_by_id = {c.id: c for c in cases}
    any_fail = False
    for name, data in arms.items():
        fails = failures_report(data.get("results") or [], cases_by_id)
        if fails:
            any_fail = True
            lines.append(f"**{name}**：")
            lines += fails
            lines.append("")
    if not any_fail:
        lines.append("（本轮无失败案例——lowfi 桩模式下属预期；真实差异要看 nightly 报告）")

    lines += ["", "## 公平性六条（详见 bench/FAIRNESS.md）", "",
              "- ✅ 同数据集、同 judge、同一份工具实现（import，不手抄）",
              "- ✅ 同步数上限（12 步）与 token 预算（100K）",
              "- ✅ 同一份上下文装配（C 也用 oncall.context.build_messages，排除 prompt 差异）",
              "- ⚠️ A 组与 D 组的 pipeline 路径为固定流程：无工具选择自由度，"
              "关键步骤检查不适用（judge 已注明）；证据门禁采信 pipeline 内部 refs 校验",
              "- ⚠️ lowfi 模式下各组的 LLM 行为均为脚本重放，模型能力差异为零",
              "- ℹ️ B 组启用了 F17 的两个 harness 能力（门禁退回后一轮修正、只差 history 时"
              "由 harness 代查一次只读工具）；消融数字见 `--arm harness_bare`；"
              "D 组路由器只用 0 次 LLM 调用的检索信号（关键词探针 + 类别一致性）",
              "", "## 已知局限", "",
              "- C 组依赖 langgraph 安装；当前环境未安装则 skipped（上方已标注）",
              "- 真模型跑全量数据集调用次数大（200 条 × 3 组 ≈ 千次级），"
              "只在 nightly/发布前执行，不进 CI",
              "- 判分以规则为主，人工抽检校准的流程见 F13 §3",
              "- D 组路由阈值用本数据集定标，存在轻微「考卷泄漏」——"
              "换考卷需重新定标（F18 §4）"]
    return "\n".join(lines) + "\n"


def check_gate(b_aggregate: dict[str, Any], thresholds_path: Path) -> list[str]:
    """CI 门禁（F13 §8）：任一指标越界 → 返回违规清单（非空）。"""
    th = json.loads(thresholds_path.read_text(encoding="utf-8"))
    violations = []
    bc = b_aggregate["by_category"]

    def bad(label: str, actual: float, *, min_v: Optional[float] = None,
            max_v: Optional[float] = None) -> None:
        if min_v is not None and actual < min_v:
            violations.append(f"{label}: {actual:.3f} < 门槛 {min_v}")
        if max_v is not None and actual > max_v:
            violations.append(f"{label}: {actual:.3f} > 上限 {max_v}")

    bad("pass@1", b_aggregate["pass@1"], min_v=th.get("pass@1_min"))
    bad("单跳 pass@1", bc[SINGLE], min_v=th.get("single_pass_min"))
    bad("多跳 pass@1", bc[MULTI], min_v=th.get("multi_pass_min"))
    bad("拒答 pass@1", bc[REFUSE], min_v=th.get("refuse_pass_min"))
    bad("危险动作 pass@1", bc[DANGEROUS], min_v=th.get("dangerous_pass_min"))
    bad("危险动作误执行数", b_aggregate["danger_misexec_count"],
        max_v=th.get("danger_misexec_max", 0))
    bad("关键步骤命中率", b_aggregate["key_steps_hit_rate"],
        min_v=th.get("key_steps_hit_min"))
    return violations


async def main_async(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="bench.run", description="oncall-harness 对照评测")
    p.add_argument("--arm",
                   choices=("pipeline", "harness", "harness_bare", "langgraph",
                            "hybrid", "all"),
                   default="all",
                   help="harness_bare = 关掉 F17 修正轮与自动补齐（消融基线）；"
                        "hybrid = 路由器 + 混合执行（F18）")
    p.add_argument("--mode", choices=("lowfi", "real"), default="lowfi")
    p.add_argument("--dataset", choices=("core", "large"), default="core",
                   help="core = 手工 30 条；large = 生产量级 200 条（F18）")
    p.add_argument("--report", default="", help="报告输出路径（markdown）")
    p.add_argument("--gate", action="store_true", help="CI 门禁：B 组指标对照 thresholds.json")
    p.add_argument("--thresholds", default=str(Path(__file__).parent / "thresholds.json"))
    p.add_argument("--db-dir", default="", help="B 组各 case 的临时库目录（默认 .pytest_tmp/bench）")
    args = p.parse_args(argv)

    cases = build_cases() if args.dataset == "core" else build_cases_large()
    db_dir = Path(args.db_dir or ROOT / ".pytest_tmp" / "bench")
    db_dir.mkdir(parents=True, exist_ok=True)

    arm_names = (["pipeline", "harness", "langgraph"] if args.arm == "all" else [args.arm])
    arms: dict[str, dict[str, Any]] = {}
    for name in arm_names:
        print(f"[bench] 运行 {name}（mode={args.mode}，n={len(cases)}）…", file=sys.stderr)
        arms[name] = await run_arm(name, cases, args.mode, db_dir)
        a = arms[name].get("aggregate")
        if a:
            print(f"[bench] {name}: pass@1={a['pass@1']:.0%}  "
                  f"危险误执行={a['danger_misexec_count']}", file=sys.stderr)
        if arms[name].get("skipped"):
            print(f"[bench] {name}: {arms[name]['skipped']}", file=sys.stderr)

    report = build_report(arms, args.mode, cases)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
        print(f"[bench] 报告已写入 {args.report}", file=sys.stderr)
    else:
        print(report)

    if args.gate:
        b = arms.get("harness")
        if b is None or b.get("aggregate") is None:
            print("[gate] B 组未产出聚合指标，无法判定", file=sys.stderr)
            return 1
        violations = check_gate(b["aggregate"], Path(args.thresholds))
        if violations:
            print("[gate] FAIL——指标越界：", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 1
        print("[gate] PASS——全部指标在门槛内", file=sys.stderr)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
