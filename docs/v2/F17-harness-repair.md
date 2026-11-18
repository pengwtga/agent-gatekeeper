# F17 门禁退回之后：一轮修正 + 确定性补齐

> 归属迭代 I5。前置：F07 传感器（S1/S2/S3）、F02 loop、F13 评测。

## 1. 问题：门禁拦对了，但只用了一次

真模型 nightly 里 B 组单跳/多跳 pass@1 长期 0%，失败原因高度集中：

```
S2 证据只覆盖 1 个字段（要求 ≥2）：['kb']
```

模型行为很稳定：查一次 `rag_search` → 直接断言根因 → 被 S2 拦 → 转人工。
门禁的判断是对的（单源断言不该自动处置），但**它把两类完全不同的事同等对待了**：

| 情形 | 正确反应 | 之前的行为 |
|---|---|---|
| 知识库里确实没有依据 | 转人工（不该编） | 转人工 ✅ |
| 模型只是少查了一个来源 | 让它补齐再判断 | 转人工 ❌ |

第二类是可修的错误，修法有两个，都在 harness 层：

- **F17-1 修正轮**：把门禁错误变成下一轮的输入，给它一次机会；
- **F17-2 确定性补齐**：缺口是「只差 history」这种明确规则时，harness 自己补。

## 2. F17-1 修正轮：退回 ≠ 终态

`loop.py` 的 finish 分支从「直接返回」改成「先判、再决定退不退」：

```
finish → check_finish
   ├─ 通过 → 返回 ok
   ├─ 未通过 且 已用满 max_finish_repairs → 返回 needs_human（原 verdict，证据保留）
   └─ 未通过 且 还有额度 → 生成 feedback 段 → continue（下一轮上下文多一段）
```

四个设计细节：

1. **反馈放在上下文最后一段**（`## 上一轮被门禁退回`）。模型对末尾指令依从度最高，
   而这是「必须马上改」的指令；放在开头会被事实段稀释。
2. **只喂一轮**：`feedback` 用完即清。模型据此改了，说明它只是「没想到」；
   没改，说明它「改不动」，继续留着只会污染上下文、烧 token。
3. **上限默认 1 轮**（`max_finish_repairs`）。这是「可修错误」的额度，不是重试机制——
   重试同一件事是 I1 无进展检测要拦的行为。
4. **退回时结论与证据原样留着**。人复核时看到的是模型真实说过的话，
   而不是一句「被门禁拒绝」——这一点和审批挂起保留 args 是同一个原则。

## 3. F17-2 确定性补齐：补事实，不补引用

触发条件（**全部**满足才动手）：

| 条件 | 为什么 |
|---|---|
| `root_cause_class != unknown` | 纯查询（「库里有几条」）本来就只该有一个权威来源，S2 不管；补了就是逼它凑数 |
| 证据里有 kb 源、没有 history 源 | 缺口必须是**明确且唯一**的那一个，不能猜 |
| **KB 的命中条目里出现过这个类名** | 否则就是「给一个 KB 从未支持的断言制造第二来源」——门禁被补齐动作绕过（见 §6.2） |
| 本次 run 还没补过 | 只补一次。补两次就变成「替模型刷证据」 |
| 正在修正轮内 | 只在被退回后补，不在正常路径上抢模型的决策 |
| `query_incidents` 在注册表里且声明为 READ | 副作用表 fail-closed：不是显式只读工具就不代执行 |

动作：`call_tool("query_incidents", {root_cause_class: 类名, limit: 3})`，
结果按**正常步骤**落 `steps` + `facts` + checkpoint，`ref` 与模型自己调的步骤同构
（`run#step:N`）。所以 trace 里它是一步正常的只读调用，不搞特殊通道。

**最关键的边界：harness 只补事实，不补 evidence。**

evidence 必须由模型自己引用。如果 harness 顺手把 `{"field": "history", "quote": ...}`
写进结论，那就是「系统自己给自己出证据」，证据门禁从此失去意义。
这条边界也是「确定性环节回 pipeline、判断与引用留给模型」的落点。

实测踩到一个坑：第一版反馈里给了个模板

```
evidence 写 {"field":"history","ref":"...","quote":"该条工单原文片段"}
```

弱模型直接把占位句当原文抄进 quote 交了。改成「**从该条事实的内容里摘 10~40 字原文**，
不要照抄这句说明」后，第三轮引用的是真原文。

> 顺带暴露一个已知缺口：S3 只校验 ref/quote 非空，没做 **S5 交叉核对**
> （quote 是否真出现在对应工具返回里）。占位符能过 S3，就是缺 S5 的直接证据。

## 4. 门禁一个字没改

S1 阈值、S2 的 ≥2 字段与「只在断言根因时要求」、S3、审批状态机、无进展检测、
五终止条件——全部不变。F17 只改**被拒之后做什么**，不改「什么算合格」。
这是判断一次优化是「提升能力」还是「降低标准」的分界线。

## 5. 一次真实链路（真模型，SH-01）

完整逐轮原文见 [`example-trace.md`](example-trace.md)（由 `python -m bench.demo_trace` 录制，
每轮输入都是 `build_messages()` 的真实产物）。

