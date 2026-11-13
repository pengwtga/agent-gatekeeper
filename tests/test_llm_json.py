"""T6-2：宽松 JSON 提取（T0-1）的纯函数单测

覆盖四种形态：纯 JSON / 代码围栏包裹 / 前后有解释文字 / 完全没有 JSON。
metrics 是进程内单例，测试只断言解析结果，不断言计数（计数正确性由 metrics 自身保证）。
"""

import json

import pytest

from app.core.llm_client import _extract_json


def test_pure_json():
    assert _extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_code_fence():
    content = '好的，结果如下：\n```json\n{"root_cause_class": "ea_sync_timeout"}\n```\n以上。'
    assert _extract_json(content) == {"root_cause_class": "ea_sync_timeout"}


def test_fence_without_json_tag():
    content = '```\n{"k": 1}\n```'
    assert _extract_json(content) == {"k": 1}


def test_noise_around_json():
    content = '模型解释 blah blah {"confidence": 0.9, "evidence": "错误码 EA_TIMEOUT"} 完毕'
    assert _extract_json(content) == {"confidence": 0.9, "evidence": "错误码 EA_TIMEOUT"}


def test_no_json_raises():
    with pytest.raises(json.JSONDecodeError):
        _extract_json("抱歉，我无法回答这个问题")


def test_invalid_json_content_raises():
    # 有花括号但内容不合法：不许把半截 JSON 当结果
    with pytest.raises(json.JSONDecodeError):
        _extract_json('{"a": 1, "b": ')
