# 生产化优化记录

这个目录记录 ssrm-aiops 从「可复现的 Demo」走向「可运维的服务」的每一步改动。
每个任务一份文档，结构固定：**问题 → 判断 → 方案取舍 → 改动 → 验收 → 边界 → 面试讲法**。

## 目录

| 文档 | 内容 |
|---|---|
| [`00-production-gap-and-roadmap.md`](00-production-gap-and-roadmap.md) | 与生产级 Agent 的差距诊断、L0~L5 分级、优化路线 |
| [`01-task-list.md`](01-task-list.md) | 32 个任务的拆解（批次、工时、验收标准） |
| [`T0-01-json-mode-fallback.md`](T0-01-json-mode-fallback.md) | 修复「宽松 JSON 降级」不可达 |
| [`T0-02-rate-limit-real-ip.md`](T0-02-rate-limit-real-ip.md) | 限速改用真实客户端 IP |
| [`T0-03-evidence-grounding.md`](T0-03-evidence-grounding.md) | 证据可回溯校验（白盒门禁） |
| [`T0-04-token-accounting.md`](T0-04-token-accounting.md) | 输出上界与 token 成本可见 |

## 进度

| 批次 | 主题 | 状态 |
|---|---|---|
| **T0** | 修 3 个硬伤 + 成本可见 | ✅ 已完成（17 项验收全通过） |
| T1 | 预算、连接、门禁收口 | ⬜ 待开始 |
| T2 | 可观测 | ⬜ |
| T3 | Prompt 版本化 + 回归 | ⬜ |
| T4 | 检索升级 | ⬜ |
| T5 | 状态机 + 工具（LangGraph） | ⬜ |
| T6 | 工程卫生 | ⬜ |

## 本地验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q          # T6-2 之后
.venv/bin/python -m uvicorn app.main:app --port 8001
```

单个任务的验收命令写在各文档的「验收」一节，可以直接复制执行。
