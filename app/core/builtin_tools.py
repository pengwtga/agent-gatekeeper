"""内置只读工具：注册进 ToolRegistry，供通用工具调用层选择执行

选这三个工具的标准（面试可讲）：
- 全部只读——副作用工具接入权限系统之前不进注册表；
- 覆盖三种典型形态：静态知识（topology）、聚合统计（kb_stats）、条件查询（query_incidents），
  足够暴露「LLM 选错工具 / 传错参数」的各种失败模式，方便把工具调用也纳入评测；
- 实现直接复用现有组件（rag.docs / dataset），不引入新数据源。
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.tools import Tool, ToolRegistry
from app.domains.ssrm.taxonomy import ROLE_INTRO


def _load_incidents() -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "data" / "ssrm" / "incidents.jsonl"
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def build_default_registry(rag) -> ToolRegistry:
    """构建默认注册表。rag 传入 RagAssistant 实例（kb_stats 依赖其已加载的知识库）。"""

    async def kb_stats() -> dict[str, Any]:
        dist = Counter(t["root_cause_class"] for t in rag.docs)
        return {"total": len(rag.docs), "by_class": dict(dist), "mode": rag.mode}

    async def query_incidents(root_cause_class: str, limit: int = 3) -> dict[str, Any]:
        """从评测集里按 label 抽样事件——给值班/上层系统看「这一类长什么样」"""
        rows = [
            row for row in _load_incidents()
            if row["label"] == root_cause_class
        ]
        picked = [
            {"incident_id": r["incident"]["incident_id"],
             "error_message": r["incident"]["error_message"],
             "expected_action": r["expected_action"]}
            for r in rows[: max(1, min(limit, 10))]
        ]
        return {"label": root_cause_class, "matched": len(rows), "samples": picked}

    async def topology() -> dict[str, str]:
        return {"topology": ROLE_INTRO}

    registry = ToolRegistry()
    registry.register(Tool(
        name="kb_stats",
        description="查询值班知识库的统计信息：工单总数、按根因类别的分布、当前检索模式",
        params={},
        handler=kb_stats,
    ))
    registry.register(Tool(
        name="query_incidents",
        description="按根因类别查询历史事件样例（来自评测集），用于了解某类问题的典型表现",
        params={
            "root_cause_class": {"type": "string", "description": "根因类别枚举值", "required": True},
            "limit": {"type": "integer", "description": "返回条数，默认 3，上限 10", "required": False},
        },
        handler=query_incidents,
    ))
    registry.register(Tool(
        name="system_topology",
        description="返回 SSRM 系统拓扑说明（xFlow / EA / Dora 的关系），回答「系统结构」类问题",
        params={},
        handler=topology,
    ))
    return registry
