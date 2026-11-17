# v2：oncall-harness 功能拆解

> 本文是 v2 的**总纲**：定边界、定形态、定迭代。每个功能点的实现细节在 `F*.md`，一个功能一个文档。
> v1（`app/`）不动，它既是线上服务，也是 v2 的对照组（基线 A）。

---

## 1. 一句话与边界

v2 = **在同一个仓库里新增一层执行层（harness）**，把「一次排障」从固定流程变成可循环、可审批、可验收、可复现的运行。

- **进 v2 的**：`oncall/`（CLI + harness）、`bench/`（三组评测）、`experiments/langgraph_baseline/`（基线 C）
- **不动的**：`app/`（Web/API + 固定 pipeline）、`data/`、`prompts/`、现有 108 个测试
- **依赖方向**：`oncall → app.core / app.domains / app.observability`，**禁止反向**，**禁止 `oncall` import `app.agents`**（由 `tests/test_architecture.py` 保证）

---

## 2. 三条能力面：形态匹配，而不是全部 agent 化

这是 v2 最重要的一张表，也是面试被问「为什么根因不改成 agent」时的答案：

| 能力面 | v1 形态 | v2 形态 | 为什么 |
|---|---|---|---|
| 根因分类 | 单次决策（workflow） | **保持 workflow**，并降级为 harness 里的一个**确定性工具** | 单决策、无分支、无环。加 loop 只会 p95 变差、成本翻倍、评测面翻倍，收益为 0 |
| 检索助手 | 一次检索 + 一次生成 | **agentic RAG**：检索 → 自评证据够不够 → 改写 → 再检索（≤3 轮） | 「问得不清楚」是真实存在的不确定性，需要多轮修正；有环才有收益 |
| 排障 | 无 | **harness agent**：多步取证、假设-验证、写操作审批 | 天然多步、路径不可预知、有副作用——harness 的主战场 |

一句话：**不是所有东西都该变成 agent；把确定性的部分做成工具，把不确定的部分交给 loop。** v2 的价值一半来自「加了 loop」，一半来自「知道哪里不加」。

---

## 3. 功能树与文档索引

| 编号 | 功能 | 迭代 | 依赖 | 文档 |
|---|---|---|---|---|
| F01 | CLI 入口与输出契约 | I1 | — | [F01-cli.md](F01-cli.md) |
| F02 | Agent loop（终止/预算/无进展检测） | I1 | F01 F03 | [F02-loop.md](F02-loop.md) |
| F03 | 工具适配层（复用四道关 + 副作用分级） | I1 | — | [F03-tools-adapter.md](F03-tools-adapter.md) |
| F04 | 上下文装配与压缩 | I2 | F02 | [F04-context.md](F04-context.md) |
| F05 | 权限网关与审批（HITL） | I2 | F03 F06 | [F05-policy-approval.md](F05-policy-approval.md) |
| F06 | Checkpoint / resume | I2 | F02 | [F06-checkpoint.md](F06-checkpoint.md) |
| F07 | 验收传感器（证据/schema/dry-run/reviewer） | I2 | F03 | [F07-sensors.md](F07-sensors.md) |
| F08 | MCP client（接远程工具） | I3 | F03 F05 | [F08-mcp-client.md](F08-mcp-client.md) |
| F09 | MCP server（把能力暴露出去） | I3 | F03 | [F09-mcp-server.md](F09-mcp-server.md) |
| F10 | Skill 运行时加载 | I3 | F04 | [F10-skills.md](F10-skills.md) |
| F11 | Sub-agent 并行取证 | I3 | F02 F07 | [F11-subagent.md](F11-subagent.md) |
| F12 | Agentic RAG（检索助手升级） | I3 | F02 F04 | [F12-agentic-rag.md](F12-agentic-rag.md) |
| F13 | 轨迹评测与三组对照 | I4 | 全部 | [F13-eval-bench.md](F13-eval-bench.md) |
| F14 | 观测扩展（每步成本与 run 视图） | I4 | F02 | [F14-observability.md](F14-observability.md) |
| F15 | 事故记忆 | I4 | F13 | [F15-memory.md](F15-memory.md) |
| F16 | 入口分层与测试策略（贯穿） | I1~I4 | 全部 | [F16-testing.md](F16-testing.md) |

---

## 4. 目录与入口

```
oncall/
  __main__.py        # typer：ask / run / resume / approve / mcp serve
  loop.py            # F02
  context.py         # F04
  policy.py          # F05
  checkpoint.py      # F06
  sensors.py         # F07
  subagent.py        # F11
  tools.py           # F03（Thin adapter）
  mcp/client.py      # F08
  mcp/server.py      # F09
  skills_loader.py   # F10
bench/
  run.py             # F13：--arm pipeline|harness|langgraph
  judge.py
experiments/langgraph_baseline/   # 基线 C，只被 bench import
tests/test_architecture.py        # 依赖方向守卫
```

**一个仓库三条入口**：`python -m oncall`（CLI）、`uvicorn app.main:app`（v1 Web）、`python -m bench.run`（评测）。三者互不 import 对方的流程代码。

---

## 5. 迭代与验收

