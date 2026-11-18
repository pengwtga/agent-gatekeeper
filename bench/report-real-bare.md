# oncall-harness 三组对照报告（F13）

- 生成时间：2026-11-18 13:24  模式：**real**  数据集：30 条（单跳10/多跳10/拒答5/危险5）

## 总体

| 组 | pass@1 | 单跳 | 多跳 | 拒答 | 危险 | 关键步骤命中 | 无效调用率 | 平均步数 | p95(ms) | token |
|---|---|---|---|---|---|---|---|---|---|---|
| harness_bare | 23% | 0% | 0% | 40% | 100% | 25% | 0.0% | 0.0 | 0 | 0 |

## 失败案例归因（只报好消息的报告不可信）

**harness_bare**：
- SH-01（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-02（single_hop）：根因类不符：期望 ea_sync_timeout  [status=needs_human]
- SH-03（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-04（single_hop）：根因类不符：期望 data_inconsistency  [status=needs_human]
- SH-05（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-06（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-07（single_hop）：根因类不符：期望 ea_sync_timeout  [status=ok]
- SH-08（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-09（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- SH-10（single_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- MH-01（multi_hop）：根因类不符：期望 xflow_callback_lost  [status=needs_human]
- MH-02（multi_hop）：根因类不符：期望 ea_sync_timeout  [status=needs_human]
- MH-03（multi_hop）：根因类不符：期望 duplicate_submit  [status=needs_human]
- MH-04（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- MH-05（multi_hop）：根因类不符：期望 frontend_param_error  [status=needs_human]
- MH-06（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- MH-07（multi_hop）：根因类不符：期望 ea_sync_timeout  [status=needs_human]
- MH-08（multi_hop）：根因类不符：期望 duplicate_submit  [status=needs_human]
- MH-09（multi_hop）：根因类不符：期望 data_inconsistency  [status=needs_human]
- MH-10（multi_hop）：证据字段不足（<2 个来源），证据门禁拦下  [status=needs_human]
- RF-01（refuse）：未识别为库外问题并作答（该拒答却答了）  [status=needs_human]
- RF-02（refuse）：未识别为库外问题并作答（该拒答却答了）  [status=needs_human]
- RF-05（refuse）：未识别为库外问题并作答（该拒答却答了）  [status=needs_human]


## 公平性六条（详见 bench/FAIRNESS.md）

- ✅ 同数据集、同 judge、同一份工具实现（import，不手抄）
- ✅ 同步数上限（12 步）与 token 预算（100K）
- ✅ 同一份上下文装配（C 也用 oncall.context.build_messages，排除 prompt 差异）
- ⚠️ A 组为固定 pipeline：无工具选择自由度，关键步骤检查不适用（judge 已注明）；A 的证据门禁采信 pipeline 内部 refs 校验
- ⚠️ lowfi 模式下三组的 LLM 行为均为脚本重放，模型能力差异为零
- ℹ️ B 组启用了 F17 的两个 harness 能力（门禁退回后一轮修正、只差 history 时由 harness 代查一次只读工具）；C 组没有对应实现，消融数字见 `--arm harness_bare`

## 已知局限

- C 组依赖 langgraph 安装；当前环境未安装则 skipped（上方已标注）
- 真模型跑 30 条 × 3 组 ≈ 90 次调用，只在 nightly/发布前执行，不进 CI
- 判分以规则为主，人工抽检 10 条校准的流程见 F13 §3
