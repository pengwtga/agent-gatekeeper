"""可注入的时钟。

为什么要这一层：预算测试不能靠 `sleep`（一次预算用例要等 90s），也不能
monkeypatch `time.monotonic`（会和别处的超时逻辑串味）。把「取时间」变成依赖，
测试里换 FakeClock 就能在毫秒内验证「预算耗尽后不再发起 LLM 调用」。
"""

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """单调递增的秒。只用于算时间差，不受系统对时影响。"""
        ...


class SystemClock:
    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()
