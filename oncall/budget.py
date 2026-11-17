"""run 级时间预算。

与 `app/core/budget.py` 的差别只有一个：**时钟可注入**。v1 的 Budget 直接读
`time.monotonic`，在单测里要么真等 90 秒，要么 monkeypatch 全局时间——两者都会
把测试变成玄学。语义（remaining_ms / slice / exhausted）保持与 v1 一致，
避免两套「预算」概念在同一个仓库里打架。
"""

from typing import Optional

from oncall.clock import Clock

# 剩余预算低于此值时不再发起 LLM 调用：连接 + 排队 + 首 token，300ms 做不完一次有效调用
MIN_TRY_MS = 300


class RunBudget:
    """run 级双预算：墙钟时间 + token 用量。

    时间预算控制「用户等多久」，token 预算控制「这次 run 花多少钱」。
    两者独立：模型在长上下文里便宜地跑 10 步，和 3 步烧掉一个大 prompt，
    是两种不同的失控，一个上限兜不住。token 只在**每次 LLM 调用后**记账并检查——
    API 不会提前告诉你这次要花多少，事后截断是唯一能做的。
    """

    __slots__ = ("total_ms", "token_budget", "_clock", "_deadline", "_tokens_used")

    def __init__(self, total_ms: int, clock: Clock,
                 token_budget: Optional[int] = None) -> None:
        self.total_ms = total_ms
        self.token_budget = token_budget
        self._clock = clock
        self._deadline = clock.now() + total_ms / 1000
        self._tokens_used = 0

    def remaining_ms(self) -> int:
        return max(0, int((self._deadline - self._clock.now()) * 1000))

    def slice(self, want_ms: int) -> int:
        """从预算里切 want_ms；不够就给剩余部分（可能为 0）"""
        return min(want_ms, self.remaining_ms())

    def exhausted(self) -> bool:
        return self.remaining_ms() <= 0

    def usable(self) -> bool:
        return self.remaining_ms() >= MIN_TRY_MS

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> int:
        self._tokens_used += int(prompt_tokens or 0) + int(completion_tokens or 0)
        return self._tokens_used

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    def token_exhausted(self) -> bool:
        return self.token_budget is not None and self._tokens_used >= self.token_budget
