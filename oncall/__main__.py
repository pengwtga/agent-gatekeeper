"""CLI 入口：argv → Deps → run_incident → 打印。

这一层只允许做三件事：解析参数、装配依赖、渲染输出。
任何「先查一下再判断」的逻辑都必须写在 loop/policy/sensors 里，否则
进程内调用和 CLI 就会有两套行为——那是这类项目最常见的腐烂方式。
"""

import argparse
import asyncio
import json
import sys
from typing import Awaitable, Callable, Optional, Sequence

from oncall.api import Deps, RunOptions, approve_incident, exit_code, resume_incident, run_incident
from oncall.deps import ConfigError, build_default_deps

DepsFactory = Callable[[str], Awaitable[Deps]]

EXIT_CONFIG = 4
EXIT_UNIMPLEMENTED = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oncall", description="SSRM 值班排障 agent（harness 版）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--domain", default="ssrm", choices=("ssrm", "csa", "credit"))
        sp.add_argument("--max-steps", type=int, default=12)
        sp.add_argument("--budget-ms", type=int, default=90_000)
        sp.add_argument("--max-tokens", type=int, default=100_000,
                        help="本次 run 的 token 总量上限（prompt+completion）")
        sp.add_argument("--json", action="store_true", help="输出结构化结果（给脚本/评测用）")
        sp.add_argument("--verbose", action="store_true", help="把每步的模型原始输出打到 stderr")
        sp.add_argument("--repairs", type=int, default=1,
                        help="finish 被门禁退回后给几轮修正（0=关闭，退回即转人工）")
        sp.add_argument("--no-auto-evidence", action="store_true",
                        help="关闭确定性证据补齐（只把门禁错误反馈给模型，不让 harness 代查）")
        sp.add_argument("--no-skills", action="store_true",
                        help="关闭技能库（不注入索引，也不注册 load_skill）")
        sp.add_argument("--subagents", choices=("auto", "off", "force"), default="auto",
                        help="并行取证触发：auto=多源线索才 spawn；off=关；force=总是")

    ask = sub.add_parser("ask", help="一句话提问，跑一次排障")
    ask.add_argument("goal")
    common(ask)

    run = sub.add_parser("run", help="按事故号跑一次排障")
    run.add_argument("--incident", required=True)
    common(run)

    resume = sub.add_parser("resume", help="从 checkpoint 续跑（审批/预算/崩溃后）")
    resume.add_argument("run_id")
    resume.add_argument("--json", action="store_true", help="输出结构化结果（给脚本/评测用）")

    approve = sub.add_parser("approve", help="处理挂起的写操作（allow/deny 后自动续跑）")
    approve.add_argument("run_id")
    approve.add_argument("--decision", choices=("allow", "deny"), required=True)
    approve.add_argument("--by", default="cli", help="审批人标识，落库供审计")
    approve.add_argument("--json", action="store_true", help="输出结构化结果（给脚本/评测用）")

    mcp = sub.add_parser("serve", help="作为 MCP server 被外部 host 调用（只读工具，stdio）")
    mcp.add_argument("--domain", default="ssrm", choices=("ssrm", "csa", "credit"))
    mcp.add_argument("--transport", default="stdio", choices=("stdio",))

    trace = sub.add_parser("trace", help="查看一个 run 的步骤时间轴（F14 面板详情页的 CLI 版）")
    trace.add_argument("run_id")

    runs = sub.add_parser("runs", help="列出最近的 run（状态/步数/token，挂起的标 ⏳）")
    runs.add_argument("--limit", type=int, default=20)

    return p


