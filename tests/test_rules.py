"""T6-2：规则基线单测（ssrm 五类 + unknown 兜底；credit 六分支含灰区）

规则是领域知识，也是评测 baseline——它自己错了，所有对比数字都会跟着错，
所以值得纯函数级单测锁住。
"""

from app.domains.credit.rules import rule_baseline as credit_rule
from app.domains.ssrm.rules import rule_baseline as ssrm_rule

# ── SSRM 运维域 ──

def test_ssrm_ea_timeout():
    r = ssrm_rule({"error_code": "EA_TIMEOUT", "error_message": "connect timed out"})
    assert r["root_cause_class"] == "ea_sync_timeout"


def test_ssrm_callback_lost():
    r = ssrm_rule({"error_message": "approved 但 SSRM 无对应任务", "source_system": "xflow"})
    assert r["root_cause_class"] == "xflow_callback_lost"


def test_ssrm_duplicate():
    r = ssrm_rule({"error_code": "DUPLICATE_KEY", "error_message": "duplicate entry"})
    assert r["root_cause_class"] == "duplicate_submit"


def test_ssrm_frontend_param():
    r = ssrm_rule({"error_message": "missing field: supplierName", "downstream": "frontend"})
    assert r["root_cause_class"] == "frontend_param_error"


def test_ssrm_data_inconsistency():
    r = ssrm_rule({"error_message": "数据缺失：明细与主单不一致", "downstream": "dora"})
    assert r["root_cause_class"] == "data_inconsistency"


def test_ssrm_unknown_when_no_match():
    r = ssrm_rule({"error_message": "今天天气不错"})
    assert r["root_cause_class"] == "unknown"
    assert r["needs_human_review"] is True  # 宁可转人工也不瞎猜


# ── 信贷域（rule_first 的权威通道）──

def _credit_inc(**kw):
    base = {"dti": 0.3, "credit_queries_3m": 2, "monthly_income": 20000,
            "amount": 5000, "term_months": 12, "device_risk": 10,
            "tenure_months": 24, "application_text": "装修自住，材料已订购"}
    base.update(kw)
    return base


def test_credit_fraud_by_device_risk():
    assert credit_rule(_credit_inc(device_risk=90))["root_cause_class"] == "fraud_suspect"


def test_credit_fraud_by_agent_words():
    assert credit_rule(_credit_inc(application_text="内部渠道包过"))["root_cause_class"] == "fraud_suspect"


def test_credit_multi_debt_by_queries():
    assert credit_rule(_credit_inc(credit_queries_3m=10))["root_cause_class"] == "multi_debt"


def test_credit_gray_zone_is_unknown():
    # dti 0.60~0.70 且查询 <8：共债/还款能力边界带，规则不硬判
    r = credit_rule(_credit_inc(dti=0.65, credit_queries_3m=3))
    assert r["root_cause_class"] == "unknown"
    assert r["needs_human_review"] is True


def test_credit_repayment_risk():
    assert credit_rule(_credit_inc(dti=0.55))["root_cause_class"] == "repayment_risk"


def test_credit_material_issue_empty_text():
    r = credit_rule(_credit_inc(application_text=""))
    assert r["root_cause_class"] == "material_issue"


def test_credit_pass():
    r = credit_rule(_credit_inc())
    assert r["root_cause_class"] == "pass"
