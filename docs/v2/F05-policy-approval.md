# F05 权限网关与审批（HITL）

迭代：I2 ✅ 已实现 ｜ 依赖：F03 F06 ｜ 代码：`oncall/policy.py` + `oncall/store.py` + `oncall/api.py`

**实现状态（I2）**：审批落 SQLite（`approvals` 表，who/status/decided_at 留痕）；挂起即返回 exit 2；超时默认拒绝（`approval_timeout_s`，默认 2h，测试可设 0）；destructive 需 `requires=2` 双人确认（单人 approve 只记 1/2）；幂等键 `applied_keys` 拦住重复执行；CLI `oncall approve <run_id> --decision allow|deny [--by 审批人]`。状态机四条边各有单测（tests/v2/test_i2.py）。**未做**：exec_failed→failed 重试边、IM 审批通道（`ApprovalChannel` 抽象留给 I3+）。

## 1. 目标

让「模型想做的」和「实际能做的」之间有一道**可审计、可恢复、默认拒绝**的闸门。

## 2. 三类动作，三条路径

```
                  ┌─ READ ──────────────→ 执行（记 span）
model.intent ─────┼─ WRITE ─────────────→ 挂起 → 等人批 → 执行 / 拒绝
                  └─ DESTRUCTIVE ───────→ 挂起 + 双人确认（I1 阶段同样挂起但不落库）
```

叠加两条 v1 已有的判据（不放松，只更严）：
- 工具必须在白名单（`registry.get` 为 None → 拒绝）
- 参数必须过 schema
- 模型自报置信度 < 0.7 → 不执行（与 v1 置信度门禁同口径）

## 3. 审批的状态机（这是与 v1「转人工落库」最大的区别）

```
pending ──approve──→ approved ──→ executing ──→ done
   │                    │
   ├──deny──→ denied    └──exec_failed──→ failed（可重试一次）
   └──timeout(2h)──→ expired（默认拒绝，不是默认允许）
```

**四条设计约束**：

| 约束 | 为什么 |
|---|---|
| 状态落 SQLite，不在内存 | 人可能 2 小时后才批；进程重启不能丢（F06 同一张表） |
| 不阻塞连接 | 挂起即返回 `status=pending_approval`，CLI 退出码 2，`oncall approve` 再续 |
| 超时默认拒绝 | 默认允许会让「没人管」变成「全放行」 |
| 记 who / what / when / why | `why` 用模型给的理由原文，这是事后复盘的唯一线索 |

## 4. 审批里必须给人看的三样东西

1. **动作**：`restart_service(service="ssrm-api", env="prod")`——参数展开，不给人看函数名
2. **影响面**：影响 N 个供应商 / 1 个服务 / 只读无影响（能自动算的必须自动算）
3. **依据**：模型为什么认为该做（引用事实 + ref），人快速判断对不对

只给一个「是否同意」按钮而不给这三样，等于把责任推给人却不给人信息。

## 5. 幂等与重放

- 写操作必须带 `idempotency_key`（默认 `run_id + tool + args_hash`）
- 执行前查 `applied_keys` 表：已执行 → 直接返回上次结果，**不重复调用**
- 重放（F06 resume）时：`READ` 可重跑；`WRITE + idempotent` 可重跑（靠上面这层）；`DESTRUCTIVE` 一律重新审批

**为什么这条是硬要求**：checkpoint 的恢复点不等于业务幂等点。「已发通知、未落库」这种中间态崩溃后重放，没有幂等键就会重复通知供应商——这是线上事故的经典形态。

## 6. 取舍

- **不做「一次授权、本 run 内免审」**：看起来方便，实际上把一个 run 的权限放大成「模型自己决定后续所有动作」，风险不可控。
- **不做自动降级为只读继续跑**：写操作被拒后，正确行为是把「建议动作 + 待人工」作为结论输出，而不是偷偷换个只读动作假装完成。
- **不做图形化审批**：CLI 的 `approve/deny` 够用；真上生产应接 IM 审批（企业微信/飞书），接口留 `ApprovalChannel` 抽象即可。

## 7. 验收

1. 危险动作集（5 条，含 `restart_service`）：**直接执行 0 次**，全部 `pending_approval`
2. `approve` 后能续跑并完成剩余步骤；`deny` 后结论为「建议动作 + 未执行」
3. 断点重放不重复副作用：同 `idempotency_key` 调 3 次，实际 handler 只被调用 1 次
4. 超时用例：把超时设为 1s，断言 `expired` 且未执行
5. 置信度 0.5 的写操作意图：不进入审批，直接拒绝并说明理由

## 8. 面试一句话

「审批不是一个按钮，是一个状态机——挂起要能持久化、超时要默认拒绝、重放要幂等。这三点里少任何一个，『人工确认』在生产上就等于没有。」
