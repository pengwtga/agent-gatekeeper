"""电商客服工单分类领域包（CSA）— 第二个 DomainPack，证明引擎可迁移

选这个领域的标准（面试重点）：
- 与 SSRM 同为「封闭枚举 + 高风险自动处置」形态，但知识面完全正交：
  没有系统拓扑、没有错误码，判断依据是用户原话（口语化文本）——
  专门用来暴露引擎在「结构化事件」之外的泛化能力；
- 规则基线的关键字规则与 SSRM 零重叠，防止"规则其实共享了领域词表"的假迁移；
- 类别数同为 5，保证与 SSRM 的评测结果可直接对比（同引擎、同规模、不同知识）。
"""

from app.domains import DomainPack
from app.domains.csa.rules import rule_baseline
from app.domains.csa.taxonomy import FEW_SHOT, ROLE_INTRO, TICKET_CLASSES

pack = DomainPack(
    name="csa",
    role_intro=ROLE_INTRO,
    classes=TICKET_CLASSES,
    few_shot=FEW_SHOT,
    rule_baseline=rule_baseline,
)
