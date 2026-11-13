"""信贷进件规则基线：硬阈值规则，优先级模拟真实风控策略

规则顺序的风控语义（面试重点）：欺诈规则最先判（拦截优先于定价），
然后是共债（多头查询是最强的客观信号），再是还款能力，最后资料补全。
所有阈值都是"策略可配置"的形态——真实风控系统里这些数字由策略团队运营，不写死在代码里。
"""

from typing import Any

from app.config import settings
from app.domains.credit.taxonomy import CREDIT_CLASSES

# 中介代办/包装话术特征词（LLM 通道与规则通道共享的最小词表，仅用于规则兜底）
_AGENT_WORDS = ("包过", "内部渠道", "包装", "代办", "无视征信", "黑户可做",
                "特殊通道", "不看征信", "不用看征信")


def _make(cls: str, conf: float, evidence: str) -> dict[str, Any]:
    return {
        "root_cause_class": cls,
        "confidence": conf,
        "evidence": evidence,
        "suggested_action": (
            CREDIT_CLASSES[cls].split("处置：", 1)[-1] if cls in CREDIT_CLASSES else "人工排查"
        ),
        "needs_human_review": conf < settings.min_confidence_for_auto_action,
    }


def rule_baseline(incident: dict[str, Any]) -> dict[str, Any]:
    text = str(incident.get("application_text") or "")
    dti = float(incident.get("dti") or 0)
    queries = int(incident.get("credit_queries_3m") or 0)
    income = float(incident.get("monthly_income") or 0)
    amount = float(incident.get("amount") or 0)
    term = int(incident.get("term_months") or 0)
    device_risk = int(incident.get("device_risk") or 0)
    tenure = int(incident.get("tenure_months") or 0)

    # 1) 欺诈最先判：设备风险 + 高频申请 + 中介话术，任一硬信号即拦截
    if device_risk >= 85:
        return _make("fraud_suspect", 0.9, f"设备风险分 {device_risk} ≥ 85，异常设备特征")
    if queries >= 12 and tenure <= 3:
        return _make("fraud_suspect", 0.88,
                     f"近 3 月征信查询 {queries} 次（≥12）且现单位仅 {tenure} 个月，短期高频申请特征")
    if any(w in text for w in _AGENT_WORDS):
        return _make("fraud_suspect", 0.92, "用途说明命中中介代办话术特征词")
    # 2) 共债：多头查询是最强的客观共债信号
    if queries >= 8:
        return _make("multi_debt", 0.85, f"近 3 月征信查询 {queries} 次（≥8），多头申请迹象")
    if dti >= 0.7:
        return _make("multi_debt", 0.82, f"负债收入比 {dti:.2f} ≥ 0.70，共债风险超审慎线")
    # 2.5) 灰区：dti 0.60~0.70 且查询不密集 —— 共债与还款能力的边界带。
    #      规则不硬判（两种归因的处置完全不同，误判代价不对称），交 LLM 仲裁，
    #      仲裁结论仍要过置信度门禁；LLM 不可用则如实转人工
    if 0.60 <= dti < 0.70 and queries < 8:
        return {
            "root_cause_class": "unknown",
            "confidence": 0.2,
            "evidence": f"dti {dti:.2f} 处于共债/还款能力边界带且征信查询不密集（{queries} 次 <8），规则不硬判",
            "suggested_action": "转 LLM 仲裁或人工信审，归因结果沉淀为新的策略规则",
            "needs_human_review": True,
        }
    # 3) 还款能力：收入与月供不匹配
    monthly_payment = amount / term if term else 0
    if dti >= 0.5 or (income > 0 and monthly_payment > income * 0.5):
        return _make("repayment_risk", 0.8,
                     f"负债收入比 {dti:.2f} 偏高" if dti >= 0.5
                     else f"月供 {monthly_payment:.0f} 超月收入 {income:.0f} 的 50%")
    # 4) 资料不全：用途缺失/过短，或关键字段缺失
    if not text or len(text) < 6:
        return _make("material_issue", 0.85, "用途说明空白或过短，无法评估借款用途")
    if income <= 0:
        return _make("material_issue", 0.8, "月收入字段缺失，无法完成还款能力测算")
    # 5) 兜底放行前先确认各阈值确实在安全区
    if queries <= 5 and dti <= 0.45 and device_risk <= 60:
        return _make("pass", 0.85,
                     f"征信查询 {queries} 次、dti {dti:.2f}、设备风险 {device_risk} 均在阈值内，用途说明具体")
    # 灰区：规则给不出明确结论，如实转人工
    return {
        "root_cause_class": "unknown",
        "confidence": 0.2,
        "evidence": "各指标处于灰区（未触发硬规则也未达放行阈值），需人工信审",
        "suggested_action": "转人工信审，并将归因结果沉淀为新的策略规则",
        "needs_human_review": True,
    }
