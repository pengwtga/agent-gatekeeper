# 生产化优化记录

这个目录记录本项目（原名 ssrm-aiops，现名 agent-gatekeeper）从「可复现的 Demo」走向「生产化 Agent 骨架」的每一步改动。
每个任务一份文档，结构固定：**问题 → 判断 → 方案取舍 → 改动 → 验收 → 边界 → 面试讲法**。

## 目录

| 文档 | 内容 |
|---|---|
| [`00-production-gap-and-roadmap.md`](00-production-gap-and-roadmap.md) | 与生产级 Agent 的差距诊断、L0~L5 分级、优化路线 |
| [`01-task-list.md`](01-task-list.md) | 38 个任务的拆解（批次、工时、验收标准） |
| [`T0-01-json-mode-fallback.md`](T0-01-json-mode-fallback.md) | 修复「宽松 JSON 降级」不可达 |
| [`T0-02-rate-limit-real-ip.md`](T0-02-rate-limit-real-ip.md) | 限速改用真实客户端 IP |
| [`T0-03-evidence-grounding.md`](T0-03-evidence-grounding.md) | 证据可回溯校验（白盒门禁） |
| [`T0-04-token-accounting.md`](T0-04-token-accounting.md) | 输出上界与 token 成本可见 |
| [`T1-01-request-budget.md`](T1-01-request-budget.md) | 请求级超时预算（deadline propagation 最小实现） |
| [`T1-02-shared-http-pool.md`](T1-02-shared-http-pool.md) | 共享连接池 + connect/read 三段超时 |
| [`T1-03-human-review-gate.md`](T1-03-human-review-gate.md) | 门禁判定收口到后端（单一来源） |
| [`T1-04-nginx-alignment.md`](T1-04-nginx-alignment.md) | nginx 超时与请求预算对齐 |
| [`T1-05-graceful-shutdown.md`](T1-05-graceful-shutdown.md) | 长请求服务的优雅退出 |
| [`T2-01-trace-id.md`](T2-01-trace-id.md) | trace_id 与请求上下文（contextvar） |
| [`T2-02-json-logs.md`](T2-02-json-logs.md) | 结构化 JSON 日志（事件即表名） |
| [`T2-03-metrics.md`](T2-03-metrics.md) | 五个指标与滑动窗口分位数 |
| [`T2-04-cost-attribution.md`](T2-04-cost-attribution.md) | 请求级 token 归因 |
| [`T2-06-observability-dashboard.md`](T2-06-observability-dashboard.md) | 可观测面板（分钟级趋势 + 请求流水） |
| [`T3-00-rag-retrieval-eval.md`](T3-00-rag-retrieval-eval.md) | RAG 检索评测（Recall/MRR + 阈值扫描，T3 前置） |
| [`T3-01-prompt-versioning.md`](T3-01-prompt-versioning.md) | Prompt 版本化（index + 启动校验 + 结果留痕）+ 20 条回归集 + `make eval` |
| [`T3-02-evidence-refs.md`](T3-02-evidence-refs.md) | 证据判据换结构化引用（消除分数反转），v1/v2 回归对比 + 标定脚本 |
| [`T5-00-domain-pack-and-tools.md`](T5-00-domain-pack-and-tools.md) | 泛化三件事：Domain Pack 剥离 + 工具调用层 + CSA 第二领域验证 |
| [`T5-01-assistant-kb-pack.md`](T5-01-assistant-kb-pack.md) | 助手知识库 Pack 化（AssistantSpec）+ 三域 KB/评测集扩充 + bigram 打分改造 |
| [`T5-02-handle-pipeline.md`](T5-02-handle-pipeline.md) | 处置编排：确定性状态机 + 只读信息收集 + SQLite 人工接管续跑（credit 错分 2/2 全部被处置层拦下） |
| [`T1-06-startup-and-provider.md`](T1-06-startup-and-provider.md) | 接真 Provider 后暴露的启动期问题（预热阻塞 / 429 误判 / 空 reason） |
| [`T6-01-main-py-slim.md`](T6-01-main-py-slim.md) | main.py 瘦身（340 行页面代码抽出） |
| [`T6-05-ui-shell-and-obs-split.md`](T6-05-ui-shell-and-obs-split.md) | 左侧栏外框 + 可观测拆三页（共享 metrics context） |
| [`T6-06-app-package-layout.md`](T6-06-app-package-layout.md) | app/ 分包（15 个平铺文件 → core/agents/observability/evaluation） |
| [`T7-02-trace-spans.md`](T7-02-trace-spans.md) | 请求链路（span 埋点 + 瀑布图，内存版） |

## 进度

| 批次 | 主题 | 状态 |
|---|---|---|
| **T0** | 修 3 个硬伤 + 成本可见 | ✅ 已完成（17 项验收全通过） |
| **T1** | 预算、连接、门禁收口 | ✅ 已完成（19 项验收全通过；T1-4 容器实测 nginx -t / 反代 / 限流 429 / Retry-After，T1-5 容器实测四个退出场景；T1-6 接真 Provider 后补 7 项启动期验收） |
| **T2** | 可观测 | ✅ 已完成（24 项验收全通过；容器实测 `docker logs` 逐行可解析 JSON；T2-6 面板已上页，接口 + 页面共 7 项验收） |
| T3 | Prompt 版本化 + 回归 | ✅ 全部完成：T3-0 检索评测集 + Recall/MRR + 阈值扫描；T3-1~4 prompts/ + 启动校验 + 20 条回归集 + `make eval`；T3-5 证据改结构化引用（v2 基线：回归 0.95 / 困难 0.96，回归集门禁误杀 3→0） |
| T4 | 检索升级 | ✅ 已完成：T4-2/3 被 T3-0 超越（230 条查询 + Recall/MRR/双口径）；T4-6 修重排伪造分数（keyword+rerank 门禁失效 bug）；T4-5 向量缓存按 KB 内容 hash 失效；T4-4 RRF 混合检索（顺带修复评测 PYTHONHASHSEED 漂移）；T4-1 切块实验结论「不做」（resolution 入索引全线变差，credit 87.5%→52.5%） |
| T5 | 状态机 + 工具 | ✅ T5-0（自研工具层 + Domain Pack）+ T5-02（处置编排状态机 + 人工接管续跑，不用 LangGraph，理由见文档） |
| T6 | 工程卫生 | ⏳ T6-1 已完成（main.py 593 → 273 行），T6-5 已完成（左侧栏 + 可观测拆页），T6-6 已完成（app/ 分包），T6-2~4 未做 |
| T7 | 观测与诊断平台化（千问 JD 定向） | ⏳ T7-2 一阶段完成（内存版链路 + 瀑布图），SQLite 化未做 |

## 本地验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q          # T6-2 之后
.venv/bin/python -m uvicorn app.main:app --port 8001
```

单个任务的验收命令写在各文档的「验收」一节，可以直接复制执行。
