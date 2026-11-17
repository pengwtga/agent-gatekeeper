# F06 Checkpoint / resume

迭代：I2 ✅ 已实现 ｜ 依赖：F02 ｜ 代码：`oncall/store.py` + `oncall/api.py`

**实现状态（I2）**：五张表齐全（runs/steps/facts/approvals/applied_keys）；每步执行后一个事务落 steps+facts（`RunStore.checkpoint`，PK(run_id,i) 保证 steps 无空洞）；`resume_incident()` 三种断点（审批挂起/预算耗尽/已批待执行）都从存储重建状态，无进展检测的 seen 一并重建——resume 后重复同样的查询照样拦；**不自动 resume**，必须显式 `oncall resume`。已验证：resume 后不重查已确认事实、步号连续、审批过期默认拒绝、同审批重放 handler 仅 1 次。**未做**：7 天归档清理（表量小，暂无必要）。

## 1. 目标

跑到一半断了（进程崩、审批等人、预算耗尽）能**接着跑**，且**重跑不重复副作用**。

## 2. 为什么需要（三个真实场景）

| 场景 | 没有 checkpoint 的后果 |
|---|---|
| 审批挂起 2 小时 | 进程一直挂着 / 重启后从头查一遍，白烧 20k token |
| 排障跑到第 9 步 OOM | 全丢，值班同学要重新触发一次 |
| 评测跑 30 条 | 一条失败要重跑整批，不可接受 |

## 3. 存储（复用 v1 的 SQLite + WAL 经验）

```sql
runs(run_id PK, incident_id, domain, status, goal, created_at, updated_at,
     usage_json, conclusion, pending_approval_id)
steps(run_id, i, thought, tool, args_json, ok, error, ms, ts)   -- 追加写
facts(run_id, field, value, ref, step_i)                        -- 结构化事实
approvals(id PK, run_id, tool, args_json, why, decision, decided_by, ts)
applied_keys(key PK, run_id, result_json)                       -- 幂等（F05）
```

**为什么不用 Temporal/LangGraph checkpointer**：
- Temporal 要额外服务，本项目单机部署；
- 框架的 checkpointer 语义与业务事务边界不对齐（恢复点 ≠ 幂等点），反而要写适配层；
- 这张表总共 5 张，SQLite 单文件 + WAL 就够（v1 的 `trace_store` 已踩过坑：0.11ms/条、7 天 5.6MB）。

## 4. 写入时机（少一步就丢数据）

每步**执行后**、下一步**装配前**写：`steps` + `facts` + `usage`，一个事务。

- 工具执行前写 `attempt`（防止「执行成功但没记上」导致重放重复执行）
- 事务提交后才允许下一步 → 崩溃最多丢一步，不会出现「step 记了但事实没记」

## 5. resume 的语义（三种断点，三种处理）

| 断点 | resume 行为 |
|---|---|
| 正常中断（步数/预算） | 从 `facts` 重建上下文，继续直到新预算耗尽 |
| 审批挂起 | 查 `approvals`：已批 → 执行该步；已拒 → 走拒绝分支；未决 → 仍挂起 |
| 进程崩溃 | 重放最后一步：`READ` 直接重跑；`WRITE+idempotent` 靠 `applied_keys` 兜；`DESTRUCTIVE` 重新审批 |

**铁律**：`DESTRUCTIVE` 永不自动重放。宁可让人再点一次，也不要重复重启生产服务。

## 6. 清理

- `status` 终态且超过 7 天的 run 归档到 `runs_archive`（或者直接删，本项目的定位是「刚才那条」）
- 参照 v1 `_PRUNE_EVERY=500` 的摊薄思路：每 500 次写入触发一次清理，不单独起定时任务

## 7. 取舍

- **不做事件溯源（event sourcing）**：状态表 + 步骤表已经够，事件溯源会让「当前状态」需要重放才能得到，调试成本反而高。
- **不做跨机恢复**：单机 SQLite，明确写在限制里。真要跨机应该走 Temporal，不是自己加复杂度。
- **不做自动 resume**：断点必须由人/CI 显式 `oncall resume`，避免「崩了又自动跑起来」把错误放大。

## 8. 验收

1. `kill -9` 后 `resume`：能续跑，`steps` 连续无空洞，结论与不中断版本一致
2. 重复 `resume` 两次：`applied_keys` 生效，写工具 handler 只被调用 1 次
3. 挂起 → 审批 → resume：状态机四条边（approve/deny/timeout/exec_failed）各一例
4. 崩溃在「执行成功、未写 step」：resume 后不出现同一步执行两次（用计数器桩断言）

## 9. 面试一句话

「checkpoint 的价值不在于『能续跑』，而在于『续跑时不重复副作用』——所以恢复点必须和幂等键、审批状态绑在一起设计，这三样拆开做就会漏。」
