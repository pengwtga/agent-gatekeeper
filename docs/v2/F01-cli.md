# F01 CLI 入口与输出契约

迭代：I1 ｜ 依赖：无 ｜ 代码：`oncall/__main__.py`

## 1. 目标

一条命令完成一次排障，**输出可被机器消费**（CI、脚本、评测），退出码可被判断。

## 2. 为什么是 CLI 而不是 Web

| 理由 | 展开 |
|---|---|
| headless 才能评测 | 评测要跑 30×3 组 = 90 次，每次都是同一条命令；Web 要写客户端、要处理会话、要截图 |
| shell 就是工具集 | 模型可以直接用 `kubectl`/`gh`/`psql`，不必为每个系统包函数 |
| 可组合 | `oncall ask "…" --json \| jq .conclusion`，能进 cron、能进 CI |
| 权限边界清楚 | 跑在用户自己的容器里，工作区就是沙箱边界 |

v1 的 Web 不废：`/assist`、`/metrics` 继续给值班同学用。**CLI 是给「执行」用的，Web 是给「看」用的。**

## 3. 命令面

```
oncall ask "XF-885 供应商注册卡单了"          # 一句话进, 结论出
oncall run --incident INC-2026-1117 --json     # 指定事故, 结构化输出
oncall resume <run_id>                         # 从 checkpoint 续跑
oncall approve <run_id> --decision allow|deny  # 处理挂起的写操作
oncall mcp serve                               # 作为 MCP server 被外部 host 调用
```

全局参数：`--max-steps 12`、`--budget-ms 90000`、`--json`、`--verbose`、`--domain ssrm`。

## 4. 输出契约（**这是 bench 与面板共同依赖的东西**）

```json
{
  "run_id": "r-20261117-203012-a91f",
  "status": "ok | needs_human | budget_exhausted | failed",
  "conclusion": "EA 侧注册任务未创建：xflow 回调 404，回调地址仍是旧域名",
  "root_cause_class": "callback_lost",
  "evidence": [
    {"field": "log", "ref": "trace:...", "quote": "POST /supplier/created 404"},
    {"field": "config", "ref": "cfg:xflow.callback_url", "quote": "http://old-ssrm/..."}
  ],
  "steps": [{"i": 1, "tool": "query_incidents", "args": {...}, "ok": true, "ms": 320}],
  "usage": {"steps": 5, "prompt_tokens": 8123, "completion_tokens": 940, "wall_ms": 21300},
  "pending_approval": null
}
```

三条硬约束：`conclusion` 必须非空；`evidence` 至少 2 个**不同 field**（复用 v1 的字段去重口径）；`usage` 必须真实（不许估算）。

## 5. 退出码

| 码 | 含义 | 谁用 |
|---|---|---|
| 0 | 结论可用 | CI / 脚本 |
| 2 | 需人工（证据不足 / 低置信 / 审批未决） | CI 判「没有结论」而不是「失败」 |
| 3 | 预算或步数耗尽 | 监控告警 |
| 4 | 配置错误（Key 缺失、KB 非法） | 启动自检 |

**为什么把「需人工」和「失败」分开**：这是产品语义。排障 agent 说「我不知道，叫人」是**正确行为**，不该被算成故障；混在一起会让成功率指标失真（这也正是 v1 把 `not_configured` 和 `provider_error` 分开记的原因）。

## 6. 取舍

- **不做交互式 REPL**：多轮追问属于 `/assist`（F12）。CLI 只做「一次运行」，因为可复现优于好用。
- **不做 TUI**：彩色输出 + `--json` 够了。TUI 是纯成本。
- **不做 daemon**：常驻会引入「谁触发、谁清理」的 loop engineering 问题，I4 再说。

## 7. 验收

1. 五个子命令 e2e 各一条测试（mock LLM，零网络）
2. `--json` 输出可被 `json.loads` 且符合上面的 schema（用 pydantic 校验）
3. 退出码矩阵：ok→0 / needs_human→2 / budget→3 / 配置错→4
4. `oncall ask` 在无 Key 环境下给出退出码 4 与明确提示，而不是栈回溯

## 8. 面试一句话

「我把 agent 做成 CLI，不是为了酷——是为了让 90 次评测跑同一条命令，并且让退出码本身携带产品语义：不知道就返回 2，而不是假装知道返回 0。」
