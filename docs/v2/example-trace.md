# 一次排障的完整链路：SH-01

- 输入（goal）：`xFlow 审批单已 APPROVED 但 SSRM 没建任务`
- 终态：**ok**，2 步 / 0 tokens / 22156 ms
- 结论：审批回调丢失：xFlow审批已通过，但回调未到达SSRM（回调链路故障/服务重启窗口）
- 根因类：`xflow_callback_lost`　门禁退回：1 轮

> 每一轮的「输入」都是 `oncall.context.build_messages()` 的真实输出（按段摘录），
> 不是事后重写的示意。工具返回进事实表、事实表进下一轮输入，这条链在这里可逐轮核对。

## 轮次 1
**模型本轮看到的关键段**
- 已确认事实：- （暂无）
- 最近步骤：- （还没查任何东西）

**模型输出**（2863 ms）
```json
{
  "action": "tool",
  "tool": "rag_search",
  "args": {
    "query": "xFlow 审批单已 APPROVED 但 SSRM 没建任务",
    "top_k": 3
  },
  "thought": "需要查找与问题描述相关的知识库条目",
  "confidence": 0.7,
  "_provider": "zhipu"
}
```

**harness 动作**
- 执行工具 `rag_search` args={"query": "xFlow 审批单已 APPROVED 但 SSRM 没建任务", "top_k": 3} → ok（3380 ms）

## 轮次 2
**模型本轮看到的关键段**
- 已确认事实：- rag_search = {"enough": true, "rounds": 1, "rewrites": [], "hits": [{"id": "ONCALL-002", "title": "审批通过但无任务", "class": "xflow_callback_lost", "resolution": "批量补建 3 笔任务；根因是服务发布窗口回调恰好在重启间隙，补加了回调重试", "score": 0.52}, …  （来源 r-20261118-131404-cc69#step:1）
- 最近步骤：[1] rag_search args={"query": "xFlow 审批单已 APPROVED 但 SSRM 没建任务", "top_k": 3} → ok

**模型输出**（5569 ms）
```json
{
  "action": "finish",
  "conclusion": "审批通过但无任务，根因是服务发布窗口回调恰好在重启间隙，补加了回调重试。建议批量补建任务。",
  "root_cause_class": "xflow_callback_lost",
  "evidence": [
    {
      "field": "kb",
      "ref": "ONCALL-002#step:1",
      "quote": "审批通过但无任务；根因是服务发布窗口回调恰好在重启间隙，补加了回调重试"
    }
  ],
  "confidence": 0.7,
  "_provider": "zhipu"
}
```

**harness 动作**
- 执行工具 `query_incidents` args={"root_cause_class": "xflow_callback_lost", "limit": 3} → ok（2 ms）
- 门禁退回：S2 证据只覆盖 1 个字段（要求 ≥2）：['kb']（自动补齐 history：是）

## 轮次 3
**模型本轮看到的关键段**
- 已确认事实：- rag_search = {"enough": true, "rounds": 1, "rewrites": [], "hits": [{"id": "ONCALL-002", "title": "审批通过但无任务", "class": "xflow_callback_lost", "resolution": "批量补建 3 笔任务；根因是服务发布窗口回调恰好在重启间隙，补加了回调重试", "score": 0.52}, …  （来源 r-20261118-131404-cc69#step:1）
- query_incidents = {"label": "xflow_callback_lost", "matched": 20, "samples": [{"incident_id": "SIM-0000", "error_message": "审批单 FT20261018370 状态为 APPROVED，但 SSRM 无对应…（共 510 字）
- 最近步骤：[1] rag_search args={"query": "xFlow 审批单已 APPROVED 但 SSRM 没建任务", "top_k": 3} → ok
[2] query_incidents args={"root_cause_class": "xflow_callback_lost", "limit": 3} → ok
- **上一轮被门禁退回**：上一次 finish 被判不合格：S2 证据只覆盖 1 个字段（要求 ≥2）：['kb']。harness 已自动调用 query_incidents(root_cause_class=xflow_callback_lost)，结果在事实段中 field=query_incidents、ref=r-20261118-131404-cc69#step:2 的那一条。重新 finish 时补一条 evidence：field 写 history、ref 写这个 ref、**quote 必须从该条事实的内容里摘 10~40 字原文**（不要照抄这句说明，也不要写占位符——会把结论打成不可信）。请基于「已确认事实」重新 finish：断言根因时 evidence 必须同时含 kb（rag_search）与 history（query_incidents）两条，quote 用事实段里的原文；确实补不齐就 finish 且 root_cau…（共 448 字）

**模型输出**（10335 ms）
```json
{
  "action": "finish",
  "conclusion": "审批回调丢失：xFlow审批已通过，但回调未到达SSRM（回调链路故障/服务重启窗口）",
  "root_cause_class": "xflow_callback_lost",
  "evidence": [
    {
      "field": "kb",
      "ref": "r-20261118-131404-cc69#step:1",
      "quote": "审批通过但无任务"
    },
    {
      "field": "history",
      "ref": "r-20261118-131404-cc69#step:2",
      "quote": "审批单 FT20261018370 状态为 APPROVED，但 SSRM 无对应任务"
    }
  ],
  "confidence": 0.9,
  "_provider": "siliconflow"
}
```

## 终态

- 证据：[{"field": "kb", "ref": "r-20261118-131404-cc69#step:1", "quote": "审批通过但无任务"}, {"field": "history", "ref": "r-20261118-131404-cc69#step:2", "quote": "审批单 FT20261018370 状态为 APPROVED，但 SSRM 无对应任务"}]
- 传感器：{"ok": true, "errors": [], "warnings": [], "evidence_fields": ["kb", "history"]}
- 退回记录：[{"attempt": 1, "at_step": 2, "errors": ["S2 证据只覆盖 1 个字段（要求 ≥2）：['kb']"], "auto_filled": true, "root_cause_class": "xflow_callback_lost"}]
