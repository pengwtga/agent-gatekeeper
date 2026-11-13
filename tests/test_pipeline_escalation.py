"""T6-2：分类引擎输出校验单测（_validate_class）+ pipeline 状态机四路径

状态机路径覆盖：auto_dispose / escalate / human_review+落库+续跑 / 步数熔断。
用未配置 LLM 的假客户端强制纯规则路径，保证测试确定性、不烧 token。
"""

import asyncio
import tempfile
from pathlib import Path

from app.agents.classifier import RootCauseClassifier
from app.agents.pipeline import HandlePipeline
from app.core.escalation import EscalationStore
from app.domains.credit import pack as credit_pack


class NoLLM:
    """is_configured=False → 引擎走规则降级通道，测试确定性且零外部调用"""
    is_configured = False


def _classifier() -> RootCauseClassifier:
    return RootCauseClassifier(NoLLM(), credit_pack)


# ── 输出校验 ──

def test_validate_class_accepts_legal():
    clf = _classifier()
    legal = next(iter(credit_pack.classes))
    assert clf._validate_class(legal) == legal


def test_validate_class_illegal_goes_unknown():
    assert _classifier()._validate_class("ea_sync_timeout") == "unknown"  # 别域的类不许过


def test_validate_class_none_goes_unknown():
    assert _classifier()._validate_class(None) == "unknown"


# ── 状态机路径 ──

def _pass_inc():
    return {"application_id": "T-1", "dti": 0.3, "credit_queries_3m": 2,
            "monthly_income": 20000, "amount": 5000, "term_months": 12,
            "device_risk": 10, "tenure_months": 24, "application_text": "装修自住，材料已订购"}


def _gray_inc():
    return {"application_id": "T-2", "dti": 0.65, "credit_queries_3m": 3,
            "monthly_income": 15000, "amount": 8000, "term_months": 12,
            "device_risk": 10, "tenure_months": 20, "application_text": "日常消费周转"}


def test_pipeline_auto_dispose():
    r = asyncio.run(HandlePipeline(_classifier()).run(_pass_inc()))
    assert r["disposition"] == "auto_dispose"
    stages = [s["stage"] for s in r["pipeline"]]
    assert stages == ["intake", "classify", "enrich", "decide"]


def test_pipeline_gray_zone_escalates():
    r = asyncio.run(HandlePipeline(_classifier()).run(_gray_inc()))
    assert r["root_cause_class"] == "unknown" and r["disposition"] == "escalate"


def test_pipeline_low_confidence_goes_human_review():
    # dti 0.55 → repayment_risk conf 0.8 < 0.85 → human_review，enrich 跳过（无注册表）
    inc = {"application_id": "T-3", "dti": 0.55, "credit_queries_3m": 2,
           "monthly_income": 10000, "amount": 12000, "term_months": 12,
           "device_risk": 10, "tenure_months": 30, "application_text": "装修购买建材"}
    r = asyncio.run(HandlePipeline(_classifier()).run(inc))
    assert r["disposition"] == "human_review"


def test_pipeline_step_limit_aborts():
    r = asyncio.run(HandlePipeline(_classifier(), max_steps=1).run(_pass_inc()))
    assert r["pipeline"][-1]["stage"] == "abort"


# ── 升级队列闭环 ──

def test_escalation_roundtrip_and_resolve_once():
    pipe = HandlePipeline(_classifier())
    r = asyncio.run(pipe.run({"application_id": "T-3", "dti": 0.55, "credit_queries_3m": 2,
                              "monthly_income": 10000, "amount": 12000, "term_months": 12,
                              "device_risk": 10, "tenure_months": 30,
                              "application_text": "装修购买建材"}))
    with tempfile.TemporaryDirectory() as td:
        store = EscalationStore(Path(td) / "esc.db")
        eid = store.add("credit", r)

        got = store.get(eid)
        assert got["status"] == "pending"
        assert "classify_result" in got["state"]

        resumed = pipe.resume(got["state"], "repayment_risk", note="人工核实批准")
        assert resumed["root_cause_class"] == "repayment_risk"
        assert resumed["confidence"] == 1.0
        assert resumed["human_override"]["approved_class"] == "repayment_risk"
        assert resumed["pipeline"][0]["stage"] == "human_override"

        assert store.resolve(eid, "repayment_risk（批准）") is True
        assert store.resolve(eid, "再次决议") is False  # 审计线索不可覆盖
        assert store.pending_count() == 0
        store.close()
