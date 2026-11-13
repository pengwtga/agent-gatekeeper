"""T6-2：预算（T1-1）单测

语义注意：Budget 是 deadline 制——remaining 只随墙钟衰减，slice 是「查询还能不能用」
的被动检查，不消耗预算。真正的消耗者是各 LLM 尝试自身花掉的时间。
"""

import time

from app.core.budget import Budget


def test_slice_within_remaining():
    b = Budget(10_000)
    assert b.slice(400) == 400
    # deadline 制：remaining 只随墙钟衰减，slice 不消耗
    assert 0 < b.remaining_ms() <= 10_000


def test_slice_never_exceeds_remaining():
    b = Budget(1)
    time.sleep(0.002)  # 越过 deadline
    assert b.slice(400) == 0


def test_slice_partial_when_low():
    b = Budget(50)
    time.sleep(0.03)  # 剩余 ~20ms
    got = b.slice(40)
    assert 0 < got < 40  # 只给剩余部分


def test_exhausted_zero_budget():
    assert Budget(0).exhausted() is True


def test_not_exhausted_initially():
    assert Budget(5000).exhausted() is False


def test_remaining_monotonic_decrease():
    b = Budget(10_000)
    r1 = b.remaining_ms()
    time.sleep(0.01)
    assert b.remaining_ms() < r1  # monotonic 时钟，不受系统对时影响
