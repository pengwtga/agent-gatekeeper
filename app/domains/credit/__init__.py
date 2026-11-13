"""消费信贷进件风险分类领域包（CREDIT）— 第三个 DomainPack，面向信贷风控场景

与 csa 的差异化定位（面试重点）：
- csa 证明引擎能吃「口语化文本」；credit 证明引擎能吃「结构化风控字段 + 少量文本」，
  且规则基线本身就是真实风控策略的形态（硬阈值 + 优先级），不是演示性质的关键字匹配；
- 这个领域的门禁语义天然成立：拒贷可解释、可申诉是监管硬要求——
  证据可回溯校验在这里是合规能力，不是工程锦上添花；
- 输入 schema 与 ssrm（工单事件）、csa（客服对话）三者互不重叠，
  三个 pack 一起完成「引擎领域无关」的完整证明。
"""

from pathlib import Path

from app.domains import AssistantSpec, DomainPack
from app.domains.credit.rules import rule_baseline
from app.domains.credit.taxonomy import CREDIT_CLASSES, FEW_SHOT, ROLE_INTRO

_DATA = Path(__file__).resolve().parents[3] / "data" / "credit"  # app/domains/credit/ → 项目根/data/credit

pack = DomainPack(
    name="credit",
    role_intro=ROLE_INTRO,
    classes=CREDIT_CLASSES,
    few_shot=FEW_SHOT,
    rule_baseline=rule_baseline,
    # 信贷是「策略即硬规则」领域：实测 glm-4-flash 全量跑 LLM 通道仅 60~67%
    # （算术与阈值规则应用不可靠），规则基线 96.7%——故规则先行、LLM 只仲裁灰区
    rule_first=True,
    # 催员辅助：催收专员与债务人沟通时的合规话术/施压分级/升级路径检索。
    # 合规红线写进知识库条目本身（engine 不知道"合规"是什么，知识知道）
    assistant=AssistantSpec(
        kb_file=_DATA / "credit_collection_kb.jsonl",
        threshold=0.2,  # 120KB 扫描定标：0.2 覆盖 87%/精度 85%
        id_field="case_id",
        class_field="category",
        role_intro="消费信贷催收团队的催员辅助助手。催收专员与债务人沟通前/中会来问你",
    ),
)
