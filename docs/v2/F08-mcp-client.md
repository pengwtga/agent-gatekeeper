# F08 MCP client（接远程工具）

迭代：I3 ｜ 依赖：F03 F05 ｜ 代码：`oncall/mcp/client.py`

## 1. 目标

不写一行集成代码，就能用上别人维护的工具（filesystem / postgres / prometheus / 内部系统），并且**仍然过 F05 的闸门**。

## 2. 接什么、怎么接

| 场景 | transport | 例子 |
|---|---|---|
| 本地工具进程 | stdio | `mcp-server-filesystem`、`mcp-server-git` |
| 远程服务 | Streamable HTTP | 内部 prometheus 网关（带 OAuth） |

启动流程：`initialize` → `tools/list`（缓存）→ 逐工具转换 schema → 注册进 harness（带来源标记）。

## 3. 命名与冲突

统一前缀：`mcp__<server>__<tool>`（例：`mcp__postgres__query`）。

**为什么必须加前缀**：两个 server 都可能有 `query`；不加前缀在注册表里会互相覆盖，且 trace 里看不出是谁。前缀也让 F05 能按 server 粒度配策略（`mcp__filesystem__*` 只允许写工作区）。

## 4. 外部声明不可信（这是 F08 最重要的一条）

MCP 规范里 tool annotations（read-only / destructive / idempotent）是 **server 自述**，规范明确要求客户端**不要默认相信**。所以：

- 默认：**所有外部工具按 `WRITE` 处理**（最保守）
- 白名单配置：`trusted_servers: {filesystem: {default: READ, allow_roots: [$PWD]}}`
- 即使标了 `readOnly`，也仍走 F03 的白名单 + schema 校验

**面试可讲**：这不是对协议不信任，是**信任边界**问题——协议只保证「格式对」，不保证「行为对」。host 侧必须再判一次，否则一个恶意/写错的 server 就能让 agent 直接删库。

## 5. 工具爆炸（真实存在的第二个瓶颈）

30 个工具的 description 约 3k token，还没干活就吃掉 1/4 上下文。三条对策，按优先级：

1. **按需发现**：初始只加载 `search_tools` + 每 server 的 2~3 个高频工具，其余通过 `list_tools(server, query)` 动态拉
2. **描述瘦身**：只保留第一句（动作 + 对象），长说明进 `resources` 按需取
3. **分组开关**：`--enable-mcp prometheus,filesystem`，评测环境只开必需项（保证可复现）

## 6. 失败与降级

| 情况 | 行为 |
|---|---|
| server 启动失败 | 记 `degraded`，继续跑（本地工具仍可用），结论里注明「少一路数据」 |
| `tools/call` 超时 | 记一次失败，不自动重试（重试交给 loop 决定） |
| schema 不兼容（新版本改字段） | 启动时校验失败 → 拒绝注册该工具并告警，**不静默降级** |

## 7. 取舍

- **不做 MCP 全部扩展**：Tasks（长任务）、Skills over MCP、Server Cards 只在文档里记一笔，不实现——本项目不需要，实现了也无法评测。
- **不做自动重连**：单次运行分钟级，重连收益低于复杂度。
- **不做工具结果缓存**：缓存会让评测不可复现；真需要应放在 server 侧。

## 8. 验收

1. 接本地 filesystem server：`tools/list` 成功、能读文件、写操作被 F05 拦住
2. 接一个只读 postgres：`SELECT` 能跑；`DELETE` 被 policy 拒绝（即使 server 声称 read-only）
3. 工具名冲突：两个 server 都有 `query`，注册后无覆盖（断言长度 = 2）
4. 工具爆炸：加载 30 个工具时，装配后的工具段 token ≤【待实测】阈值

## 9. 面试一句话

「MCP 解决的是『接得上』，不解决『能不能信』——它的 annotations 是 server 自述，规范自己都说别默认信。所以我把外部工具默认按最危险的等级处理，白名单才降级。」