| 轮次 | 模型看到的关键输入 | 模型动作 | harness 动作 |
|---|---|---|---|
| 1 | 事实 `（暂无）`、步骤 `（还没查任何东西）` | `rag_search("xFlow 审批单已 APPROVED 但 SSRM 没建任务")` | 执行检索（内部 1 轮，`enough=true`）→ 记 fact#1 |
| 2 | 事实：`rag_search` 命中 ONCALL-002（含类名与处置） | `finish`，evidence 只有 `kb` 一条 | S2 退回 → **自动** `query_incidents(类名)` → fact#2 → 生成反馈段 |
| 3 | 事实 2 条 + 步骤 2 条 + `## 上一轮被门禁退回` | `finish`，evidence = `kb` + `history`，quote 摘原文 | S1/S2/S3 全过 → **ok** |

同样是这个模型、这份 prompt：优化前这条会停在「needs_human」，优化后 3 轮 2 步拿到 ok。

## 6. 代价与风险

### 6.1 成本

失败路径多 1 次 LLM 调用（实测 5~10s、约 4K token）；成功路径零成本。
消融两侧的平均工具调用数：bare 0.33 步 → F17 0.8 步（多出来的是被退回后的补齐与重试）。

### 6.2 真模型消融暴露的两个真问题

`--arm harness` vs `--arm harness_bare`（同模型、同数据集、同 prompt，各 30 条）：

| 指标 | bare（= I4） | F17（无 KB 约束） | F17（加约束后） |
|---|---|---|---|
| pass@1 | 23% | 33% | 见 §6.4 |
| 单跳 | 0% | 50% | 同上 |
| 拒答 | 40% | **20%** | 同上 |

**问题①：修正轮会把「该转人工」推成「继续找证据」。**
拒答题里 KB 往往能召回语义相近的条目（`enough=true`），模型被退回一次后拿它当依据作答，
judge 判「答了不该答的」。这不是门禁坏了，是**「再找找」这个动作本身带有乐观偏置**。

**问题②：无约束的补齐等于给断言背书。**
只要模型写得出类名，harness 就去拉该类的历史工单——KB 里根本没有这个类也能补出
「history 来源」。这直接架空了 S2：两个来源不再是两个独立证据，而是「断言 + 断言的镜像」。
修法是补一条硬条件：**KB 命中条目的 `class` 必须真的等于断言的类名**，否则不代查
（`_kb_supports_class`，含截断结果的字面退回匹配，宁可少补不可错补）。

### 6.3 风险：修正轮会掩盖模型能力问题

所以 `repairs` 进 `Outcome` / CLI JSON / 报告，面板上能看出「这条 ok 是修出来的」。
不记录修复过程的 agent 平台，等于把所有弱模型的问题都记账成「人工介入率下降」。

### 6.4 评测公平性

B 组多了两个能力，C 组（LangGraph 裸图）没有。处理办法不是关掉，而是提供消融组
`--arm harness_bare`（`repairs=0` + 关闭 auto_evidence），把两个数字都摆出来，
并在 `bench/FAIRNESS.md` 声明「提升要写成 A→B 的差距，不能写成 harness 比框架强」。

## 7. 验收（tests/v2/test_i5.py，9 例）

| 用例 | 钉住的契约 |
|---|---|
| `test_repair_round_fills_history_and_finishes_ok` | 只差 history 时自动补齐一次，随后 ok |
| `test_feedback_is_written_into_next_prompt_and_cleared_after` | 反馈进下一轮 prompt；模型改用工具后不再重复喂 |
| `test_repair_is_capped_and_falls_back_to_needs_human` | 上限 1 轮；模型改不动就转人工，`llm.calls == 2` |
| `test_class_unknown_skips_auto_evidence` | 纯查询不触发补齐 |
| `test_history_present_means_no_auto_fill` | 两个来源齐了（但结论太短）只走反馈，不重复代查 |
| `test_auto_evidence_skipped_when_tool_absent` | 注册表没有该工具 → 不越权、不报错 |
| `test_repairs_zero_restores_i4_behaviour` | 开关关闭 = I4 行为（1 次 LLM 调用） |
| `test_repairs_are_serialized_for_observability` | 退回记录进 `to_dict()`，面板/报告能看见 |
| `test_auto_fill_requires_kb_to_support_the_asserted_class` | KB 没出现过该类 → **不代查**（不许给断言背书） |

## 8. 未做（诚实清单）

- **S5 quote 交叉核对**：现在只查非空，占位符能过。要按「quote 的 n-gram 是否出现在
  对应 fact.value 里」做，先以 warning 形式跑一段评测再升级成 error。
- **修正轮历史不落库**：`repairs` 只在内存 Outcome 里，resume 后看不到「上次被退回过」。
  要落库得给 `runs` 表加一列 JSON，属于 F06 的 schema 变更。
- **补齐只覆盖 history 缺口**：没有泛化成「按缺失来源表补齐」。真要做成表驱动，
  得先有第二个需要跨源的场景（目前只有 kb+history）。
