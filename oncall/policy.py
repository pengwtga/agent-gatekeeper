"""执行策略：模型想做的 → 实际允许做的。

顺序不能变：
1. 置信度（与 v1 分类器同一个哲学：低置信宁可不做）
2. 副作用等级（read 放行 / write 挂起 / destructive 挂起且要求双人）
3. 白名单与 schema 在 `oncall/tools.call_tool` 里，这里不重复

I1 阶段：write / destructive 都返回 `approval`，由 loop 收尾成
`status=needs_human` + `pending_approval`（不落库）。I2 才接 SQLite 状态机与
`oncall approve`——先让「不执行」这条规则跑起来，再补「怎么批准」。
"""

from dataclasses import dataclass
from typing import Optional

from oncall.tools import SideEffect

DEFAULT_MIN_CONFIDENCE = 0.7

ALLOW, APPROVAL, DENY = "allow", "approval", "deny"


@dataclass(frozen=True)
class Decision:
    kind: str
    reason: str = ""
    risk: str = ""
    requires: int = 1


def decide(name: str, effect: SideEffect, confidence: Optional[float],
           *, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Decision:
    if confidence is None:
        # 模型没自报置信度 = 输出契约没遵守，不等于「它不确定」。
        # 把它当 0 会让弱模型永远执行不了任何工具（实测 glm-4-flash 会漏这个字段）；
        # 所以只读放行、有副作用照旧挂起——真正的风险由副作用等级兜，不靠一个缺失的字段。
        if effect is SideEffect.READ:
            return Decision(ALLOW, "未自报置信度，只读工具按放行处理")
        return Decision(APPROVAL, "未自报置信度且动作有副作用，需人工确认",
                        risk=effect.value, requires=2 if effect is SideEffect.DESTRUCTIVE else 1)

    if confidence < min_confidence:
        return Decision(DENY, f"置信度 {confidence:.2f} 低于 {min_confidence:.2f}，不执行")

    if effect is SideEffect.READ:
        return Decision(ALLOW)

    if effect is SideEffect.DESTRUCTIVE:
        # 不可逆动作：I1 先挂起并标记需要两人确认，I2 落库后强制两次 approve
        return Decision(APPROVAL, "不可逆动作，需人工确认（双人）",
                        risk="destructive", requires=2)

    return Decision(APPROVAL, "写操作，需人工确认", risk="write", requires=1)
