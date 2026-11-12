"""电商客服工单分类体系：5 类 + few-shot

字段结构刻意与 SSRM 不同（text / channel / order_status / order_amount），
验证引擎不依赖特定输入 schema——只要求一个 JSON 对象。
"""

ROLE_INTRO = """电商客服工单分类专家。用户工单来自多个渠道（chat 在线客服 / phone 电话 / email 邮件），
内容是用户原话（口语化，可能带情绪词）。分类决定工单路由到哪个处理队列和 SLA：
- 退款纠纷和账号安全是高优先级队列
- 售前咨询走机器人话术，不占人工"""

TICKET_CLASSES: dict[str, str] = {
    "refund_dispute": "退款纠纷：要求退款、仅退款不退货、退货金额争议、承诺退款未到账。处置：按售后政策审核，48 小时内响应（高优先级队列）。",
    "logistics_delay": "物流延迟：未按承诺发货、物流长时间停滞、超时未送达、快递丢件。处置：查询物流单轨迹，超过 72 小时升级仓配催派。",
    "presale_consult": "售前咨询：商品规格、库存、优惠活动、发货时间等购买前询问，无售后诉求。处置：按售前话术答复，不进售后队列。",
    "invoice_request": "发票开具：申请开票、修改发票抬头、电子发票补发、发票金额与实付不符。处置：走开票流程，3 个工作日内开具。",
    "account_security": "账号安全：疑似盗号、异地登录提醒、手机号改绑、密码重置失败。处置：冻结敏感操作并发起身份验证，2 小时内响应（最高优先级）。",
}

FEW_SHOT = [
    {
        "role": "user",
        "content": '{"channel":"chat","text":"我上周就申请退款了说三天到账，现在都七天了钱呢？订单金额 259 不小呢","order_status":"refunding","order_amount":259.0,"user_tenure_days":680,"hour":20}',
    },
    {
        "role": "assistant",
        "content": '{"ticket_class":"refund_dispute","confidence":0.95,"evidence":"用户明确说申请退款七天未到账，order_status=refunding，属于承诺退款未到账","suggested_action":"按售后政策审核退款进度，48 小时内响应，优先处理"}',
    },
    {
        "role": "user",
        "content": '{"channel":"email","text":"这个手机支持双卡双待吗？现在有货吗，什么时候能发？","order_status":"none","order_amount":0.0,"user_tenure_days":45,"hour":11}',
    },
    {
        "role": "assistant",
        "content": '{"ticket_class":"presale_consult","confidence":0.96,"evidence":"问题全部是购买前的规格与发货时间询问，无任何售后诉求","suggested_action":"按售前话术答复规格与库存，不进售后队列"}',
    },
]
