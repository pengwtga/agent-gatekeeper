"""工具适配层：复用 v1 的四道关，补上「副作用等级 + 幂等 + 截断」。

为什么不改 `app/core/tools.py`：
- v1 的 `Tool` 是「分类服务的工具」，它的 `read_only` 表达的是**能不能自动执行**；
- v2 需要的是**执行策略的输入**（副作用等级 / 幂等 / 审批要求）。
后者属于 harness，不属于工具实现——放在一起会让 v1 为 v2 的需求改接口，
也会让「副作用」这一个概念散落在两个包里。

代价是「声明离实现远」，所以用一条测试兜底：注册表里的每个工具都必须在
`SIDE_EFFECTS` 里显式声明，**未声明 = 按最危险的等级（WRITE）处理**，fail-closed。
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.core.tools import ToolRegistry

# 单次工具结果进上下文的字符上限。头 500 字通常是状态/时间，尾 200 字通常是结论/异常，
# 中间大概率是重复堆栈——砍中间比砍尾巴安全。
MAX_RESULT_CHARS = 800
_HEAD, _TAIL = 500, 200


class SideEffect(str, Enum):
    READ = "read"                # 只读，自动执行
    WRITE = "write"             # 有副作用，需审批
    DESTRUCTIVE = "destructive"  # 不可逆，默认不自动执行，需更高等级确认


# 副作用声明表。键是工具名，值是等级。
# 未列出的工具按 WRITE 处理（fail-closed）：新增工具忘了声明，最坏结果是多问一次人，
# 而不是悄悄放行一个写操作。
SIDE_EFFECTS: dict[str, SideEffect] = {
    "kb_stats": SideEffect.READ,
    "query_incidents": SideEffect.READ,
    "system_topology": SideEffect.READ,
    "rag_search": SideEffect.READ,  # F12：只读检索（多轮改写在工具内部，不占主 loop 步数）
    "load_skill": SideEffect.READ,  # F10：读取技能正文，不产生任何副作用
}

# 幂等声明：重放（resume）时，只有幂等的写操作可以直接重做
IDEMPOTENT: dict[str, bool] = {
    "add_ticket_comment": True,
}

# 单工具截断上限覆盖：load_skill 返回的是「打法正文」，砍掉一半就没有执行价值，
# 所以给它单独一档。其余工具仍用默认 800——证据引用不该依赖长文本。
MAX_RESULT_OVERRIDES: dict[str, int] = {
    "load_skill": 3000,
}


@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""
    truncated: bool = False
    full_ref: str = ""
    ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "value": self.value, "error": self.error,
                "truncated": self.truncated, "full_ref": self.full_ref, "ms": self.ms}


def side_effect_of(name: str) -> SideEffect:
    return SIDE_EFFECTS.get(name, SideEffect.WRITE)


def is_idempotent(name: str) -> bool:
    return IDEMPOTENT.get(name, False)


def specs_for_prompt(registry: ToolRegistry) -> list[dict]:
    """给模型看的工具清单：只保留能让它做对选择的信息。

    刻意不带 side_effect：模型知道「这个工具要审批」并不会让它少犯错，
    反而可能诱导它绕路。策略是 harness 的事。
    """
    return registry.specs()


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    # 上限太小（<700）时按比例切，避免 head+tail 超过 limit
    head = min(_HEAD, limit - 100)
    tail = min(_TAIL, max(0, limit - head - 40))
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…省略 {omitted} 字符…\n{text[-tail:] if tail else ''}", True


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


async def call_tool(
    registry: ToolRegistry,
    name: str,
    args: dict,
    *,
    full_ref: str = "",
) -> ToolResult:
    """白名单 → schema → 执行 → 截断。错误一律返回结构，不抛异常。

    抛异常会让模型看不见原因（栈对它没有意义），它只能瞎猜重试；
    返回「参数缺少 incident_id」这种话，它才有机会改对。
    """
    tool = registry.get(name)
    if tool is None:
        return ToolResult(ok=False, error=f"工具 {name} 不在注册表中")

    ok, err = registry.validate_args(tool, args)
    if not ok:
        return ToolResult(ok=False, error=f"参数校验失败: {err}")

    started = time.perf_counter()
    try:
        value = await tool.handler(**args)
    except Exception as exc:  # noqa: BLE001 —— 工具失败必须变成可读结果，由 loop 决定下一步
        return ToolResult(ok=False, error=f"执行失败: {type(exc).__name__}: {exc}",
                          ms=int((time.perf_counter() - started) * 1000))

    text, truncated = _truncate(_render(value), MAX_RESULT_OVERRIDES.get(name, MAX_RESULT_CHARS))
    return ToolResult(
        ok=True,
        value=text if truncated else value,
        truncated=truncated,
        full_ref=full_ref if truncated else "",
        ms=int((time.perf_counter() - started) * 1000),
    )


@dataclass
class ToolCallRecord:
    """一步工具调用（落 steps / 进 trace 的同一份数据）"""

    i: int
    tool: str = ""
    args: dict = field(default_factory=dict)
    ok: bool = False
    error: str = ""
    ms: int = 0
    truncated: bool = False
    skipped: Optional[str] = None  # 非空表示未执行：unknown_tool / denied / approval

    def to_dict(self) -> dict[str, Any]:
        return {"i": self.i, "tool": self.tool, "args": self.args, "ok": self.ok,
                "error": self.error, "ms": self.ms, "truncated": self.truncated,
                "skipped": self.skipped}
