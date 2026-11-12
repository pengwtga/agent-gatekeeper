"""SSRM 分类体系：5 类根因定义 + few-shot（从 classifier.py 剥离的领域知识）

每类定义的结构约定：「现象描述。处置：标准动作」——
后半段会被规则基线和 LLM 的 suggested_action 复用，改文案时别破坏这个分隔符。
"""

# 系统拓扑：模型判定根因前必须知道有哪些系统、它们之间怎么交互
ROLE_INTRO = """SSRM（供应商管理系统）运维根因分析专家。SSRM 的供应商注册/变更流程横跨多个系统：
- xFlow：部门通用审批流组件，审批完成后回调 SSRM 创建任务
- EA：公司级供应商主数据平台，SSRM 的供应商增删改查都同步给它
- Dora：对外供应商门户，供应商在 Dora 注册并提交材料"""

# 分类体系：5 类根因（每类绑定标准处置动作，来自真实 oncall 处置经验）
ROOT_CAUSE_CLASSES: dict[str, str] = {
    "xflow_callback_lost": "审批回调丢失：xFlow 审批已通过，但回调未到达 SSRM（回调链路故障/服务重启窗口）。处置：按工单号主动拉取 xFlow 状态补齐，无需打扰用户。",
    "ea_sync_timeout": "同步 EA 超时/失败：向公司级供应商平台 EA 写数据超时（网络抖动/EA 发布中/接口限流）。处置：异步任务退避重试，超过阈值转人工；绝不阻塞注册主流程。",
    "duplicate_submit": "重复提交/幂等冲突：前端重复点击或 MQ 重复消费导致同一单多次到达。处置：幂等键去重直接返回成功，无需人工介入。",
    "data_inconsistency": "数据不一致：多下游同步部分成功（如 EA 成功但 Dora 缺联系人）。处置：由对账任务发现并补推，确认对账是否按时运行。",
    "frontend_param_error": "前端/参数错误：请求字段缺失、格式非法或前端调用方式错误。处置：拒绝请求并反馈具体字段问题，推动前端修复调用。",
}

# few-shot 示例（固定 2 个，覆盖易混淆对）
FEW_SHOT = [
    {
        "role": "user",
        "content": '{"stage":"register","source_system":"xflow","error_code":null,"error_message":"审批单 FT20261023001 状态为 APPROVED，但 SSRM 无对应注册任务","wait_minutes":42,"retry_count":0,"downstream":"ssrm","http_status":null}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"xflow_callback_lost","confidence":0.95,"evidence":"审批单已 APPROVED 且 SSRM 无任务，说明回调未到达而非审批未完成","suggested_action":"按工单号主动拉取 xFlow 状态补齐注册任务，并检查回调链路监控"}',
    },
    {
        "role": "user",
        "content": '{"stage":"register","source_system":"ssrm","error_code":"EA_TIMEOUT","error_message":"调用 EA createSupplier 接口 5s 超时","wait_minutes":2,"retry_count":3,"downstream":"ea","http_status":504}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"ea_sync_timeout","confidence":0.97,"evidence":"error_code=EA_TIMEOUT 且 http_status=504，重试 3 次均失败，指向 EA 侧超时","suggested_action":"异步任务退避重试，若 EA 处于发布窗口则等待恢复后由对账任务补推"}',
    },
]
