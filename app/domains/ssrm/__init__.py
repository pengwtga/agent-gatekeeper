"""SSRM 领域包：供应商准入/变更卡单与同步失败的根因分类

知识来源：两年真实 oncall 归因经验。5 类根因覆盖注册/变更链路 90%+ 的卡单场景。
"""

from app.domains import DomainPack
from app.domains.ssrm.rules import rule_baseline
from app.domains.ssrm.taxonomy import FEW_SHOT, ROLE_INTRO, ROOT_CAUSE_CLASSES

pack = DomainPack(
    name="ssrm",
    role_intro=ROLE_INTRO,
    classes=ROOT_CAUSE_CLASSES,
    few_shot=FEW_SHOT,
    rule_baseline=rule_baseline,
)
