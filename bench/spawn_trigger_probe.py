"""F11 触发判据的离线对照：旧判据（措辞）vs 新判据（KB 根因簇分裂）。

为什么单独做这个探针，而不是直接跑 LLM 消融：
判据的**触发面**与模型能力无关——它只依赖 goal 和知识库检索结果。先用 0 次
LLM 调用把「旧判据在真实分布上到底触发多少条、新判据触发的是哪一类」量清楚，
再决定要不要为它付两路 LLM 的钱。判据不改对就去做消融，是在给错误的东西测收益。

口径说明：
- 检索走 RagAssistant 的关键词通道（与 router 探针完全同一条路），不调 LLM；
- class_spread = 探针 top 命中里不同根因类的个数（去重）；
- 旧判据 = goal 含 MULTI_SOURCE_HINTS 里的词；
- 期望方向：单跳/多跳触发率显著高于旧判据，拒答/危险不因判据变化被误触发。

用法：
    python -m bench.spawn_trigger_probe --report bench/report-spawn-trigger.md
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from pathlib import Path

from bench.dataset import DANGEROUS, MULTI, REFUSE, SINGLE, Case
from oncall.router import route_request
from oncall.subagent import MULTI_SOURCE_HINTS

CATS = (SINGLE, MULTI, REFUSE, DANGEROUS)


async def _probe(cases: list[Case], *, split_min: int = 2) -> dict:
    from app.agents.rag_assistant import RagAssistant
    from app.core.llm_client import llm_client
    from app.domains.ssrm import pack as ssrm_pack

    rag = RagAssistant(llm_client, ssrm_pack.assistant)
    await rag.warmup()

    rows = []
    for c in cases:
        d = await route_request(c.goal, rag)
        spread = int(d.signals.get("class_spread") or 0)
        old = any(h in c.goal for h in MULTI_SOURCE_HINTS)
        strong = bool(d.signals.get("strong_cover"))
        rows.append({
            "id": c.id, "category": c.category, "route": d.route,
            "spread": spread, "classes": d.signals.get("candidate_classes") or [],
            "strong": strong, "old": old,
            # 护栏后的判据：簇分裂 **且** 强覆盖（弱覆盖的多簇是撞词，见报告结论）
            "new": spread >= split_min and strong,
            "raw": spread >= split_min,
        })
    return {"rows": rows}


def report(data: dict, *, split_min: int) -> str:
    rows = data["rows"]
    lines = ["# F11 触发判据对照：措辞 vs 根因簇分裂", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
             f"- 样本：{len(rows)} 条（单跳/多跳/拒答/危险）",
             "- 检索：RagAssistant 关键词通道（与路由探针同一条路，0 次 LLM 调用）",
             f"- 新判据：class_spread ≥ {split_min} **且 strong_cover**"
             "（弱覆盖的多簇是词面撞词，不是多解）", "",
             "## 触发率按类别", "",
             "| 类别 | 条数 | 旧判据（措辞） | 簇分裂（无护栏） | 新判据（含护栏） |",
             "|---|---|---|---|---|"]
    for cat in CATS:
        sub = [r for r in rows if r["category"] == cat]
        if not sub:
            continue
        n = len(sub)
        old = sum(1 for r in sub if r["old"])
        raw = sum(1 for r in sub if r["raw"])
        new = sum(1 for r in sub if r["new"])
        lines.append(f"| {cat} | {n} | {old}（{old / n:.0%}） | {raw}（{raw / n:.0%}） | "
                     f"{new}（{new / n:.0%}） |")

    tot = len(rows)
    old_all = sum(1 for r in rows if r["old"])
    raw_all = sum(1 for r in rows if r["raw"])
    new_all = sum(1 for r in rows if r["new"])
    lines += ["", f"合计：旧判据 **{old_all}/{tot}（{old_all / tot:.1%}）** → "
                  f"簇分裂无护栏 {raw_all}/{tot}（{raw_all / tot:.1%}）→ "
                  f"含护栏 **{new_all}/{tot}（{new_all / tot:.1%}）**", "",
              "## 新判据触发条目的路由分布（触发后实际会进哪条路）", "",
              "| 类别 | agent | pipeline | refuse |", "|---|---|---|---|"]
    for cat in CATS:
        sub = [r for r in rows if r["category"] == cat and r["new"]]
        if not sub:
            continue
        c = Counter(r["route"] for r in sub)
        lines.append(f"| {cat} | {c.get('agent', 0)} | {c.get('pipeline', 0)} | {c.get('refuse', 0)} |")

    lines += ["", "## 只看 agent 路由（并行取证真实发生的那部分）", ""]
    agent_rows = [r for r in rows if r["route"] == "agent"]
    if agent_rows:
        c = Counter(r["category"] for r in agent_rows)
        hit = [r for r in agent_rows if r["new"]]
        lines.append(f"- agent 路由共 {len(agent_rows)} 条：{dict(c)}")
        lines.append(f"- 其中新判据触发 {len(hit)} 条（{len(hit) / len(agent_rows):.0%}）")
        spread_dist = Counter(r["spread"] for r in agent_rows)
        lines.append(f"- 簇数分布：{dict(sorted(spread_dist.items()))}")
    else:
        lines.append("- （无 agent 路由条目）")

    lines += ["", "## 结论与限制", ""]
    safe = [r for r in rows if r["category"] in (REFUSE, DANGEROUS)]
    safe_hit = [r for r in safe if r["new"]]
    agent_rows = [r for r in rows if r["route"] == "agent"]
    agent_hit = [r for r in agent_rows if r["new"]]
    lines += [
        f"- 安全面：拒答+危险共 {len(safe)} 条，含护栏判据触发 {len(safe_hit)} 条"
        "（护栏的目的就是让弱覆盖的假分裂不触发）",
        f"- **agent 路径实际并行面**：agent {len(agent_rows)} 条里触发 {len(agent_hit)} 条"
        f"（{len(agent_hit) / max(1, len(agent_rows)):.0%}）——这是首轮判据的真实覆盖上限",
        "- 首轮判据在单域 4 工具场景下作用有限（真实 goal 既少措辞线索、簇分裂又多见于库外），"
        "所以 F11 的 auto 模式另有**门禁退回触发**：退回时类名已确定、缺口已知，"
        "history+topology 两路并行才有根据（见 F11-subagent.md §触发时机）",
        "- 该探针只量触发面，不量收益；收益（pass@1 / token / 延迟）必须跑真模型 A/B",
        f"- 阈值 split_min={split_min} 与 decide_spawn 的 SPLIT_MIN_CLASSES 必须保持一致",
    ]
    return "\n".join(lines) + "\n"


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.spawn_trigger_probe")
    p.add_argument("--dataset", choices=("core", "large"), default="large")
    p.add_argument("--split-min", type=int, default=2)
    p.add_argument("--report", default="bench/report-spawn-trigger.md")
    args = p.parse_args(argv)

    if args.dataset == "large":
        from bench.dataset_large import build_cases_large

        cases = build_cases_large()
    else:
        from bench.dataset import build_cases

        cases = build_cases()

    data = await _probe(cases, split_min=args.split_min)
    text = report(data, split_min=args.split_min)
    Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
