# F09 MCP server（把能力暴露出去）

迭代：I3 ｜ 依赖：F03 ｜ 代码：`oncall/mcp/server.py`

## 1. 目标

把 SSRM 的排障能力（查工单、读日志、查指标）**暴露成标准 MCP 工具**，让 Claude Code / Cursor / 别的 agent 都能直接用，而不是只服务自家 CLI。

## 2. 为什么值得做（不只是为了简历）

- **复用**：同一个 server 可被 IDE、聊天客户端、值班机器人共用；集成从「一次性 glue code」变成「有 owner、有版本、有测试的资产」
- **验证工具设计**：能被外部 host 用起来，说明工具描述真的写清楚了（自己用会「脑补」，别人用不会）
- **成本极低**：`ToolRegistry` 的形状已经是 `tools/list` + `tools/call`，适配器约 60 行

## 3. 暴露什么（默认最小）

| 工具 | 暴露 | 理由 |
|---|---|---|
| `query_incidents` | 是 | 只读，同簇历史，价值最高 |
| `read_logs` | 是（带条数上限） | 只读，注意返回体积 |
| `query_metrics` | 是 | 只读，返回聚合值不返原始序列 |
| `classify_incident` | 是 | 只读、确定性，适合给别的 agent 当子程序 |
| `restart_service` | **否** | destructive，永不暴露 |
| `add_ticket_comment` | 否（默认） | 写；如要暴露必须加 OAuth + 审计 |

**原则**：server 只暴露「读」，写操作留在 CLI 本体里走审批。开放写能力需要一整套身份与审计，不是这次的范围。

## 4. 实现要点

```python
# 两个 handler 就够
async def list_tools():  return [t.to_mcp_schema() for t in registry.specs() if t.side_effect == READ]
async def call_tool(name, args):
    err = registry.validate_args(registry.get(name), args)   # 复用 v1 schema 校验
    if err: return is_error(err)
    return registry.get(name).handler(**args)
```

三个细节：
1. **返回值必须裁剪**：MCP 客户端会把整个结果塞进模型上下文，一次 `read_logs` 返回 2MB 会直接把对方的窗口打爆。服务端截断 + `full_ref`
2. **错误用 `isError` 而不是异常**：客户端能把错误给模型看，模型才有机会改参数
3. **resources 暴露 KB**：`resources/list` 给 `kb://ssrm/{cluster}`，让客户端按需取（比全塞 tools 更省）

## 5. 安全边界（写在明处）

- transport 只用 **stdio**（本地进程，由 host 拉起），不开公网 HTTP
- 无鉴权：stdio 的信任来自「谁能启动这个进程」，这与「谁能执行本地命令」等价；开放远程必须上 OAuth 2.1 + PKCE（规范要求，含拒绝错误 audience 的 token）
- 只读 + 域白名单：启动参数 `--domain ssrm`，不暴露其他域的 KB

## 6. 取舍

- **不做 MCP Apps / Server Card**（2026 年的扩展）：对本项目没有收益，且规范还在演进。
- **不做「一个 server 装所有域」**：按域拆 server，便于授权和限流。
- **不为了演示暴露写工具**：演示价值抵不上风险，且会让「默认拒绝」这条设计原则显得不真诚。

## 7. 验收

1. `mcp inspector` 或 Claude Code 能 `list` 出 4 个工具并成功 `call`
2. 返回体积：`read_logs` 1000 行输入 → 输出 ≤【待实测】字节，含 `full_ref`
3. 非法参数：返回 `isError=true` 且文案可读（不是栈）
4. `restart_service` 不出现在 `tools/list` 里（断言）

## 8. 面试一句话

「写 server 比写 client 更能说明工具设计得好——别人用你的工具时不会脑补，描述含糊立刻就暴露。而且这 60 行是把内部能力资产化的最短路径。」