def _render_text(result) -> str:
    o = result.outcome
    u = o.usage
    head = (f"run {result.run_id}  status={o.status}  "
            f"{u.get('steps', 0)} 步 / {u.get('wall_ms', 0)} ms / "
            f"{u.get('prompt_tokens', 0) + u.get('completion_tokens', 0)} tokens")
    lines = [head]
    if o.conclusion:
        lines.append(f"结论：{o.conclusion}")
    if o.evidence:
        fields = ",".join(sorted({str(e.get('field', '')) for e in o.evidence}))
        lines.append(f"证据：{len(o.evidence)} 条（{fields}）")
    if o.pending_approval:
        pa = o.pending_approval
        lines.append(f"待审批：{pa['tool']}({json.dumps(pa.get('args', {}), ensure_ascii=False)})"
                     f"  风险={pa.get('risk')}  需 {pa.get('requires')} 人确认")
    if o.repairs:
        filled = sum(1 for r in o.repairs if r.get("auto_filled"))
        lines.append(f"门禁退回：{len(o.repairs)} 轮（其中 {filled} 轮由 harness 自动补齐 history）")
    if o.subagents:
        ok = sum(1 for s in o.subagents if s.get("summary") and not s.get("error"))
        lines.append(f"并行取证：{len(o.subagents)} 路（{ok} 路有结论）"
                     + ("，存在冲突" if any(f.get("field") == "conflict" for f in o.facts) else ""))
    if o.reason:
        lines.append(f"原因：{o.reason}")
    return "\n".join(lines)


async def _run_runcontrol(args: argparse.Namespace, deps_factory: DepsFactory) -> int:
    """resume / approve 的公共入口：装配依赖 → 调 api → 渲染 → 退出码。"""
    try:
        deps = await deps_factory("ssrm")
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if deps.store is None:
        print("resume/approve 需要持久化存储（ONCALL_DB，默认 data/oncall.sqlite3）",
              file=sys.stderr)
        return EXIT_CONFIG

    try:
        if args.cmd == "resume":
            result = await resume_incident(args.run_id, deps=deps)
        else:
            result = await approve_incident(args.run_id, args.decision, deps=deps)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_UNIMPLEMENTED

    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(result))
    return exit_code(result)


async def _run(args: argparse.Namespace, deps_factory: DepsFactory) -> int:
    goal = args.goal if args.cmd == "ask" else f"排查事故 {args.incident}"
    try:
        deps = await deps_factory(args.domain)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.verbose:
        def _echo(kind: str, payload: dict) -> None:
            print(f"[{kind}] {json.dumps(payload, ensure_ascii=False)[:400]}", file=sys.stderr)

        deps.observer = _echo

    opts = RunOptions(max_steps=args.max_steps, budget_ms=args.budget_ms,
                      max_tokens=args.max_tokens, domain=args.domain,
                      max_finish_repairs=args.repairs,
                      auto_evidence=not args.no_auto_evidence,
                      use_skills=not args.no_skills,
                      subagents=args.subagents)
    result = await run_incident(goal, deps=deps, opts=opts)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(result))
    return exit_code(result)


def main(argv: Optional[Sequence[str]] = None, deps_factory: DepsFactory = build_default_deps) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "serve":
        return _serve(args)
    if args.cmd in ("resume", "approve"):
        return asyncio.run(_run_runcontrol(args, deps_factory))
    if args.cmd in ("trace", "runs"):
        return _observe(args)

    return asyncio.run(_run(args, deps_factory))


def _observe(args: argparse.Namespace) -> int:
    """F14 面板的 CLI 版：只读 store，不要求 LLM 配置（看轨迹不需要 Key）。"""
    from oncall.deps import build_store
    from oncall.observe import format_runs_table, format_trace

    store = build_store()
    try:
        if args.cmd == "trace":
            run = store.load_run(args.run_id)
            if run is None:
                print(f"run {args.run_id} 不存在（库：{store.path}）", file=sys.stderr)
                return EXIT_UNIMPLEMENTED
            print(format_trace(run, store))
        else:
            rows = store.list_runs(limit=args.limit)
            if not rows:
                print(f"库 {store.path} 里还没有 run")
            else:
                print(format_runs_table(rows, store))
        return 0
    finally:
        store.close()


def _serve(args: argparse.Namespace) -> int:
    """F09：以 MCP server 身份常驻（stdio 行协议，由外部 host 拉起）。"""
    from oncall.deps import build_serve_registry
    from oncall.mcp.server import McpServer

    try:
        registry = asyncio.run(build_serve_registry(args.domain))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    McpServer(registry, name=f"oncall-{args.domain}").serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
