"""bench 的评测桩（低保真模式）。

与 tests/v2/stubs.py 的 ScriptedLLM 同实现——但评测代码不 import 测试目录，
这里独立一份（三行核心逻辑，漂移风险可控，且双方都有各自测试钉着）。
低保真模式只验证**链路**（跑通 / 判分 / 门禁 / 报告），数字不代表模型能力，
报告头部必须标注 mode=lowfi。
"""

from __future__ import annotations

import copy
from typing import Any


class ScriptedLLM:
    """按顺序吐出预设响应；脚本用完后重复最后一条。

    is_configured=True：A 组的 classifier 会先查这个属性决定走 LLM 通道还是降级——
    桩要冒充「已配置的模型」，否则评测跑的根本不是 LLM 通道。
    """

    is_configured = True

    def __init__(self, script: list[dict]) -> None:
        if not script:
            raise ValueError("script 不能为空")
        self.script = script
        self.calls = 0

    async def chat_json(self, system_prompt: str, user_payload: str,
                        budget: Any = None) -> dict:
        self.calls += 1
        return copy.deepcopy(self.script[min(self.calls - 1, len(self.script) - 1)])
