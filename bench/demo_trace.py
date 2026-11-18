"""把一次排障的完整链路录成 markdown（F17 讲解材料 / 复盘用）。

它回答一个具体问题：**每一轮模型到底看到了什么、上一轮的结果怎么进到下一轮**。
做法不是画示意图，而是把真实的 `build_messages()` 产物按段拆出来——
所以文档里的「已确认事实 / 最近步骤 / 上一轮被门禁退回」都是模型当时看到的那几段原文。

用法（真模型，需要 .env 里的 Provider）：
    python -m bench.demo_trace --case SH-01 --out docs/v2/example-trace.md
桩模式（不联网，验证链路）：
    python -m bench.demo_trace --case SH-01 --stub
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


class RecordingLLM:
    """套在真模型外面，录下每一轮的 system/user 与原始输出。

    为什么不改 loop 加回调：观测不该侵入被测代码——同一份 loop 代码，
    评测用 ScriptedLLM、演示用 RecordingLLM、生产用真客户端。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.rounds: list[dict] = []

    async def chat_json(self, system_prompt: str, user_payload: str, budget: Any = None) -> dict:
        rec: dict[str, Any] = {"system": system_prompt, "user": user_payload}
        self.rounds.append(rec)
        t0 = time.perf_counter()
        out = await self.inner.chat_json(system_prompt, user_payload, budget=budget)
        rec["ms"] = int((time.perf_counter() - t0) * 1000)
        rec["out"] = out
        return out


def split_sections(payload: str) -> dict[str, str]:
    """把 build_messages 的 user 段按 `## 标题` 切开，便于逐段展示。"""
    body: dict[str, str] = {}
    current = ""
    for line in payload.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            body[current] = ""
        elif current:
            body[current] += line + "\n"
    return {k: v.strip() for k, v in body.items()}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"


def render(case: Any, result: Any, llm: RecordingLLM, events: list[dict], limit: int) -> str:
    o = result.outcome
    lines = [
        f"# 一次排障的完整链路：{case.id}",
        "",
        f"- 输入（goal）：`{case.goal}`",
        f"- 终态：**{o.status}**，{len(o.steps)} 步 / "
        f"{(o.usage.get('prompt_tokens') or 0) + (o.usage.get('completion_tokens') or 0)} tokens / "
        f"{o.usage.get('wall_ms')} ms",
        f"- 结论：{o.conclusion or '（未给出）'}",
        f"- 根因类：`{o.root_cause_class}`　门禁退回：{len(o.repairs)} 轮",
        "",
        "> 每一轮的「输入」都是 `oncall.context.build_messages()` 的真实输出（按段摘录），",
        "> 不是事后重写的示意。工具返回进事实表、事实表进下一轮输入，这条链在这里可逐轮核对。",
        "",
    ]

    for idx, rec in enumerate(llm.rounds, start=1):
        lines.append(f"## 轮次 {idx}")
        secs = split_sections(rec["user"])

        lines.append("**模型本轮看到的关键段**")
        facts = secs.get("已确认事实", "")
        lines.append(f"- 已确认事实：{_clip(facts, limit)}")
        if "历史同类" in secs:
            lines.append(f"- 历史同类（记忆）：{_clip(secs['历史同类'], limit)}")
        steps = secs.get("最近步骤", "")
        lines.append(f"- 最近步骤：{_clip(steps, limit)}")
        if "上一轮被门禁退回" in secs:
            lines.append(f"- **上一轮被门禁退回**：{_clip(secs['上一轮被门禁退回'], limit)}")
        lines.append("")

        out = rec.get("out") or {}
        lines.append(f"**模型输出**（{rec.get('ms', 0)} ms）")
        lines.append("```json")
        lines.append(_clip(json.dumps(out, ensure_ascii=False, indent=2), limit * 3))
        lines.append("```")
        lines.append("")

        # 这一轮之后 harness 干了什么：从事件流里按顺序取
        actions = [e for e in events if e["round"] == idx]
        if actions:
            lines.append("**harness 动作**")
            for e in actions:
                p = e["payload"]
                if e["kind"] == "tool":
                    state = "ok" if p.get("ok") else f"失败：{p.get('error', '')[:70]}"
                    lines.append(f"- 执行工具 `{p.get('tool')}` args={json.dumps(p.get('args'), ensure_ascii=False)}"
                                 f" → {state}（{p.get('ms')} ms）")
                elif e["kind"] == "repair":
                    lines.append(f"- 门禁退回：{'；'.join(p.get('errors') or [])}"
                                 f"（自动补齐 history：{'是' if p.get('auto_filled') else '否'}）")
            lines.append("")

    lines.append("## 终态")
    lines.append("")
    lines.append(f"- 证据：{json.dumps(o.evidence, ensure_ascii=False)}")
    if o.sensors:
        lines.append(f"- 传感器：{json.dumps(o.sensors, ensure_ascii=False)}")
    if o.repairs:
        lines.append(f"- 退回记录：{json.dumps(o.repairs, ensure_ascii=False)}")
    if o.reason:
        lines.append(f"- 原因：{o.reason}")
    lines.append("")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    from bench.dataset import build_cases
    from oncall.api import Deps, RunOptions, run_incident
    from oncall.deps import build_registry
    from oncall.store import RunStore

    case = next((c for c in build_cases() if c.id == args.case), None)
    if case is None:
        print(f"没有 case {args.case}")
        return 1

    registry = await build_registry("ssrm", require_llm=not args.stub)
    if args.stub:
        from bench.stub import ScriptedLLM

        inner: Any = ScriptedLLM(case.plan)
    else:
        from app.core.llm_client import llm_client

        inner = llm_client
    llm = RecordingLLM(inner)

    events: list[dict] = []
    state = {"round": 0}

    def observer(kind: str, payload: dict) -> None:
        if kind == "llm":
            state["round"] = len(llm.rounds)
            return
        events.append({"kind": kind, "round": state["round"], "payload": payload})

    db = Path(args.db_dir) / f"{case.id}.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(db).init()
    try:
        deps = Deps(llm=llm, registry=registry, store=store, observer=observer)
        result = await run_incident(case.goal, deps=deps,
                                   opts=RunOptions(max_finish_repairs=args.repairs,
                                                   auto_evidence=not args.no_auto_evidence))
    finally:
        store.close()

    md = render(case, result, llm, events, args.max_chars)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"写入 {args.out}（{len(llm.rounds)} 轮，终态 {result.status}）")
    else:
        print(md)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.demo_trace", description="录一次排障的逐轮链路")
    p.add_argument("--case", default="SH-01")
    p.add_argument("--out", default="")
    p.add_argument("--stub", action="store_true", help="用桩 LLM（不联网）")
    p.add_argument("--repairs", type=int, default=1)
    p.add_argument("--no-auto-evidence", action="store_true")
    p.add_argument("--max-chars", type=int, default=420)
    p.add_argument("--db-dir", default="/tmp/oncall_trace")
    return asyncio.run(_main_async(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