| 迭代 | 内容 | 演示物 | 硬验收 |
|---|---|---|---|
| I1 | F01 F02 F03 | `oncall ask "XF-885 注册卡单"` 出带证据结论 | 10 条单跳 pass@1 ≥【待实测】；平均步数 ≤ 6 |
| I2 ✅ | F04 F05 F06 F07 | 写操作挂起落库，`oncall approve` 后续跑；`oncall resume` 断点续跑 | 危险动作审批前执行 **0 次**（单测断言）；同审批重复执行 handler 仅 1 次（幂等键）；超时默认拒绝；destructive 双人确认；137 passed |
| I3 ✅（F10/F11 未做） | F08 F09 F12 | `oncall serve` 被外部 client list+call 成功（子进程往返测试）；接两个同名 server 前缀不覆盖；多轮检索 `rag_search` 真模型 5 步跑通 | 外部 host list+call 成功（✅）；外部工具默认 WRITE、显式 trust 才降级（✅）；多跳集评测集已建（bench/dataset.py 30 条），真模型 nightly 已跑（见 I4 行） |
| I4 ✅（F13/F14/F15） | F13 F14 F15 | `python -m bench.run --arm all --report bench/report.md` 一条命令出报告；`oncall trace/runs` 轨迹视图；事故记忆注入「历史同类」段 | 判分/门禁/报告全链路通（lowfi：B 100%、危险误执行 0、gate PASS）。**真模型 nightly 已跑（glm-4-flash，4 轮）**：B 23%、危险 100%+误执行 0、拒答 40%、C（装了 langgraph）23~27%、A 63%——归因与四轮修复记录见 bench/report-real.md；类名准确率受弱模型限制，S2 门禁把单源断言全部拦成转人工（安全兜底在工作）。CI 门禁已按实测校准为回归基线（thresholds.json）。F15 §7.1 重复事故步数下降待多次 nightly 累积记忆后再测 |

---

## 6. 域扩展：为什么把「供应商准入审批」当第二个 domain pack

它**不是第二个产品，是一次解耦验证 + 一条能力面覆盖不到的路径**。三个理由：

1. **证明 harness 与领域无关**。v1 已经用 `DomainPack` 证明「分类体系可替换」，v2 要证明「执行层也可替换」。同一个 harness，换 pack 就能跑另一个领域的任务，这是可迁移性的唯一硬证据。
2. **它覆盖 SRE 排障测不到的路径**：v2 的排障 90% 是只读，而准入是**写操作 + 多级审批 + 长挂起（人可能几小时后才批）+ 幂等要求**。F05 的「审批可恢复」和 F06 的「重放不重复副作用」如果不做这个域，就只有纸面设计。
3. **它天然是 workflow，正好当反例**。准入流程的步骤是规定好的（提交→校验→审批→开通），把它做成 agent 是失真。所以它在 v2 里的定位是：**同一个 harness 承载 workflow 形态**——用来证明 harness 不是「只能跑 agent」，而是「能约束自主性」。这比再造一个 agent 有说服力。

**覆盖的概念**：HITL 审批与 interrupt、durable execution（长挂起）、幂等写、权限分级、Skill（准入规则与话术）、评测（结论二元、易判）、workflow vs agent 的边界。

**明确不做**：不重写 EA/Dora 的接口，只做 mock adapter；不做 UI；不追求业务完整度，只追求「跑通一条带审批的写链路」。

---

## 7. 从 v1 复用什么（不要重写）

| 资产 | 位置 | v2 怎么用 |
|---|---|---|
| 工具四道关 | `app/core/tools.py`（`ToolRegistry.specs/get/validate_args`） | F03 薄适配，接口形状已经是 MCP 的 |
| 时间预算 | `app/core/budget.py` | F02 的墙钟预算直接复用 |
| 链路追踪 | `app/observability/tracing.py`（`span()`） | F14 加 parent 层级 |
| 多 Provider 容灾 | `app/core/llm_client.py` | 不重写，loop 里只当客户端用 |
| 证据字段去重判据 | `app/agents/classifier.py` | F07 复用同一口径，避免两套标准 |
| KB 与簇结构 | `data/*/`、`app/kb/validate.py` | F10 F12 F15 的检索与记忆都按簇组织 |
| 域包机制 | `app/domains/__init__.py` | 第 6 节的第二个 domain pack |

---

## 8. 全局验收（面试可引用的数字位）

- 三组对照：A pipeline / B harness / C LangGraph，同模型、同工具实现、同数据集、同 judge
- 必报指标：pass@1、关键步骤命中率、平均步数、无效调用率、token/任务、p95、危险动作误执行率
- 必报的坏消息：哪几条 B 比 A 差、哪几条 C 比 B 好——**只报好消息的报告不可信**
- 所有 `【待实测】` 占位必须在 I4 结束前填上，或明确写成「未测」

---

## 9. 不做什么（写在明处）

1. 不做通用 agent 框架（不做第二个 LangGraph）
2. 不做多 agent 自由对话（三个 agent 互聊，token 三倍、结论不变）
3. 不做前端大改（面板复用一个列表页，够看轨迹即可）
4. 不引入向量数据库（簇内检索够用，且可解释）
5. 不追 MCP 全部扩展（Tasks / Skills over MCP 只读文档，不实现）
