"""SSRM 规则基线：基于结构化特征的关键字/阈值规则（也是评测的 baseline）

从 classifier.py 原样剥离。规则是领域知识（关键字、错误码、阈值都是 SSRM 特有的），
所以它属于 domain pack 而不属于分类引擎。
"""

from typing import Any

from app.config import settings
from app.domains.ssrm.taxonomy import ROOT_CAUSE_CLASSES


def _make(cls: str, conf: float, evidence: str) -> dict[str, Any]:
    return {
        "root_cause_class": cls,
        "confidence": conf,
        "evidence": evidence,
        "suggested_action": (
            ROOT_CAUSE_CLASSES[cls].split("处置：", 1)[-1] if cls in ROOT_CAUSE_CLASSES else "人工排查"
        ),
        "needs_human_review": conf < settings.min_confidence_for_auto_action,
    }


def rule_baseline(incident: dict[str, Any]) -> dict[str, Any]:
    msg = str(incident.get("error_message", "")).lower()
    code = str(incident.get("error_code") or "").upper()
    downstream = str(incident.get("downstream") or "").lower()
    http_status = incident.get("http_status")

    if code in ("EA_TIMEOUT", "EA_5XX") or http_status in (504, 503) or (
        downstream == "ea" and "timeout" in msg or "timed out" in msg
    ):
        return _make("ea_sync_timeout", 0.8,
                     f"error_code={code}, http_status={http_status}, downstream=ea")
    if "approved" in msg and ("无对应" in msg or "no task" in msg or "回调" in msg) or (
        incident.get("source_system") == "xflow"
        and incident.get("wait_minutes", 0) >= 30
    ) or code == "CALLBACK_MISSING":
        return _make("xflow_callback_lost", 0.85,
                     "审批已通过但 SSRM 侧无任务，回调链路异常")
    if "duplicate" in msg or "幂等" in msg or code in ("DUPLICATE_KEY", "IDEMPOTENT_CONFLICT"):
        return _make("duplicate_submit", 0.9, "命中幂等冲突特征")
    if code in ("PARAM_MISSING", "PARAM_INVALID") or "missing field" in msg or (
        incident.get("downstream") == "frontend"
    ):
        return _make("frontend_param_error", 0.85, "参数/前端调用错误特征")
    if "inconsisten" in msg or "缺失" in msg or "missing" in msg and downstream in ("dora", "ea"):
        return _make("data_inconsistency", 0.7, "部分成功导致的数据缺口特征")
    if downstream == "ea":
        return _make("ea_sync_timeout", 0.5, "下游为 EA，默认按同步超时处理")
    # 兜底：证据不足，如实返回 unknown（宁可转人工也不瞎猜）
    return {
        "root_cause_class": "unknown",
        "confidence": 0.2,
        "evidence": "规则无法匹配任何已知模式",
        "suggested_action": "转人工排查，并将该 case 归因后沉淀为新规则",
        "needs_human_review": True,
    }
