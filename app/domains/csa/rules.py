"""电商客服工单规则基线：基于用户原话关键字 + 订单状态的规则

规则优先级有讲究（与 SSRM 同哲学）：高优先级类（账号安全/退款）先判，
避免"退款纠纷"因为文本里带"物流"（如"退货的物流"）被先吸走。
"""

from typing import Any

from app.config import settings
from app.domains.csa.taxonomy import TICKET_CLASSES


def _make(cls: str, conf: float, evidence: str) -> dict[str, Any]:
    return {
        "root_cause_class": cls,
        "confidence": conf,
        "evidence": evidence,
        "suggested_action": (
            TICKET_CLASSES[cls].split("处置：", 1)[-1] if cls in TICKET_CLASSES else "人工排查"
        ),
        "needs_human_review": conf < settings.min_confidence_for_auto_action,
    }


def rule_baseline(incident: dict[str, Any]) -> dict[str, Any]:
    text = str(incident.get("text", ""))
    order_status = str(incident.get("order_status") or "")

    # 账号安全最先判：关键词歧义最小、优先级最高
    if any(kw in text for kw in ("盗号", "被盗", "异地登录", "改绑", "密码重置", "登录不上",
                                 "有人用我", "冻结", "敏感操作", "验证码")):
        return _make("account_security", 0.9, "命中账号安全关键字（盗号/改绑/冻结/验证码）")
    # 没有订单就不可能是售后：这是比关键字更强的判据（售前问"退货政策"不含糊）
    if order_status == "none":
        return _make("presale_consult", 0.8, "尚无订单（order_status=none），售后类无客体，按售前咨询")
    # 退款在物流之前：文本同时出现时（"退货的物流"）退款才是诉求
    if any(kw in text for kw in ("退款", "仅退款", "退货", "退钱", "退给", "没到账")):
        return _make("refund_dispute", 0.85, "命中退款关键字（退款/退货/未到账）")
    if any(kw in text for kw in ("发票", "开票", "抬头", "补寄发票")):
        return _make("invoice_request", 0.88, "命中发票关键字（开票/抬头）")
    if any(kw in text for kw in ("物流", "快递", "发货", "送达", "丢件", "停滞")) or order_status == "shipped":
        return _make("logistics_delay", 0.8, "命中物流关键字或订单已发货未妥投")
    # 咨询类词面：疑问句特征
    if any(kw in text for kw in ("吗", "什么时候", "有没有", "支持", "多少", "能不能", "可以")):
        return _make("presale_consult", 0.75, "疑问句特征，无售后关键字，按售前咨询处理")
    # 兜底：证据不足，如实返回 unknown
    return {
        "root_cause_class": "unknown",
        "confidence": 0.2,
        "evidence": "规则无法匹配任何已知模式",
        "suggested_action": "转人工分诊，并将该 case 归因后沉淀为新规则",
        "needs_human_review": True,
    }
