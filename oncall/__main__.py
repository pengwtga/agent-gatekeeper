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

from oncall.api import Deps, RunOptions, exit_code, run_incident
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

    ask = sub.add_parser("ask", help="一句话提问，跑一次排障")
    ask.add_argument("goal")
    common(ask)

    run = sub.add_parser("run", help="按事故号跑一次排障")
    run.add_argument("--incident", required=True)
    common(run)

    resume = sub.add_parser("resume", help="从 checkpoint 续跑（I2）")
    resume.add_argument("run_id")

    approve = sub.add_parser("approve", help="处理挂起的写操作（I2）")
    approve.add_argument("run_id")
    approve.add_argument("--decision", choices=("allow", "deny"), required=True)

    mcp = sub.add_parser("serve", help="作为 MCP server 被外部 host 调用（I3）")
    mcp.add_argument("--transport", default="stdio", choices=("stdio",))

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
    if o.reason:
        lines.append(f"原因：{o.reason}")
    return "\n".join(lines)


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
                      max_tokens=args.max_tokens, domain=args.domain)
    result = await run_incident(goal, deps=deps, opts=opts)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_text(result))
    return exit_code(result)


def main(argv: Optional[Sequence[str]] = None, deps_factory: DepsFactory = build_default_deps) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd in ("resume", "approve", "serve"):
        # 如实报「未实现」，不假装成功：I2/I3 才落库审批与 MCP server
        print(f"'{args.cmd}' 将在 I2/I3 实现（见 docs/v2/F05、F06、F09）", file=sys.stderr)
        return EXIT_UNIMPLEMENTED

    return asyncio.run(_run(args, deps_factory))


if __name__ == "__main__":
    sys.exit(main())
