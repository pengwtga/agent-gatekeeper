"""唯一的运行入口：`run_incident()`。

CLI、进程内调用、MCP server、将来的 HTTP 都调这一个函数，谁都不许在入口层
自己写一遍循环。这样「业务层怎么用 CLI」这个问题就不存在了——业务层不经过 CLI，
它直接调这里；CLI 只是一层 argv → 参数 → 打印。

依赖全部从 `Deps` 注入（LLM / 工具注册表 / 时钟）。不读全局单例的原因：
1. 业务方要把审批通道换成 IM 审批、把 LLM 换成内部网关，改参数就行
2. 测试要换脚本化 LLM 和假时钟，读 `os.environ` 的写法根本做不到
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.core.tools import ToolRegistry
from oncall.clock import Clock, SystemClock
from oncall.loop import BUDGET_EXHAUSTED, FAILED, NEEDS_HUMAN, OK, Outcome, run_loop

# 退出码语义（F01 §5）：把「需人工」和「失败」分开是产品语义，不是技术细节
EXIT_CODES = {OK: 0, NEEDS_HUMAN: 2, BUDGET_EXHAUSTED: 3, FAILED: 1}


@dataclass
class RunOptions:
    max_steps: int = 12
    budget_ms: int = 90_000
    # token 预算：本次 run 的 prompt+completion 总量上限。12 步 × 每步约 4~6K，
    # 正常排障 30~50K 就够，100K 是兜底不是常态（到顶说明在打转，步数/无进展会先拦）。
    max_tokens: int = 100_000
    min_confidence: float = 0.7
    domain: str = "ssrm"


@dataclass
class Deps:
    llm: Any
    registry: ToolRegistry
    clock: Clock = field(default_factory=SystemClock)
    # 观测钩子：CLI 的 --verbose 用它把每一步的原始输出打到 stderr。
    # 用回调而不是 print：loop 不该知道「输出到哪」，测试里也可以收进列表断言。
    observer: Optional[Callable[[str, dict], None]] = None


@dataclass
class RunResult:
    run_id: str
    goal: str
    outcome: Outcome

    @property
    def status(self) -> str:
        return self.outcome.status

    def to_dict(self) -> dict[str, Any]:
        o = self.outcome
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": o.status,
            "conclusion": o.conclusion,
            "root_cause_class": o.root_cause_class,
            "evidence": o.evidence,
            "reason": o.reason,
            "steps": [s.to_dict() for s in o.steps],
            "usage": o.usage,
            "sensors": o.sensors,
            "pending_approval": o.pending_approval,
        }


def new_run_id() -> str:
    return f"r-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


async def run_incident(goal: str, *, deps: Deps, opts: Optional[RunOptions] = None) -> RunResult:
    opts = opts or RunOptions()
    run_id = new_run_id()
    outcome = await run_loop(
        goal,
        llm=deps.llm,
        registry=deps.registry,
        clock=deps.clock,
        run_id=run_id,
        max_steps=opts.max_steps,
        budget_ms=opts.budget_ms,
        token_budget=opts.max_tokens,
        min_confidence=opts.min_confidence,
        observer=deps.observer,
    )
    return RunResult(run_id=run_id, goal=goal, outcome=outcome)


def exit_code(result: RunResult) -> int:
    return EXIT_CODES.get(result.status, 1)
