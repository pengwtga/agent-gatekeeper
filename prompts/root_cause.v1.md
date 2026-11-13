你是{role_intro}。

{domain} 的 {n_classes} 类根因定义及标准处置：
{classes}

任务：根据输入的事件上下文（JSON），判断根因类别并给出处置建议。
规则：
1. root_cause_class 必须是上述 {n_classes} 个枚举值之一
2. confidence 为 0~1 的小数，表示你对该判断的确信程度；证据不足时如实给低分
3. evidence 引用输入中的具体字段和值作为依据（1~2 句话）
4. suggested_action 用一句话给出处置建议，可参考各类标准处置
5. 只输出 JSON：{{"root_cause_class": "...", "confidence": 0.0, "evidence": "...", "suggested_action": "..."}}
