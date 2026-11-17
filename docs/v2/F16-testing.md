# F16 入口分层与测试策略

迭代：贯穿 I1~I4 ｜ 代码：`oncall/api.py`、`oncall/__main__.py`、`tests/v2/`

## 1. 业务层不经过 CLI：一个核心，四个入口

**CLI 不是业务层，它是一个壳。** 真正的入口是一个函数：

```python
# oncall/api.py —— 唯一的核心入口，所有形态都调它
async def run_incident(goal: str, *, opts: RunOptions, deps: Deps) -> RunResult: ...
```

| 入口 | 谁用 | 怎么用 | 什么时候选 |
|---|---|---|---|
| **进程内调用** | 值班平台后端、自家 Python 服务 | `await run_incident(...)` | 同语言、同进程、要低延迟（推荐默认） |
| **子进程 CLI** | 跨语言服务、CI、运维脚本 | `oncall run --json`，读 stdout | 要崩溃隔离 / 权限降级 / 语言无关 |
| **MCP server** | 别的 agent、IDE、聊天客户端 | `tools/call query_incidents` | 只暴露只读能力时（F09） |
| **HTTP** | 前端页面、外部系统 | `POST /runs`（薄封装 FastAPI，复用 v1） | 要给浏览器用；**v2 先不做** |

**CLI 只做三件事**：解析 argv → 装配 `Deps` → 渲染输出。一行业务逻辑都不许写在里面。

```python
# oncall/__main__.py
def main(argv):
    args = parse(argv)
    deps = build_deps(args)                    # LLM 客户端 / registry / 审批通道 / 时钟
    result = asyncio.run(run_incident(args.goal, opts=args.opts, deps=deps))
    print(render(result, json_mode=args.json))
    return EXIT[result.status]                 # 0/2/3/4
```

**依赖必须注入，不许读全局**：预算、审批通道、时钟、LLM 客户端都从 `Deps` 进。理由很实际——业务方要替换审批通道（IM 审批 vs CLI 审批），测试要替换时钟和 LLM，读 `os.environ` 的写法两边都做不了。

## 2. 测试四层（按成本排）

| 层 | 测什么 | 替身 | 单次耗时 | 在 CI 里 |
|---|---|---|---|---|
| L1 单测 | loop 终止/打转、policy 分支、sensors、压缩、截断、幂等 | ScriptedLLM、FakeClock、CountingTool | ms | 每次 PR |
| L2 契约 | 输出 schema、退出码矩阵、MCP `tools/list` 快照 | 无（纯断言） | ms | 每次 PR |
| L3 e2e | 真进程跑 CLI：stdout、退出码、kill -9 后 resume | subprocess + `httpx.MockTransport` | 1~3s | 每次 PR（marker 可跳） |
| L4 评测 | 30 条 × 3 组，真模型 | 真 API | 分钟~小时 | nightly / 发布前 |

比例大致 80 / 10 / 8 / 2。**注意 L4 不进 PR**：90 次真调用既慢又花钱，还会因为模型波动让 CI 随机红。

## 3. 三个测试替身（写法决定测试有没有用）

### 3.1 ScriptedLLM：按步返回不同动作

最没用的桩是「永远返回同一句」——那样只能测单步。必须能**按步数脚本化**，才能测多步、终止、打转：

```python
class ScriptedLLM:
    """按顺序吐出预设响应；用完之后重复最后一个（模拟模型卡住）"""
    def __init__(self, script: list[dict]):
        self.script, self.i, self.calls = script, 0, 0

    async def chat_json(self, messages, **kw):
        self.calls += 1
        out = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return out

# 用法：第 1 步查工单，第 2 步又查一次同一张工单 → 触发无进展
llm = ScriptedLLM([
    {"action": "tool", "tool": "query_incidents", "args": {"q": "XF-885"}},
    {"action": "tool", "tool": "query_incidents", "args": {"q": "XF-885"}},
])
```

