"""领域包（Domain Pack）：分类引擎的知识插件

设计动机（面试重点）：
分类引擎（app.agents.classifier）是领域无关的——它只负责「LLM 主通道 + 规则降级 +
证据门禁 + 置信度门禁」这套**机制**；而「分几类、每类什么定义、规则怎么写」是**领域知识**。
把两者切开之后：

- 换一个高风险领域（客服工单分类、风控可疑交易、诊断分诊）= 写一个新 pack，引擎代码零改动；
- 领域知识的演化（加类、改 few-shot）被限制在 pack 目录里，回归评测也按 pack 跑；
- 「引擎的机制正确」和「领域的知识正确」可以分开测——前者用通用验收，后者用领域评测集。

一个 pack 必须提供四样东西（见 DomainPack 字段），其中 rule_baseline 允许为 None
（此时 LLM 全链失败会直接返回 unknown，而不是降级规则）。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class DomainPack:
    """一个可插拔的分类领域。字段即 pack 契约，实现时不允许偏离。"""

    name: str
    # 领域角色与系统拓扑描述：system prompt 的第一段（模型需要背景才能判定）
    role_intro: str
    # 类别 -> 定义+标准处置。约定定义里用「处置：」分隔标准动作，
    # 规则基线的 suggested_action 会取「处置：」之后的文本
    classes: dict[str, str]
    # OpenAI 消息格式的 few-shot（user/assistant 交替），建议覆盖最易混淆的类别对
    few_shot: list[dict] = field(default_factory=list)
    # 规则基线：incident -> 结果 dict（与 LLM 通道输出同构）
    rule_baseline: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
