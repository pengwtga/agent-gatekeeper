"""证据门禁的下限口径：按「不同字段」算，不按条目算

2026-11-16 挖出来的洞：`_value_matches` 对长文本允许引用为原文子串（截断是合理的），
于是一条 `error_message` 切成两段就是 2 条 ok 引用、`refs_score` 还是 1.0 —— 
`min_evidence_refs=2` 的「至少两条证据」被一句半的话凑满了。

修法：下限统计 `{d["field"] for d in detail if d["ok"]}` 的基数。
两条引用指向同一个字段，只算一个独立事实。
"""

import asyncio

from app.agents.classifier import RootCauseClassifier
from app.config import settings
from app.domains.ssrm import pack as ssrm_pack

INCIDENT = {
    "incident_id": "GATE-1",
    "stage": "register",
    "source_system": "ssrm",
    "error_code": None,
    "http_status": 504,
    "retry_count": 3,
    "downstream": "ea",
    "wait_minutes": 5,
    "error_message": "调用 EA createSupplier 接口 5s 超时",
}

# 一条较长的自然语言错误信息：子串引用需要 ≥8 个字符才算数（evidence._MIN_SUBSTRING），
# 所以「同一句切两段」这个洞只在长文本上成立 —— 这恰恰是真实告警的常见形态
LONG_INCIDENT = dict(INCIDENT, error_message="供应商提交了准入申请，过了好久一直没反应")


class _StubLLM:
    """只回一份固定 JSON 的桩：绕过 provider 与网络，专测门禁判定"""

    is_configured = True

    def __init__(self, payload: dict):
        self._payload = payload

    async def chat_json(self, system_prompt=None, user_payload=None, budget=None):
        return dict(self._payload)


def _classify(evidence_refs, confidence=0.95, evidence="http_status=504 且重试 3 次均失败",
              incident=None):
    payload = {
        "root_cause_class": "ea_sync_timeout",
        "confidence": confidence,
        "evidence": evidence,
        "evidence_refs": evidence_refs,
        "suggested_action": "异步任务退避重试",
    }
    clf = RootCauseClassifier(_StubLLM(payload), ssrm_pack)
    return asyncio.run(clf.classify(dict(incident or INCIDENT)))


def test_two_refs_on_same_field_are_blocked():
    """同一句错误信息切两段：条数够、分数满分，但只有一个独立字段 → 必须拦"""
    result = _classify(
        [{"field": "error_message", "value": "供应商提交了准入申请"},
         {"field": "error_message", "value": "过了好久一直没反应"}],
        incident=LONG_INCIDENT,
    )
    assert result["evidence_score"] == 1.0          # 分数确实是满分
    assert result["evidence_refs_count"] == 2      # 条数确实够
    assert result["evidence_refs_fields"] == 1     # 但只有一个字段
    assert result["confidence"] == settings.low_evidence_confidence_cap
    assert "个字段" in result["evidence_warning"]
    assert result["needs_human_review"] is True


def test_two_distinct_fields_pass():
    result = _classify([
        {"field": "http_status", "value": 504},
        {"field": "retry_count", "value": 3},
    ])
    assert result["evidence_refs_fields"] == 2
    assert "evidence_warning" not in result
    assert result["confidence"] == 0.95
    assert result["needs_human_review"] is False


def test_distinct_fields_count_only_valid_refs():
    """字段多但都对不上，不能算数：只有 ok 的引用才计入下限"""
    result = _classify([
        {"field": "http_status", "value": 504},
        {"field": "error_code", "value": "EA_TIMEOUT"},      # 输入里是 None
        {"field": "db_pool_usage", "value": "95%"},          # 字段不存在
    ])
    assert result["evidence_refs_fields"] == 1
    assert result["confidence"] == settings.low_evidence_confidence_cap
    assert "个字段" in result["evidence_warning"]


def test_three_distinct_fields_keep_high_confidence():
    result = _classify([
        {"field": "http_status", "value": 504},
        {"field": "retry_count", "value": 3},
        {"field": "downstream", "value": "ea"},
    ])
    assert result["evidence_refs_fields"] == 3
    assert result["evidence_score"] == 1.0
    assert "evidence_warning" not in result