### 3.2 FakeClock：预算测试不能靠 sleep

`Budget` 用的是 `time.monotonic`，真等 90s 会把测试拖死。所以 loop 里的时间来源必须可注入：

```python
class FakeClock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def advance(self, ms): self.t += ms / 1000
```

`oncall/loop.py` 里所有取时间的地方走 `deps.clock.now()`。**这不是为了测试方便才加的抽象**——生产上它也是唯一能让「预算」在单测里被验证的办法。

### 3.3 CountingTool：断言「没被调用」

危险动作的测试，最有价值的断言不是「结果对不对」，而是「handler 一次都没被调」：

```python
class CountingTool:
    def __init__(self, ret=None): self.n, self.ret = 0, ret or {"ok": True}
    async def __call__(self, **kw):
        self.n += 1
        return self.ret

restart = CountingTool()
result = run_incident("把 ssrm-api 重启一下", deps=deps(restart=restart))
assert restart.n == 0                      # 关键断言
assert result.status == "needs_human"
assert result.pending_approval is not None
```

## 4. 六个必测用例（一个对应一类线上事故）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 模型重复调同一工具 | 第 3 步 abort，`status=needs_human`，`reason` 含「无进展」 |
| 2 | 预算 2s，任务需 10 步 | 停止后 `llm.calls` 不再增长；`status=budget_exhausted` |
| 3 | 要求重启生产服务 | `handler.n == 0`，`pending_approval` 非空，退出码 2 |
| 4 | 审批挂起后进程退出 | DB 里 `approvals.decision='pending'`，`runs.status='pending_approval'` |
| 5 | `kill -9` 后 `resume` | `steps` 连续；写工具 handler 累计只调 1 次 |
| 6 | 结论只有 1 个字段的证据 | 传感器 S2 拦截 → 回 loop 一次 → 仍不过则 `unverified` |

## 5. e2e 只测「进程边界」

子进程测试贵且脆，只用来测函数测不到的东西：**退出码、stdout 契约、信号处理、跨进程 resume**。

```python
def test_cli_returns_2_when_needs_human(tmp_path):
    env = {**os.environ, "ONCALL_LLM_BASE": "http://mock", "ONCALL_DB": str(tmp_path / "r.db")}
    p = subprocess.run([sys.executable, "-m", "oncall", "run", "--json", "--incident", "INC-1"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 2
    assert RunOut.parse_raw(p.stdout).status == "needs_human"
```

外部依赖用 `httpx.MockTransport` 挂在注入的 client 上——**不引入 respx/vcr 这类新依赖**，现有 httpx 就够。

## 6. 隔离纪律（不做这四条，测试会变成玄学）

1. **禁真实网络**：`tests/v2/conftest.py` 里给默认 client 挂一个「任何请求都失败」的 transport，谁忘了 mock 会立刻红，而不是悄悄打真 API 烧钱
2. **固定时间与顺序**：时钟注入（§3.2），工具结果固定，不用 `random` 不设 seed
3. **每个测试独立库**：`tmp_path` 下建 SQLite，不共享 `.pytest_data`（v1 有过共享目录互相污染的坑）
4. **慢测试打 marker**：`@pytest.mark.slow`，PR 里 `-m "not slow"`，nightly 全跑

## 7. 验收

1. `pytest -q -m "not slow"` 全绿，总时长 < 30s
2. `pytest -q -m slow` 覆盖 6 个用例中的 4、5（进程边界与 resume）
3. `pytest --collect-only` 里 L1 用例数 ≥ 25（少于这个数说明只测了 happy path）
4. 故意删掉「无进展检测」的实现，用例 1 必须红（变异测试，验证测试真的有效）

## 8. 面试一句话

「CLI 只是壳，核心是 `run_incident()` 一个函数、四个入口；测试分四层，90% 是零网络的单测——靠脚本化 LLM 和可注入时钟，把『打转』『超预算』『危险动作没执行』这些事故变成毫秒级断言。」
