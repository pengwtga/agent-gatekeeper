"""v2 执行层（harness）——CLI + 循环 + 治理，与 v1 的 app/ 单向依赖。

边界（由 tests/test_architecture.py 保证）：
- 可以 import app.core / app.domains / app.observability / app.agents.rag_assistant
- **不许** import app.agents.pipeline（那是 v1 的固定流程，也是对照组）
- app/ 不许 import oncall（v1 必须能独立运行）
"""

__all__ = ["api", "loop", "tools", "policy", "sensors", "context", "budget", "clock"]
