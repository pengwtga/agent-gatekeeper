"""通用工具调用层（T5-0）：tool registry + LLM 选工具 + 校验执行

为什么自己写 80 行而不用 Function Call / LangChain（面试重点）：
1. 项目其他链路全部走 JSON mode（glm-4-flash 不支持 response_format，且 JSON mode
   跨 Provider 行为一致、可评测）；Function Call 是另一套协议，混用会双倍评测面
2. LangChain 的 AgentExecutor 把"选了哪个工具、参数是什么、为什么"埋进抽象里——
   而本项目的立身之本恰恰是每一步可观测、可门禁、可评测
3. 工具调用的风险等级比纯文本高一级（有副作用），所以它必须过与分类器同一套门禁哲学：
   LLM 的选择只是"建议"，schema 校验 + 白名单 + 置信度门禁之后才执行

执行契约：
- 工具在注册表里声明 name / description / params(JSON Schema 子集) / read_only；
- LLM 只负责「选哪个工具 + 给参数 + 自报置信度」，输出与分类器同构；
- 引擎做三道关：工具必须已注册（白名单）、参数必须过 schema、置信度过低不执行；
- read_only=False 的工具当前一律拒绝执行（返回需要人工）——副作用工具接真实
  权限系统之前，宁可让它空手而归。
"""

import json as _json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.core.budget import Budget
from app.observability.tracing import span

logger = logging.getLogger(__name__)

# 允许的参数类型：JSON Schema 的子集，校验逻辑自己写（见 _check_type）
_PARAM_TYPES = ("string", "number", "integer", "boolean")


@dataclass
class Tool:
    """一个可被 LLM 选用的工具。handler 必须是 async，返回可 JSON 序列化的 dict。"""

    name: str
    description: str
    params: dict[str, dict]  # {"参数名": {"type": "...", "description": "...", "required": bool}}
    handler: Callable[..., Awaitable[dict[str, Any]]]
    read_only: bool = True


def _check_type(value: Any, type_name: str) -> bool:
    checks = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
    }
    return checks[type_name](value)


class ToolRegistry:
    """工具注册表：白名单的唯一来源。LLM 只能从这里选，参数只能按这里声明的校验。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        for pname, spec in tool.params.items():
            if spec.get("type") not in _PARAM_TYPES:
                raise ValueError(f"工具 {tool.name} 参数 {pname} 类型非法: {spec.get('type')}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def specs(self) -> list[dict]:
        """给 LLM 看的工具清单（handler 不暴露）"""
        return [
            {"name": t.name, "description": t.description, "params": t.params}
            for t in self._tools.values()
        ]

    def validate_args(self, tool: Tool, args: Any) -> tuple[bool, str]:
        """schema 校验：必填不缺、类型正确、不收声明外的参数（防注入多余字段）"""
        if not isinstance(args, dict):
            return False, "args 必须是对象"
        for pname, spec in tool.params.items():
            if pname in args:
                if not _check_type(args[pname], spec["type"]):
                    return False, f"参数 {pname} 期望 {spec['type']}，实际 {type(args[pname]).__name__}"
            elif spec.get("required"):
                return False, f"缺少必填参数 {pname}"
        extra = set(args) - set(tool.params)
        if extra:
            return False, f"声明外的参数: {sorted(extra)}"
        return True, ""


_SELECTION_PROMPT = """你是工具调用决策器。根据用户问题，从工具清单中选择至多一个最合适的工具并给出参数；都不合适则不选。
规则：
1. 只能选工具清单里存在的工具，参数只给该工具声明过的参数
2. confidence 为 0~1：问题与工具职责的匹配程度；拿不准就给低分
3. 不选任何工具时输出 {"tool": null}
4. 只输出 JSON：{"tool": "名称或null", "args": {...}, "confidence": 0.0, "reason": "一句话理由"}"""


async def choose_and_run(
    llm,
    registry: ToolRegistry,
    question: str,
    budget: Optional[Budget] = None,
    min_confidence: float = 0.7,
) -> dict[str, Any]:
    """通用「选工具 → 校验 → 执行」引擎。返回值对 LLM 的选择和引擎的裁决都有留痕。

    与分类器同构：LLM 的输出是建议，引擎的三道关（白名单 / schema / 置信度）
    之后才是"系统行为"——这个层次划分让工具调用也可以进评测集。
    """
    specs = _json.dumps(registry.specs(), ensure_ascii=False)
    with span("tool.choose", tools=len(registry)) as s:
        selection = await llm.chat_json(
            _SELECTION_PROMPT,
            f"工具清单：{specs}\n\n用户问题：{question}",
            budget=budget,
        )
        s["attrs"]["tool"] = selection.get("tool")

    tool_name = selection.get("tool")
    confidence = selection.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(selection.get("reason") or "")

    if not tool_name:
        return {"tool": None, "answer": None, "confidence": confidence,
                "reason": reason, "executed": False,
                "note": "没有合适的工具，建议直接回答或转人工"}

    tool = registry.get(tool_name)
    if tool is None:
        # 白名单关：LLM 编造的工具名到这里为止
        return {"tool": tool_name, "answer": None, "confidence": confidence,
                "reason": reason, "executed": False, "note": "工具不在注册表中，拒绝执行"}

    args = selection.get("args") or {}
    ok, err = registry.validate_args(tool, args)
    if not ok:
        # schema 关：参数缺失/类型错/声明外字段，都不给执行
        return {"tool": tool.name, "args": args, "answer": None, "confidence": confidence,
                "reason": reason, "executed": False, "note": f"参数校验失败: {err}"}

    if not tool.read_only:
        return {"tool": tool.name, "args": args, "answer": None, "confidence": confidence,
                "reason": reason, "executed": False,
                "note": "非只读工具需要接入权限系统后才能自动执行，转人工"}

    if confidence < min_confidence:
        # 置信度关：与分类器同一个哲学——低置信宁可不执行
        return {"tool": tool.name, "args": args, "answer": None, "confidence": confidence,
                "reason": reason, "executed": False,
                "note": f"置信度 {confidence:.2f} 低于 {min_confidence}，不执行"}

    with span("tool.call", tool=tool.name) as s:
        result = await tool.handler(**args)
        s["attrs"]["status"] = "ok"
    return {"tool": tool.name, "args": args, "answer": result,
            "confidence": confidence, "reason": reason, "executed": True}
