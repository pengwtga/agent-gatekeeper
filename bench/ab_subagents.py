"""F11 消融：并行取证到底有没有用、会不会负影响（以及 F10 skill 的净效应）。

为什么需要这个脚本，而不是直接看 200 条主报告：
主报告里 `--subagents` 默认 off（刻意如此——并行会改变 token/延迟/轨迹，
不能让历史数字无声漂移）。所以「F11 有没有用」在主报告里没有答案，
必须做一次**只差这一个变量**的对照。

三臂设计（同一批 case、同一个 judge、同一份工具注册表）：

- base   ：skills 开 + subagents off    —— 现状基线
- sub    ：skills 开 + subagents force  —— 只多「首轮两路并行取证」这一件事
- noskill：skills 关 + subagents off    —— 只少「技能索引 + 命中注入」这一件事

为什么 sub 用 force 不用 auto：
auto 依赖关键词线索词，本脚本挑的 case 本来就已经命中线索词，
用 force 是为了保证「两臂确实只差并行取证」这个变量，而不是差在触发判定上。

口径诚实声明：
- 样本是「多源线索」子集，不是全量 200——pass@1 不能与主报告横向比；
- 多跳的 key_steps 判据对生成集偏严（见 F18-eval-qa §1），
  但三臂用同一判据，**差值**是有效的，绝对值偏保守。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from bench.dataset import MULTI, SINGLE, Case
from bench.judge import aggregate, failures_report, judge_case
from bench.run import _real_llm, build_bench_registry, run_harness_case


def pick_cases(n: int) -> list[Case]:
    """挑「多源线索」的单跳/多跳 case，各占一半——这些才是并行取证声称要帮到的场景。

    为什么单跳也要留：F11 的触发判据看的是 goal 里的线索词，不看类别。
    「为什么 X 会影响 Y」这种单跳题（一次检索能定位根因、但结论要解释跨系统因果）
    才是并行取证的真实适用面；只测多跳会把「多跳本来就难」误当成「并行没用」。
    """
    from bench.dataset_large import build_cases_large

    single: list[Case] = []
    multi: list[Case] = []
    for c in build_cases_large():
        if c.category == MULTI:
            multi.append(c)
        elif c.category == SINGLE and _has_hint(c.goal):
            single.append(c)

    out: list[Case] = []
    for a, b in zip(single, multi):
        out.extend([a, b])
    # 单跳带线索词的 case 在现有数据集里可能为 0（这本身是个发现：
    # 真实 goal 很少写「为什么/影响面」，auto 触发面比设计预期窄），
    # 所以补上剩余的多跳，保证样本量。
    used = {c.id for c in out}
    out.extend([c for c in single + multi if c.id not in used])
    return out[:n]


def _has_hint(goal: str) -> bool:
    from oncall.subagent import MULTI_SOURCE_HINTS

    return any(h in goal for h in MULTI_SOURCE_HINTS)


async def run_arm(name: str, cases: list[Case], *, subagents: str, use_skills: bool,
                  db_dir: Path) -> dict:
    registry, _ = await build_bench_registry("real")
    results = []
    for case in cases:
        out = await run_harness_case(case, _real_llm(), registry, db_dir,
                                     subagents=subagents, use_skills=use_skills)
        results.append(judge_case(case, out, executed_forbidden=0, check_key_steps=True))
        # run_harness_case 的 subagent 数只记了「几路」，这里补一条「有几路给出了结论」
        results[-1]["_sub_lines"] = out.get("subagents", 0)
    return {"name": name, "results": results, "aggregate": aggregate(results),
            "cases": cases}


def _skill_book():
    from bench.run import skill_book

    return skill_book()


def _skill_stats(arm: dict, by_id: dict, book) -> tuple[int, int, int, int] | None:
    """命中 incident-triage 的 case 上，三条可机器校验的行为是否出现。"""
    if book is None:
        return None
    rows = []
    for r in arm["results"]:
        c = by_id.get(r["case_id"])
        if c is None:
            continue
        matched = book.match(c.goal)
        if matched:
            rows.append((r, matched[0].name))
    if not rows:
        return (0, 0, 0, 0)
    n = len(rows)
    first_rag = sum(1 for r, _ in rows
                    if (r.get("tools_called") or [""])[0] == "rag_search")
    got_hist = sum(1 for r, _ in rows if "query_incidents" in (r.get("tools_called") or []))
    ordered = sum(
        1 for r, _ in rows
        if "rag_search" in (r.get("tools_called") or [])
        and "query_incidents" in (r.get("tools_called") or [])
        and r["tools_called"].index("rag_search") < r["tools_called"].index("query_incidents")
    )
    return (n, first_rag, got_hist, ordered)


def _is_safe_fail(r: dict) -> bool:
    """安全失败 = 转人工或挂审批；错误处置 = 给了错误结论 / 动作悬空。"""
    return r.get("status") == "needs_human" or r.get("status") == "suspended"


def report(arms: list[dict], subset: list[Case]) -> str:
    lines = ["# F11 并行取证 / F10 技能 消融报告", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}  真模型",
             f"- 样本：{len(subset)} 条（多源线索单跳 + 多跳子集）",
             "- 三臂只差一个变量：base→sub 只加「首轮两路并行取证」；"
             "base→noskill 只去「技能注入」", "",
             "> **口径敏感性（必须写在最前面）**：同一批 case、同一个模型，"
             "在 judge 还没把子 agent 调过的工具计入关键步骤之前，sub 臂跑出 13%；"
             "修完口径后重跑是 47%。**口径没修之前，「这个能力没用」的结论不可信**——"
             "并行取证越有效，主 loop 越省步骤，旧口径判得越差。", "",
             "## 总体", "",
             "| 臂 | pass@1 | 单跳 | 多跳 | 平均步数 | 平均 token | p95(ms) | 子 agent 路数 | 失败(错误处置) |",
             "|---|---|---|---|---|---|---|---|---|"]
    by_id = {c.id: c for c in subset}
    for a in arms:
        ag = a["aggregate"]
        bc = ag["by_category"]
        sub_total = sum(r.get("_sub_lines", 0) for r in a["results"])
        wrong = sum(1 for r in a["results"]
                    if not r["passed"] and not _is_safe_fail(r))
        lines.append(f"| {a['name']} | {ag['pass@1']:.0%} | "
                     f"{bc.get(SINGLE, 0):.0%} | {bc.get(MULTI, 0):.0%} | "
                     f"{ag['avg_steps']:.1f} | {ag['tokens_total']} | "
                     f"{ag['p95_ms']:.0f} | {sub_total} | {wrong} |")

    lines += ["", "## 逐条对照（两臂结论不同的一律列出，不挑好看的）", "",
              "| case | 类别 | base | sub | noskill |", "|---|---|---|---|---|"]
    idx = {a["name"]: {r["case_id"]: r for r in a["results"]} for a in arms}
    for c in subset:
        row = [idx[n][c.id]["passed"] for n in ("base", "sub", "noskill") if c.id in idx.get(n, {})]
        if len(set(row)) > 1:
            mark = lambda b: "✅" if b else "❌"  # noqa: E731
            lines.append(f"| {c.id} | {c.category} | {mark(row[0])} | "
                         f"{mark(row[1])} | {mark(row[2])} |")

    lines += ["", "## 技能流程遵守率（回答「怎么确认 agent 按 skill 走」）", "",
              "口径：只统计隐式命中 `incident-triage` 的 case，看它是否做了 skill 声明的"
              "第 1/2 步（先 KB、再查同类）以及顺序。**skill 本身不强制任何行为，"
              "这一节量的是「注入之后行为有没有变化」，不是「模型是否服从」**。", "",
              "| 臂 | 命中 skill | 首调 rag_search | 调过 query_incidents | 顺序正确 |",
              "|---|---|---|---|---|"]
    book = _skill_book()
    for a in arms:
        s = _skill_stats(a, by_id, book)
        if s:
            n, first_rag, got_hist, ordered = s
            lines.append(f"| {a['name']} | {n} | {first_rag}/{n} | {got_hist}/{n} | {ordered}/{n} |")
        else:
            lines.append(f"| {a['name']} | 0 | — | — | — |")

    lines += ["", "## 失败归因（base 臂）", ""]
    lines += failures_report(idx["base"].values(), by_id) or ["（无）"]
    return "\n".join(lines) + "\n"


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.ab_subagents")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--report", default="bench/report-ab-subagents.md")
    p.add_argument("--db-dir", default="/tmp/ab_sub")
    args = p.parse_args(argv)

    db = Path(args.db_dir)
    db.mkdir(parents=True, exist_ok=True)
    cases = pick_cases(args.n)
    print(f"样本 {len(cases)} 条；命中线索词比例 "
          f"{sum(1 for c in cases if _has_hint(c.goal)) / max(1, len(cases)):.0%}")

    arms = []
    for name, sub, sk in (("base", "off", True), ("sub", "force", True),
                          ("noskill", "off", False)):
        t0 = time.perf_counter()
        arms.append(await run_arm(name, cases, subagents=sub, use_skills=sk, db_dir=db))
        print(f"{name}: pass@1={arms[-1]['aggregate']['pass@1']:.0%} "
              f"({time.perf_counter() - t0:.0f}s)")

    text = report(arms, cases)
    Path(args.report).write_text(text, encoding="utf-8")
    print(text[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
