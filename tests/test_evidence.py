"""T6-2：证据校验单测——两个判据（结构化引用主判据 + 字面匹配兜底）

标定口径来自 T0-03 / T3-02：诚实引用高分、纯编造 0 分、无 refs 返回 None 触发回退。
"""

from app.agents.evidence import check_refs, evidence_score, refs_score

INCIDENT = {
    "incident_id": "FT20261025088",
    "error_code": "EA_TIMEOUT",
    "http_status": 504,
    "downstream": "ea",
    "error_message": "调用 EA 接口超时，连接建立后 30s 无响应",
    "items": [{"sku": "SKU-001", "qty": 3}],
}


# ── 结构化引用（主判据）──

def test_refs_all_valid():
    refs = [{"field": "error_code", "value": "EA_TIMEOUT"},
            {"field": "http_status", "value": 504}]
    detail = check_refs(refs, INCIDENT)
    assert refs_score(detail) == 1.0
    assert all(d["ok"] for d in detail)


def test_refs_numeric_string_equivalence():
    # JSON 里 504 与 "504" 是同一事实
    detail = check_refs([{"field": "http_status", "value": "504"}], INCIDENT)
    assert detail[0]["ok"] is True


def test_refs_field_missing():
    detail = check_refs([{"field": "db_pool_usage", "value": "95%"}], INCIDENT)
    assert detail[0]["ok"] is False and detail[0]["reason"] == "字段不存在"


def test_refs_value_fabricated():
    detail = check_refs([{"field": "error_code", "value": "DB_POOL_EXHAUSTED"}], INCIDENT)
    assert detail[0]["ok"] is False and detail[0]["reason"] == "值不相等"


def test_refs_nested_path():
    detail = check_refs([{"field": "items.0.sku", "value": "SKU-001"}], INCIDENT)
    assert detail[0]["ok"] is True


def test_refs_empty_returns_none():
    # 没给 refs ≠ 编造：返回 None 让调用方回退字面匹配
    assert refs_score(check_refs(None, INCIDENT)) is None
    assert refs_score([]) is None


def test_refs_string_encoded_array():
    # 小模型把数组二次编码成字符串，能救就救
    refs = '[{"field": "error_code", "value": "EA_TIMEOUT"}]'
    assert refs_score(check_refs(refs, INCIDENT)) == 1.0


def test_refs_substring_quote_allowed():
    # 长文本允许截断引用（引用原文一段），但不许改写
    detail = check_refs(
        [{"field": "error_message", "value": "连接建立后 30s 无响应"}], INCIDENT)
    assert detail[0]["ok"] is True


# ── 字面匹配（兜底判据）──

def test_literal_honest_reference():
    # 标定口径（T0-03）：诚实引用 0.476~0.684，阈值 0.35 之上
    score = evidence_score("错误码 EA_TIMEOUT，HTTP 504，下游为 ea", INCIDENT)
    assert 0.4 <= score <= 0.75


def test_literal_fabricated():
    # 纯编造：锚点全部不在输入里
    score = evidence_score("数据库连接池耗尽导致服务不可用，需扩容连接池", INCIDENT)
    assert score <= 0.2


def test_literal_empty_evidence():
    assert evidence_score("", INCIDENT) == 0.0
