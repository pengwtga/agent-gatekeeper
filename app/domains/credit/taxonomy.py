"""消费信贷进件风险分类体系：5 类 + few-shot

领域背景（面试重点）：这是信贷审批流程的**前置分流**环节——在人工信审介入之前，
把进件先分成五个流向。分类错误的代价不对称：误放欺诈件是真金白银的损失，
误拒好客户是体验与规模损伤——所以引擎的置信度门禁 + 证据可回溯在这个领域
不是"工程亮点"而是**监管合规的硬要求**（拒贷必须可解释、可申诉）。

输入是风控进件的结构化字段 + 一段用途说明自然语言——刻意让规则通道吃字段、
LLM 通道吃文本+字段，两条通道的判据来源不同，才测得出真实的互补性。
"""

ROLE_INTRO = """消费信贷进件风险分类专家。进件数据来自授信申请系统（结构化风控字段 +
申请人填写的用途说明），分类决定这条进件走哪个审批流向：
- fraud_suspect 进欺诈调查队列（冻结授信，人工核查）
- multi_debt / repayment_risk 进信审决策队列（降额或拒绝，需给出可解释理由）
- material_issue 进补件队列（退回补齐后再审）
- pass 进正常授信流程
拒绝结论必须附带可核验的证据链，监管要求可解释、可申诉。"""

CREDIT_CLASSES: dict[str, str] = {
    "fraud_suspect": "欺诈嫌疑。硬信号（任一即判本类）：设备风险分 ≥85；近 3 月征信查询 ≥12 次且现单位 <6 个月（短期高频申请）；用途说明出现中介代办话术（「包装」「内部渠道」「包过」「无视征信」）。处置：冻结授信转欺诈调查，2 小时内人工核查。",
    "multi_debt": "多头借贷/共债风险。判据（按优先级）：近 3 月征信查询 ≥8 次——这是共债的最强客观信号，只要查询达到 8 次，无论 dti 与收入如何优先判本类；或 dti ≥0.70。注意边界：查询 ≥8 判共债，查询 <8 而单纯 dti 偏高判 repayment_risk。处置：进信审决策，降额或拒绝，结论须附征信查询与负债证据。",
    "repayment_risk": "还款能力不足。判据：dti ≥0.50 且征信查询 <8 次；或月供（金额/期限月数）超过月收入的 50%。与 multi_debt 的边界：负债高同时查询也密集（≥8 次）是共债不是单纯还款能力问题。处置：进信审决策，降额授信或拒绝，附收入负债测算依据。",
    "material_issue": "资料不全/待核验。判据（即使其余风控指标全部正常也判本类）：用途说明空白或少于 6 个字；或月收入字段缺失——没有收入数据就无法完成还款能力测算，不能凭其他指标放行。处置：退回补件队列，列明需补齐的材料清单。",
    "pass": "正常进件。判据需同时满足：征信查询 ≤5 次、dti ≤0.45、月供 ≤月收入 50%、设备风险分 ≤60，且用途说明具体可信、收入字段完整。历史逾期 ≤1 次不构成拒绝信号。处置：进入正常授信流程，按标准额度模型定价。",
}

FEW_SHOT = [
    {
        "role": "user",
        "content": '{"amount":80000.0,"term_months":12,"monthly_income":8500.0,"dti":0.38,"credit_queries_3m":14,"overdue_24m":0,"age":27,"tenure_months":2,"device_risk":88,"application_text":"急需周转，朋友说这个平台好过，包过","hour":23}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"fraud_suspect","confidence":0.93,"evidence":"device_risk=88 异常偏高；credit_queries_3m=14 短期高频申请；tenure_months=2 入职极短；用途说明出现中介话术「包过」，与正常借款用途矛盾","suggested_action":"冻结授信转欺诈调查，2 小时内人工核查"}',
    },
    {
        "role": "user",
        "content": '{"amount":30000.0,"term_months":24,"monthly_income":12000.0,"dti":0.44,"credit_queries_3m":2,"overdue_24m":0,"age":33,"tenure_months":48,"device_risk":12,"application_text":"装修房子，已开工，需要支付尾款","hour":14}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"pass","confidence":0.94,"evidence":"征信查询仅 2 次频率正常；dti=0.44 在审慎线内；收入 12000 与申请 30000/24 期匹配；现单位 48 个月稳定；设备风险分 12 正常；用途说明具体可信（装修尾款）","suggested_action":"进入正常授信流程，按标准额度模型定价"}',
    },
    {
        "role": "user",
        "content": '{"amount":40000.0,"term_months":24,"monthly_income":14000.0,"dti":0.60,"credit_queries_3m":9,"overdue_24m":0,"age":34,"tenure_months":54,"device_risk":16,"application_text":"整合之前的分期账单","hour":15}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"multi_debt","confidence":0.9,"evidence":"近 3 月征信查询 9 次 ≥8，是共债最强信号，dti 偏高只是伴生症状——查询密集时优先判共债而非还款能力","suggested_action":"进信审决策，降额或拒绝，附征信查询与负债证据"}',
    },
    {
        "role": "user",
        "content": '{"amount":25000.0,"term_months":12,"monthly_income":10000.0,"dti":0.30,"credit_queries_3m":2,"overdue_24m":0,"age":30,"tenure_months":30,"device_risk":10,"application_text":"","hour":21}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"material_issue","confidence":0.9,"evidence":"用途说明完全空白——即使其余指标全部正常，没有用途与收入佐证不能放行，按资料不全处理","suggested_action":"退回补件队列，要求补充借款用途说明"}',
    },
]
