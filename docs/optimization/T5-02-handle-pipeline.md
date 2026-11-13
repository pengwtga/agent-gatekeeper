# T5-01 · 处置编排：确定性状态机 + 只读信息收集 + 人工接管持久化

日期：2026-11-13 ｜ 状态：✅ 已完成

## 动机：从「分类器」到「Agent」缺的最后一块

分类引擎（规则先行 + LLM 仲裁 + 双门禁）已经能给出**可信的结论**，但结论之后没有动作——
分类结果只是被返回，处置仍然全靠人。这一批把闭环补上：**分类 → 信息收集 → 处置映射 →
人工接管续跑**，同时守住项目的立身之本。

## 0 · 为什么不用 LangGraph（原计划改道）

原任务清单 T5-1 写的是 LangGraph 骨架，实现时改为自己写确定性状态机（`app/agents/pipeline.py`
约 180 行），理由与不用 LangChain 同源：

1. **编排不需要智能**：分几步、什么条件走哪条边是业务规则，让 LLM 参与编排等于给
   确定性流程引入不确定性。LLM 已经在 classify/灰区仲裁节点里干活了。
2. **每步可观测**：LangGraph 把「为什么走这条边」埋进图抽象；状态机的每一步进出
   都有留痕（`result["pipeline"]`：stage/status/detail/ms）。
3. **上界是结构保证**：步数上限（max_steps=3）与预算（Budget 贯穿）是不变量，
   不存在「Agent 绕着门禁多绕几圈」的自由度——超限结构性熔断，不靠 prompt 祈祷。

## 1 · 状态机

```
intake ──► classify ──► enrich ──► decide
规整输入    复用分类引擎   只读信息收集   确定性处置映射
```

- **classify**：复用现有引擎（规则先行/容灾链/证据+置信度双门禁原样生效），编排层零改动接入；
- **enrich**：仅在 `needs_human_review` 或灰区时触发——自动调只读工具（`query_incidents`）
  拉同类历史样本，生成人工处置简报（`human_brief`）。高置信自动处置路径直接跳过（不浪费）；
  非注册域如实标注 skipped，不硬凑。
- **decide**：确定性映射，无 LLM 参与：

| 条件 | 处置 | 语义 |
|---|---|---|
| `class == unknown` | `escalate` | 引擎无法归类，转人工兜底 |
| `needs_human_review`（低置信或证据拦截） | `human_review` | 中断落升级队列，等人工决议续跑 |
| 其余（高置信 + 证据过门） | `auto_dispose` | 按 suggested_action 自动处置 |

## 2 · 人工接管持久化（`app/core/escalation.py`）

- human_review 即落 SQLite（WAL）：incident + 分类结果 + 编排留痕整体快照；
- `POST /escalations/{id}/resolve`：人工批准类别后**从快照续跑** decide——不重新分类
  （人已裁决），续跑结果带 `human_override` 留痕（谁批的/批了什么），confidence 置 1.0
  （人工裁决是新的置信上界）；
- 审计语义：决议只允许一次（pending→resolved 不可覆盖），升级记录只追加不篡改；
- 单实例 SQLite 零运维；多实例换 Postgres，接口不变。

## 3 · 上界守护（T5-4 并入）

- 步数：只数编排动作（classify/enrich/decide），intake 不计；超限 `abort` 熔断；
- 预算：Budget 贯穿（分类引擎已在用），耗尽带 `pipeline_exit={reason, uncertainty}` 退出；
- 验收：`max_steps=1` 构造用例确认熔断生效。

## 4 · 对比评测（credit 域，30 条采样 seed=42，真实 LLM 灰区仲裁）

| 指标 | 结果 |
|---|---|
| 准确率 | 28/30（93.3%，与既有口径一致的采样） |
| 处置分布 | auto_dispose 16 / human_review 12 / escalate 2 |
| **错误自动关闭** | **0/2 —— 错分样本 100% 被处置层拦下转人工** |
| 编排增量延迟 | enrich/decide 为确定性代码，P50 增量 <1ms |

两个错分样本（标注 multi_debt → 引擎 unknown）走的正是 escalate：引擎承认不知道 →
处置层不放行 → 人工兜底。**处置层的价值不是提高准确率，是保证错误的代价可控**——
这与「门禁不提高答对率、只保证答错时不做错动作」是同一哲学的延伸。

另一条实测链路（.env 带 key 时）：灰区 → LLM 仲裁 conf 0.8 → 低于 0.85 自动处置阈值 →
human_review 落库——灰区仲裁的输出同样被处置层把关，没有旁路。

## 5 · 端点

- `POST /handle`：编排入口（body: `{"incident": {...}, "domain": "ssrm|csa|credit"}`）；
- `GET /escalations?status=pending|resolved`：升级队列清单（含 pending_count）；
- `POST /escalations/{id}/resolve`：人工决议 + 续跑。

## 已知边界

- auto_dispose 目前仍是「建议执行」而非真执行写操作——副作用工具接权限系统之前
  不进注册表（T5-00 的边界继续成立），处置结果是机器可消费的决策记录；
- enrich 的工具面只有 ssrm 注册表（query_incidents 是 ssrm 知识）；csa/credit 如实
  skipped。接新域的工具 = 在该域 builtin_tools 注册，管线零改动；
- EscalationStore 无清理策略（记录只增不减）——审计语义下暂不删，量大后归档。
