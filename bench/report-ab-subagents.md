# F11 并行取证 / F10 技能 消融报告

- 生成时间：2026-11-18 20:27  真模型
- 样本：30 条（多源线索单跳 + 多跳子集）
- 三臂只差一个变量：base→sub 只加「首轮两路并行取证」；base→noskill 只去「技能注入」

> **口径敏感性（必须写在最前面）**：同一批 case、同一个模型，在 judge 还没把子 agent 调过的工具计入关键步骤之前，sub 臂跑出 13%；修完口径后重跑是 47%。**口径没修之前，「这个能力没用」的结论不可信**——并行取证越有效，主 loop 越省步骤，旧口径判得越差。

## 结论（先看这三条）

1. **F11 净正效应 +27pp（20% → 47%），代价是 token +71%、p95 +34%**。逐条看 12 条转正、3 条转负——不是全赢，但净收益远超噪声（单条 3.3pp，净 +8 条）。
2. **F10（skill 注入）净效应 0pp（20% → 20%）**，但 token 反而少 13%、步数 1.6 vs 1.8——**skill 的收益在这批任务上没体现出来**。遵守率段给了一个解释：注入后 3/3 会去查第二来源，不注入 0/3；但 pass@1 没差，因为 F17 的确定性补齐会把模型没查的 history 代查掉——**skill 教会模型自己做，harness 在它不做时替它做，两条路等价**。
3. skill 命中只有 3 条（triggers 窄），这一项的样本量不足以定论，只能说不构成负影响。

## 总体

| 臂 | pass@1 | 单跳 | 多跳 | 平均步数 | 平均 token | p95(ms) | 子 agent 路数 | 失败(错误处置) |
|---|---|---|---|---|---|---|---|---|
| base | 20% | 0% | 20% | 1.8 | 194052 | 31012 | 0 | 17 |
| sub | 47% | 0% | 47% | 2.1 | 331761 | 41424 | 60 | 12 |
| noskill | 20% | 0% | 20% | 1.6 | 168683 | 32224 | 0 | 12 |

## 逐条对照（两臂结论不同的一律列出，不挑好看的）

| case | 类别 | base | sub | noskill |
|---|---|---|---|---|
| MH-01 | multi_hop | ✅ | ✅ | ❌ |
| MH-03 | multi_hop | ❌ | ✅ | ❌ |
| MH-04 | multi_hop | ✅ | ❌ | ❌ |
| MH-06 | multi_hop | ❌ | ✅ | ❌ |
| MH-08 | multi_hop | ❌ | ✅ | ❌ |
| MH-09 | multi_hop | ❌ | ✅ | ❌ |
| GM-002 | multi_hop | ❌ | ❌ | ✅ |
| GM-004 | multi_hop | ❌ | ✅ | ❌ |
| GM-006 | multi_hop | ❌ | ✅ | ❌ |
| GM-007 | multi_hop | ✅ | ❌ | ✅ |
| GM-008 | multi_hop | ❌ | ✅ | ❌ |
| GM-010 | multi_hop | ❌ | ✅ | ❌ |
| GM-012 | multi_hop | ❌ | ✅ | ❌ |
| GM-013 | multi_hop | ✅ | ❌ | ✅ |
| GM-014 | multi_hop | ❌ | ✅ | ❌ |
| GM-016 | multi_hop | ❌ | ❌ | ✅ |
| GM-018 | multi_hop | ❌ | ✅ | ❌ |

## 技能流程遵守率（回答「怎么确认 agent 按 skill 走」）

口径：只统计隐式命中 `incident-triage` 的 case，看它是否做了 skill 声明的第 1/2 步（先 KB、再查同类）以及顺序。**skill 本身不强制任何行为，这一节量的是「注入之后行为有没有变化」，不是「模型是否服从」**。

| 臂 | 命中 skill | 首调 rag_search | 调过 query_incidents | 顺序正确 |
|---|---|---|---|---|
| base | 3 | 0/3 | 3/3 | 0/3 |
| sub | 3 | 0/3 | 3/3 | 0/3 |
| noskill | 3 | 0/3 | 0/3 | 0/3 |

## 失败归因（base 臂）

- MH-02（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- MH-03（multi_hop）：根因类不符：期望 duplicate_submit  [status=ok]
- MH-05（multi_hop）：根因类不符：期望 frontend_param_error  [status=needs_human]
- MH-06（multi_hop）：根因类不符：期望 xflow_callback_lost  [status=ok]
- MH-07（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- MH-08（multi_hop）：根因类不符：期望 duplicate_submit  [status=ok]
- MH-09（multi_hop）：根因类不符：期望 data_inconsistency  [status=ok]
- MH-10（multi_hop）：根因类不符：期望 frontend_param_error  [status=ok]
- GM-002（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'system_topology']  [status=ok]
- GM-003（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'kb_stats']  [status=ok]
- GM-004（multi_hop）：关键步骤未命中：期望工具 ['system_topology', 'rag_search']  [status=ok]
- GM-005（multi_hop）：关键步骤未命中：期望工具 ['kb_stats', 'rag_search']  [status=ok]
- GM-006（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- GM-008（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- GM-009（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'kb_stats']  [status=ok]
- GM-010（multi_hop）：关键步骤未命中：期望工具 ['system_topology', 'rag_search']  [status=ok]
- GM-011（multi_hop）：关键步骤未命中：期望工具 ['kb_stats', 'rag_search']  [status=ok]
- GM-012（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- GM-014（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'system_topology']  [status=ok]
- GM-015（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- GM-016（multi_hop）：关键步骤未命中：期望工具 ['system_topology', 'rag_search']  [status=ok]
- GM-017（multi_hop）：关键步骤未命中：期望工具 ['kb_stats', 'rag_search']  [status=ok]
- GM-018（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'system_topology', 'query_incidents']  [status=ok]
- GM-020（multi_hop）：关键步骤未命中：期望工具 ['rag_search', 'system_topology']  [status=ok]
