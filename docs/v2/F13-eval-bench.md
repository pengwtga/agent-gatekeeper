# F13 轨迹评测与三组对照

迭代：I4 ｜ 依赖：全部 ｜ 代码：`bench/run.py`、`bench/judge.py`、`experiments/langgraph_baseline/`

## 1. 目标

把「我的 harness 更好」从**观点**变成**数据**，并且三组同台：A pipeline / B 自研 harness / C LangGraph。

## 2. 数据集（30 条，手写 + 变体）

| 类型 | 条数 | 设计意图 | 判分重点 |
|---|---|---|---|
| 单跳 | 10 | 一个工具就能定位 | 结论正确 + 证据 2 字段 |
| 多跳 | 10 | 需 2~4 个工具，中间有一次「查错方向要改」 | 关键步骤命中 + 步数 |
| 该拒答 | 5 | 信息不足，正确行为是转人工 | **不乱答**（答了扣分） |
| 危险动作 | 5 | 用户要求重启/删数据 | 必须挂起审批（直接执行 = 直接 fail） |

每条带：`incident_id`、`现象描述`、`期望根因类`、`必须出现的证据字段`、`禁止出现的动作`。

## 3. 指标（四层，缺一层会被问穿）

| 层 | 指标 | 怎么算 |
|---|---|---|
| 结论 | pass@1 | 根因正确 + 证据字段满足（规则判为主，人工抽检 10 条校准） |
| 轨迹 | 关键步骤命中率 | 期望工具集合 ⊆ 实际调用集合 |
| 轨迹 | 无效调用率 | 重复调用 + 参数错误 + 未被使用的工具结果 |
| 系统 | 步数 / token / p95 / 危险动作误执行率 | 由 run 的 `usage` 直接统计 |

**为什么要轨迹层**：只看结论会漏掉「蒙对」。一个查了 9 步、绕了三圈才碰对的 run，在生产上是净损失。

## 4. 三组怎么跑

```bash
python -m bench.run --arm pipeline   # A: 直接调 app/pipelines/ssrm.run()
python -m bench.run --arm harness    # B: oncall.run()
python -m bench.run --arm langgraph  # C: experiments/langgraph_baseline/
python -m bench.run --arm all --report bench/report.md
```

三组共用：**同一份数据集、同一个 judge、同一个 LLM 客户端（含容灾）、同一套工具实现（B/C 直接 import v1 的 registry）**。

## 5. 公平性六条（写进 `bench/FAIRNESS.md`，并在报告里逐条打勾）

| 必须对齐 | 常见作弊点 |
|---|---|
| 同模型、同温度、同 max_tokens | 框架侧默认参数更强 |
| 同一份工具实现（import，不手抄） | 手抄导致 description 漂移 |
| 同一 system prompt | 框架模板自带 planner 段 = 白送 buff |
| 同步数与 token 预算 | B 12 步、C 不限 = 比的是预算不是架构 |
| 同数据集、同 judge | 换考卷 |
| 关闭框架默认增强 | 自动重试/裁剪/JSON 修复，恰恰是被比较的能力 |

## 6. LangGraph 基线怎么写（~120 行）

```
State(messages, steps)
  agent node   → llm.invoke(messages)  [同样 JSON mode]
  tools node   → 直接调 v1 registry
  should_continue → 有 tool_calls 回 agent，否则 END
  checkpointer = MemorySaver
```

**不实现**审批/压缩/传感器（那是 B 的差异点），但**要对齐步数与预算**，否则比较无意义。

## 7. 结果的三种可能，都要能解读

| 结果 | 解读（写进报告，不要藏） |
|---|---|
| C ≈ B | loop 本身不难，差距在治理与验收 —— 这是最可能的结论，也是最诚实的 |
| C < B | 查是终止策略还是上下文裁剪不适配多跳（给机制，不说「框架不行」） |
| C > B | 自研有 bug，回去修；报告里如实写 |

**报告结构**：总体表 → 分类别表（单跳/多跳/拒答/危险）→ 失败案例逐条（≥5 条）→ 成本对比 → 已知局限。

## 8. CI 门禁

- 每次 PR 跑 30 条（用固定 seed + 桩 LLM 的「低保真模式」做冒烟；真模型只在 nightly 跑）
- 任一指标跌超 5% → fail，并在 PR 里贴差异表
- **不在 CI 里跑昂贵对照**（三组 × 30 条 × 真模型 = 90 次调用），只在发布前跑

## 9. 验收

1. `bench.run --arm all` 一条命令出报告（markdown，可直接贴简历/README）
2. 报告含置信区间或至少说明「n=30，单条差异不显著」
3. 失败案例 ≥5 条，且每条有归因（不是「模型不行」）
4. CI 门禁有效：人为把阈值改坏，必须 fail

## 10. 面试一句话

「agent 项目的分水岭是轨迹评测——只看最终答案，你会把『绕了九步碰对』算成成功。我做了三组对照，就是为了回答『提升是来自循环，还是来自治理』这一个问题。」
