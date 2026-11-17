"""v2 测试替身：脚本化 LLM / 假时钟 / 计数工具。

三条纪律：
1. LLM 必须**按步**返回不同响应——永远返回同一句的桩只能测单步
2. 时钟必须可注入——否则预算用例要真等 90 秒
3. 危险动作用**计数**断言「一次都没被调」，而不是只看结果对不对
"""

import copy
from typing import Any, Optional

from app.core.tools import Tool, ToolRegistry


class ScriptedLLM:
    """按顺序吐出预设响应；脚本用完后重复最后一条（模拟模型卡在原地）。"""

    def __init__(self, script: list[dict]) -> None:
        if not script:
            raise ValueError("script 不能为空")
        self.script = script
        self.calls = 0
        self.payloads: list[str] = []

    async def chat_json(self, system_prompt: str, user_payload: str, budget: Any = None) -> dict:
        self.calls += 1
        self.payloads.append(user_payload)
        return copy.deepcopy(self.script[min(self.calls - 1, len(self.script) - 1)])


class FakeClock:
    """手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance_ms(self, ms: int) -> None:
        self._t += ms / 1000


class CountingTool:
    """记录调用次数；可选：每次调用消耗时钟（用来触发预算耗尽）。"""

    def __init__(self, ret: Optional[Any] = None, clock: Optional[FakeClock] = None,
                 cost_ms: int = 0) -> None:
        self.n = 0
        self.ret = ret if ret is not None else {"ok": True}
        self.clock = clock
        self.cost_ms = cost_ms

    async def __call__(self, **kwargs: Any) -> Any:
        self.n += 1
        self.last_args = kwargs
        if self.clock is not None and self.cost_ms:
            self.clock.advance_ms(self.cost_ms)
        return self.ret


def build_registry(*entries: tuple[str, dict, Any]) -> ToolRegistry:
    """entries: (name, params, handler)。params 传 {} 表示无参数工具。"""
    reg = ToolRegistry()
    for name, params, handler in entries:
        reg.register(Tool(name=name, description=f"测试工具 {name}", params=params, handler=handler))
    return reg


FINISH_OK = {
    "action": "finish",
    "conclusion": "xflow 回调地址仍是旧域名，更新配置并重放回调",
    "root_cause_class": "callback_lost",
    "evidence": [
        {"field": "log", "ref": "run#step:1", "quote": "POST /supplier/created 404"},
        {"field": "config", "ref": "cfg:xflow.callback_url", "quote": "http://old-ssrm/"},
    ],
    "confidence": 0.9,
}
