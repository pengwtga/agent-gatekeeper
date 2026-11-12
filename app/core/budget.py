"""请求级超时预算 — Agent 服务的时间治理

问题：LLM 调用的耗时上界 = provider 数 × 单次超时 × (重试+1)，按旧配置最坏
3 × 30s × 2 = 180s，而 nginx proxy_read_timeout=120s → nginx 先 504 断开，
后端还在继续烧 token。各段超时互相独立时，「总时长」没有任何一处负责。

解法：请求入口发一个 Budget（deadline = now + 总预算），下游每个耗时操作
从预算里「切」一段时间出来用；切不出来（剩余 < 最小可用值）就放弃这条路径，
走降级。超时从「各段的局部参数」变成「全请求共享的全局约束」。

设计取舍：
- 用 time.monotonic() 而不是 time.time()：deadline 只用于测时间差，
  monotonic 不受系统对时/NTP 回拨影响。
- 预算不够时「跳过并降级」而不是「硬着头皮调」：一个注定超时的 LLM 调用
  只会白烧 token 和连接，规则基线在几毫秒内就能给出可用结果。
"""

import time

# 剩余预算低于此值（毫秒）时不再发起 LLM 调用：
# 连接建立 + 请求写出 + 服务端排队，300ms 以下不可能完成一次有效调用
MIN_TRY_MS = 300


class Budget:
    """单请求的时间预算。线程安全不做保证——FastAPI 单请求内是协程串行使用。"""

    __slots__ = ("total_ms", "_deadline")

    def __init__(self, total_ms: int):
        self.total_ms = total_ms
        self._deadline = time.monotonic() + total_ms / 1000

    def remaining_ms(self) -> int:
        return max(0, int((self._deadline - time.monotonic()) * 1000))

    def slice(self, want_ms: int) -> int:
        """从预算里切 want_ms 出来；剩余不足则只给剩余部分（可能为 0）"""
        return min(want_ms, self.remaining_ms())

    def exhausted(self) -> bool:
        return self.remaining_ms() <= 0
