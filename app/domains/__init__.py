"""领域包（Domain Pack）：分类引擎 + 助手引擎的知识插件

设计动机（面试重点）：
分类引擎（app.agents.classifier）与助手引擎（app.agents.rag_assistant）都是领域无关的——
它们只负责**机制**（分类：LLM 主通道 + 规则降级 + 证据/置信度双门禁；
助手：检索 + 采信阈值 + grounding 总结），而「分几类、知识库放什么、阈值多少」是**领域知识**。
把两者切开之后：

- 换一个高风险领域（客服工单分类、风控可疑交易、诊断分诊）= 写一个新 pack，引擎代码零改动；
- 领域知识的演化（加类、改 few-shot、扩知识库）被限制在 pack 目录里，回归评测也按 pack 跑；
- 「引擎的机制正确」和「领域的知识正确」可以分开测——前者用通用验收，后者用领域评测集。

pack 契约见 DomainPack / AssistantSpec 字段：rule_baseline 允许为 None（此时 LLM 全链
失败直接返回 unknown）；assistant 允许为 None（该领域暂无助手场景，只有分类）。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class AssistantSpec:
    """助手引擎（RAG）的领域知识规格。

    知识库条目标准化为五字段 schema（三个领域统一，字段名可映射）：
        {id 字段, category 字段, title, symptom, resolution}
    - title + symptom 参与关键词检索打分；
    - resolution 是喂给 LLM 总结的处置/话术字段。
    """

    # 知识库 JSONL 路径
    kb_file: Path
    # 采信阈值：检索分数低于此标记「低置信，建议人工」，不硬答。应在评测集上扫描定标
    threshold: float = 0.55
    # 条目 ID / 类别字段名（不同领域叫法不同：ticket_id / case_id；category 语义各异）
    id_field: str = "ticket_id"
    class_field: str = "root_cause_class"
    # system prompt 的领域背景段（催员辅助的合规边界、坐席助手的业务范围等）
    role_intro: str = ""


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
    # 规则先行（rule-primary）：规则给出明确结论直接采用，落 unknown（灰区）才升级 LLM 仲裁。
    # 适用于「策略即硬规则」的领域（如信贷风控：阈值是策略权威，小模型做不可靠的算术/规则
    # 应用反而引入误差）；LLM 的价值收窄到灰区仲裁与文本信号（话术/用途合理性）。
    rule_first: bool = False
    # 助手场景（RAG）：None 表示该领域暂无助手（只有分类）。
    # 有助手 = 该领域是完整的「分类 + 助手」双 Agent 结构（如 SSRM 运维、信贷催员辅助）
    assistant: Optional[AssistantSpec] = None
