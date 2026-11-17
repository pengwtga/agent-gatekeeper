"""SSRM 领域包：供应商准入/变更卡单与同步失败的根因分类

知识来源：两年真实 oncall 归因经验。5 类根因覆盖注册/变更链路 90%+ 的卡单场景。
完整的双 Agent 结构：根因分类（给机器）+ 值班助手 RAG（给人）。
"""

from pathlib import Path

from app.domains import AssistantSpec, ConsistencyRule, DomainPack
from app.domains.ssrm.rules import rule_baseline
from app.domains.ssrm.taxonomy import FEW_SHOT, ROLE_INTRO, ROOT_CAUSE_CLASSES

_DATA = Path(__file__).resolve().parents[3] / "data" / "ssrm"  # app/domains/ssrm/ → 项目根/data/ssrm

pack = DomainPack(
    name="ssrm",
    role_intro=ROLE_INTRO,
    classes=ROOT_CAUSE_CLASSES,
    few_shot=FEW_SHOT,
    rule_baseline=rule_baseline,
    assistant=AssistantSpec(
        kb_file=_DATA / "kb_tickets.jsonl",
        threshold=0.4,  # 150KB 扫描定标：0.4 覆盖 91%/精度 98%
        # 助手的领域人设（与 pack.role_intro 不同：后者是分类器的系统拓扑知识）。
        # 不填会退化成「你是智能助手」，约束只剩知识库文本，进不了 system prompt。
        role_intro="SSRM（供应商管理系统）值班团队的运维助手，"
                   "值班同学遇到注册/变更卡单、EA 同步失败、审批回调异常时会来问你",
        # 一类一动作：同根因的条目如果动作不一致，说明有一条写错了（通常是新条目自己造句）
        consistency=(ConsistencyRule("root_cause_class", ("standard_action",)),),
    )
)
