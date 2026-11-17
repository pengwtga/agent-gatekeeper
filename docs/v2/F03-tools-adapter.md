# F03 工具适配层（复用四道关 + 副作用分级）

迭代：I1 ｜ 依赖：无 ｜ 代码：`oncall/tools.py`

## 1. 目标

让 v1 的 `ToolRegistry` **不加改动**就能给 harness 用，并补上 v1 缺的一件事：**副作用分级**。

## 2. 现状与缺口

v1 的 `Tool` 只有 `read_only: bool`，执行策略是「`read_only=False` 一律拒绝」。这在固定流程里够用，在 harness 里不够：

| 缺口 | 后果 |
|---|---|
| 只有二值（只读 / 不可用） | 无法表达「可写但要审批」「危险但允许双人确认」 |
| 没有幂等声明 | F06 的断点重放无法判断「这一步能不能重做」 |
| 没有截断策略 | 一次 `kubectl describe` 可能几万 token，直接爆上下文 |
| 返回值形状不统一 | loop 里要写一堆 `if "error" in result` |

## 3. 设计

### 3.1 副作用等级

```python
class SideEffect(str, Enum):
    READ = "read"                # 只读，自动执行
    WRITE = "write"              # 有副作用，需审批（F05）
    DESTRUCTIVE = "destructive"  # 不可逆，默认禁止，需双人确认或配置放行
```

`idempotent: bool` 单独一维：**决定能否在 resume 时重放**（`WRITE`+`idempotent=True` 可重放；`DESTRUCTIVE` 永不重放）。

**落点（I1 实现时的决定）**：副作用声明**放在 harness 侧**（`oncall/tools.py` 的 `SIDE_EFFECTS` 表），不改 v1 的 `Tool`。理由：v1 的 `read_only` 表达的是「能不能自动执行」，v2 需要的是「执行策略的输入」——后者属于 harness，放在一起会让 v1 为 v2 的需求改接口。代价是「声明离实现远」，两条措施兜住：

1. **fail-closed**：`SIDE_EFFECTS` 没列的工具一律按最危险的 `WRITE` 处理——忘了声明的最坏结果是多问一次人，而不是悄悄放行写操作
2. **测试兜底**：`test_default_registry_tools_are_all_declared` 把「忘了声明」变成红的

### 3.2 适配器（薄，不重写）

```python
def adapt(reg: ToolRegistry) -> list[ToolSpec]:
    return [ToolSpec(
        name=t.name, description=t.description, params=t.params,
        side_effect=t.side_effect, idempotent=t.idempotent,
        handler=lambda **kw: reg.get(t.name).handler(**kw),
    ) for t in reg.specs()]
```

**为什么只做适配不重写**：`ToolRegistry.specs()/get()/validate_args()` 的形状已经是 `tools/list` + `tools/call`，重写等于把四道关的测试（白名单、schema、只读、置信度）全部重做一遍。

### 3.3 统一返回

```python
@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""            # 给模型看的人话，不是栈
    truncated: bool = False
    full_ref: str = ""         # 原文在哪（trace_id / 分页游标）
    ms: int = 0
```

`error` 必须是**模型能据此改动作**的话：`"参数缺少 incident_id"`，而不是 `KeyError: 'incident_id'`。

### 3.4 截断策略

> 800 字符：保留头 500 + 尾 200 + `…省略 N 字符，完整内容见 {full_ref}`。

头部通常有状态/时间，尾部通常有结论/异常。**中间大概率是重复堆栈**——这是经验判断，会在评测里验证「截断前后 pass 是否变化」。

## 4. 工具集（控制在 8 个以内）

| 工具 | 副作用 | 用途 |
|---|---|---|
| `query_incidents` | read | 同现象历史工单（v1 已有） |
| `read_logs` | read | 按 incident 拉日志片段 |
| `query_metrics` | read | 分位/错误率（v1 已有指标） |
| `classify_incident` | read | 调 v1 分类器（F02 §5） |
| `list_changes` | read | 最近发布/配置变更 |
| `get_ticket` | read | 外部工单状态 |
| `restart_service` | destructive | 演示危险动作（默认禁用） |
| `add_ticket_comment` | write, idempotent | 演示审批写（幂等键 = run_id + 内容 hash） |

**为什么限 8 个**：工具描述本身吃上下文。每个工具约 80~150 token，30 个工具就是 3k token 起步，还没开始干活。超过 8 个必须分组（按 server 前缀 + 按需 `list_tools`），这条在 F08 会再遇到一次。

## 5. 取舍

- **不做动态工具发现**（除 MCP）：本地工具集是静态的，动态发现只会让评测不可复现。
- **不做工具级重试**：重试属于 harness（F02 的下一轮），工具自己吞掉失败会让模型误以为成功。
- **不做参数自动修正**：schema 不过就返回错误让模型改，自动修正是把「人理解错了」静默改成「看起来合法的值」——和 KB 校验不做自动修复同一个理由。

## 6. 验收

1. 适配后的 `specs()` 与 v1 逐字段相等（一条断言，防手抄漂移）
2. 每个工具的 schema 校验有正反例（缺必填、类型错、多余参数）
3. 截断：造 5000 字符返回值，断言 `truncated=True`、长度 ≤ 800、`full_ref` 非空
4. `read` 工具执行路径上**不允许**出现审批分支（用桩断言 policy 未被调用）

## 7. 面试一句话

「工具层的四道关是 v1 就有的，v2 只补了一件事：把『只读/不可用』的二值改成三级副作用加幂等声明——因为断点重放必须知道哪一步能重做，而审批必须知道哪一步要问人。」
