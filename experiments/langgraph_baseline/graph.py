"""基线 C：LangGraph 最小 agent（F13 §6，~120 行）。

定位：回答「差距是来自 loop 本身，还是来自治理」。所以它**刻意不带**审批/压缩/传感器
——那是 B（自研 harness）的差异点；但步数与预算必须对齐，否则比的是预算不是架构。

公平性对齐（bench/FAIRNESS.md 逐条打勾）：
- 同一份工具实现：直接 import v1 registry，不手抄；
- 同一个 LLM 客户端：chat_json 适配成 node 里的调用，同样 JSON mode；
- 同步数上限（12 步）：图内用计数器硬拦，不靠模型自觉；
- 关闭框架默认增强：不用 LangGraph 自带的 ToolNode 重试/裁剪，工具调用走与 B
  完全相同的 oncall.tools.call_tool（四道关里只复用「白名单+校验+截断」，审批/传感器
  是治理差异，不在 C 里复刻）。

依赖：langgraph（pip install langgraph）。未安装时 import 抛 LanggraphUnavailable，
bench --arm langgraph 会记录 skipped 并在报告里如实标注——不做假数据。
"""

from __future__ import annotations

import json
from typing import Any, Optional


class LanggraphUnavailable(RuntimeError):
    """环境未安装 langgraph。bench 捕获后记录 skipped，不伪装成跑过。"""


try:
    from langgraph.graph import END, StateGraph  # type: ignore
    _HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover —— CI 环境无 langgraph 时走这条路
    _HAS_LANGGRAPH = False
    LanggraphUnavailable = type("LanggraphUnavailable", (RuntimeError,), {})

MAX_STEPS = 12  # 与 B 对齐（RunOptions.max_steps）


def _action_of(out: dict) -> str:
    action = str(out.get("action") or "").strip().lower()
    if action in ("tool", "finish", "ask_human"):
        return action
    if out.get("conclusion"):
        return "finish"
    if out.get("tool"):
        return "tool"
    return "ask_human"


def build_graph(llm: Any, registry: Any, *, max_steps: int = MAX_STEPS):
    """agent → tools → (agent | END) 的最小环。

    state: {goal, facts, steps, tokens, i}。
    与 B 的差异点（刻意的）：无审批（write 工具照样执行）、无压缩、无 finish 传感器——
    这三项正是 B 要证明价值的治理能力。
    """
    if not _HAS_LANGGRAPH:
        raise LanggraphUnavailable("langgraph 未安装：pip install langgraph 后再跑基线 C")

    from oncall.context import build_messages  # 同一份上下文装配，排除 prompt 差异
    from oncall.tools import call_tool, specs_for_prompt

    async def agent_node(state: dict) -> dict:
        system, user = build_messages(state["goal"], state["facts"],
                                      state["steps"], specs_for_prompt(registry))
        out = await llm.chat_json(system, user, budget=None)
        usage = out.get("_usage") if isinstance(out, dict) else None
        tokens = state.get("tokens", 0)
        if isinstance(usage, dict):
            tokens += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        out["_tokens"] = tokens
        return {"_llm_out": out, "tokens": tokens}

    async def tools_node(state: dict) -> dict:
        out = state["_llm_out"]
        name, args = str(out.get("tool") or ""), out.get("args") or {}
        from oncall.tools import ToolCallRecord

        record = ToolCallRecord(i=state["i"], tool=name, args=args)
        result = await call_tool(registry, name, args)
        record.ok, record.error, record.ms = result.ok, result.error, result.ms
        steps = state["steps"] + [record]
        facts = state["facts"]
        if result.ok:
            facts = facts + [{"field": name, "value": json.dumps(result.value, ensure_ascii=False,
                                                               default=str)[:800],
                              "ref": f"langgraph#step:{state['i']}"}]
        return {"steps": steps, "facts": facts, "i": state["i"] + 1}

    def should_continue(state: dict) -> str:
        out = state.get("_llm_out") or {}
        if state["i"] > max_steps:
            return END  # 步数对齐：12 步硬拦，与 B 的 max_steps 一致
        action = _action_of(out)
        if action in ("finish", "ask_human"):
            return END
        return "tools"

    g = StateGraph(dict)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile()


async def run_case(goal: str, *, llm: Any, registry: Any,
                   max_steps: int = MAX_STEPS) -> dict[str, Any]:
    """单条 case 的 C 组入口。返回与 B 的 RunResult.to_dict() 同形的 dict（judge 共用）。"""
    g = build_graph(llm, registry, max_steps=max_steps)
    state = {"goal": goal, "facts": [], "steps": [], "tokens": 0, "i": 1}
    final: Optional[dict] = None
    async for chunk in g.astream(state, stream_mode="values"):
        final = chunk
    final = final or state
    out = final.get("_llm_out") or {}
    action = _action_of(out)
    status = "ok" if action == "finish" else "needs_human"
    steps = final.get("steps") or []
    return {
        "status": status,
        "conclusion": str(out.get("conclusion") or ""),
        "root_cause_class": str(out.get("root_cause_class") or "unknown"),
        "evidence": out.get("evidence") if isinstance(out.get("evidence"), list) else [],
        "pending_approval": None,  # C 无审批（治理差异点）
        "reason": "ask_human（LangGraph 基线）" if action == "ask_human" else "",
        "steps": [s.to_dict() if hasattr(s, "to_dict") else s for s in steps],
        "usage": {"steps": len(steps),
                  "wall_ms": 0,
                  "prompt_tokens": 0, "completion_tokens": 0},
        "tokens": final.get("tokens", 0),
        "sensors": {},  # C 无传感器
    }
