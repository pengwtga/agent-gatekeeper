# 生产化优化记录

这个目录记录 ssrm-aiops 从「可复现的 Demo」走向「可运维的服务」的每一步改动。
每个任务一份文档，结构固定：**问题 → 判断 → 方案取舍 → 改动 → 验收 → 边界 → 面试讲法**。

## 目录

| 文档 | 内容 |
|---|---|
| [`00-production-gap-and-roadmap.md`](00-production-gap-and-roadmap.md) | 与生产级 Agent 的差距诊断、L0~L5 分级、优化路线 |
| [`01-task-list.md`](01-task-list.md) | 33 个任务的拆解（批次、工时、验收标准） |
| [`T0-01-json-mode-fallback.md`](T0-01-json-mode-fallback.md) | 修复「宽松 JSON 降级」不可达 |
| [`T0-02-rate-limit-real-ip.md`](T0-02-rate-limit-real-ip.md) | 限速改用真实客户端 IP |
| [`T0-03-evidence-grounding.md`](T0-03-evidence-grounding.md) | 证据可回溯校验（白盒门禁） |
| [`T0-04-token-accounting.md`](T0-04-token-accounting.md) | 输出上界与 token 成本可见 |
| [`T1-01-request-budget.md`](T1-01-request-budget.md) | 请求级超时预算（deadline propagation 最小实现） |
| [`T1-02-shared-http-pool.md`](T1-02-shared-http-pool.md) | 共享连接池 + connect/read 三段超时 |
| [`T1-03-human-review-gate.md`](T1-03-human-review-gate.md) | 门禁判定收口到后端（单一来源） |
| [`T1-04-nginx-alignment.md`](T1-04-nginx-alignment.md) | nginx 超时与请求预算对齐 |
| [`T1-05-graceful-shutdown.md`](T1-05-graceful-shutdown.md) | 长请求服务的优雅退出 |

## 进度

| 批次 | 主题 | 状态 |
|---|---|---|
| **T0** | 修 3 个硬伤 + 成本可见 | ✅ 已完成（17 项验收全通过） |
| **T1** | 预算、连接、门禁收口 | ✅ 已完成（19 项验收全通过；T1-4/T1-5 的 docker 实测待容器化验证） |
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
