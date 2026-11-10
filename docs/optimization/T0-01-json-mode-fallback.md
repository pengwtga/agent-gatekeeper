# T0-01 修复「宽松 JSON 降级」分支不可达

> 文件：`app/llm_client.py`　函数：`LLMClient._call_with_retry`
> 类型：正确性 Bug（容灾链少一跳 + 错误信息失真）

## 1. 问题

原实现：

```python
async def _call_with_retry(self, provider, system_prompt, user_payload):
    last_err = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            return await self._call(provider, system_prompt, user_payload, use_json_mode=True)
        except _JSONModeUnsupported:
            continue          # ← 问题在这里
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, LLMError) as e:
            last_err = e
    raise LLMError(str(last_err))
```

`use_json_mode=True` 是在**调用点硬编码**的，`continue` 只是进入下一轮循环，下一轮仍然带着
`response_format: {"type": "json_object"}` 发请求。所以实际链路是：

```
请求(带 response_format) → 400 → continue → 请求(还带 response_format) → 400 → 循环耗尽
```

两个后果：

1. **降级分支永远走不到**。对 `glm-4-flash` 这类不支持 `response_format` 的免费 Provider，
   实际行为是「直接失败，跳到下一家」，等于容灾链上少一跳 —— 而它本来是链上最不该被浪费的一跳。
2. `last_err` 在这个分支里从未赋值，全部走该分支时会抛 `LLMError("None")`，
   日志里只留一句 `None`，排查时完全无法定位。

## 2. 判断：这是状态机问题，不是重试问题

代码里其实有**两个正交的状态**，被压成了一个循环变量：

| 状态 | 语义 | 变化方向 | 受谁约束 |
|---|---|---|---|
| `use_json_mode` | Provider 的**能力** | 只能 `True → False`，单向 | 服务端能力，与调用次数无关 |
| `failures` | **失败次数** | 递增，有上限 | `llm_max_retries` |

把「能力不支持」当成「这次调用失败」来处理，就必然出现「用同一个错误参数重试到耗尽」。
一旦把两个计数器分开，逻辑就变成一个很短的 while。

顺带一个设计升级：**能力是进程级事实，应该粘住**。同一个 Provider 在这次进程生命周期内
已经证明不支持 `response_format`，后续每个请求都不需要再吃一次 400 —— 加一个
`_no_json_mode: set[str]` 即可，代价 3 行，省的是每个请求一次无效往返。

## 3. 方案取舍

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 双层 try | 外层先按 json 模式调，捕获 `_JSONModeUnsupported` 后整段重调一次 | 最短，但重试逻辑要写两遍，后续接超时预算时会重复两份，放弃 |
| **B. 单循环 + 双计数器** | `use_json_mode` 单向降级、`failures` 计数，降级不计入重试 | **采用**。状态显式、只有一处重试逻辑，T1-1 接预算时也只改一处 |
| C. 启动时探测能力 | 启动时对每个 Provider 发一次探测请求，记下能力 | 多一次请求与启动延迟；且 400 未必是 `response_format` 引起，探测结论不可靠，放弃 |

## 4. 改动

`app/llm_client.py`：

- `LLMClient.__init__` 增加 `self._no_json_mode: set[str]`
- `invalidate_chain()` 同时清空该集合（配置热更新后应重新探测）
- `_call_with_retry` 重写为双计数器循环
- `_call` 中 400 分支补一行日志（打响应片段，否则只看到 "400 Bad Request"）
- `_call` 中 `resp.raise_for_status()` 换成显式 `httpx.HTTPStatusError`，把服务端错误体带进异常

核心逻辑：

```python
use_json_mode = provider.name not in self._no_json_mode
failures = 0
last_err = None

while failures <= settings.llm_max_retries:
    try:
        return await self._call(provider, system_prompt, user_payload, use_json_mode=use_json_mode)
    except _JSONModeUnsupported:
        if not use_json_mode:      # 宽松模式还说不支持 → 不是这个原因，交给上层容灾
            raise
        use_json_mode = False      # 单向降级，不计入 failures
        self._no_json_mode.add(provider.name)
        continue
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        failures += 1
        last_err = e

raise LLMError(f"{provider.name}: {last_err if last_err is not None else '调用失败'}")
```

用 `HTTPStatusError` 而不是自定义 `LLMError` 的原因：`LLMError` 不在重试的异常元组里，
换成它会让 **5xx 变成不可重试**，行为悄悄变差。`HTTPStatusError` 保留「可重试」语义，
同时因为它的 message 我们拼了响应体，日志里能直接看到上游说了什么。

## 5. 验收

```python
# mock 一个 Provider：带 response_format 返回 400，不带则返回 ```json 围栏内容
client = LLMClient()
result = await client._call_with_retry(provider, "sys", "user")
```

实测结果：

```
PASS T0-1 调用次数=2（400 后降级重试一次）  calls=2
PASS T0-1 第二次请求已去掉 response_format
PASS T0-1 围栏 JSON 能解析  {'root_cause_class': 'ea_sync_timeout', 'confidence': 0.9}
粘住降级状态：首次 2 次请求，第二次 1 次请求（期望 2 / 1）
```

## 6. 边界与遗留

- **400 也可能不是 `response_format` 引起的**（模型名错、参数非法）。此时宽松模式再 400 会计入
  `failures` 并最终抛错跳到下一家 —— 行为仍然正确，只是多一次无效请求。
- **能力记忆是进程级的**：多 worker 部署时每个进程冷启动各吃一次 400。代价是几毫秒，不值得为此加共享存储。
- **未做**：按 Provider 拉 `/v1/models` 判断能力。免费档接口不保证返回 `response_format` 支持信息，不划算。
- `LLMError` 与 `LLMNotConfiguredError` 的语义保持原样，上层 `classify()` 的降级判断不受影响。

## 7. 面试讲法（30 秒）

> 这里原来的 bug 是把「能力降级」和「调用失败」当成同一件事，都塞进 `for attempt in range(...)`，
> 结果 `continue` 之后下一轮还带着不支持的参数，降级分支永远走不到，日志还只剩一句 `None`。
>
> 我的修法是把两个状态分开：能力是单向的（`True → False`），失败次数是有界的，两者不共用计数器；
> 并且把「这个 Provider 不支持 json_object」记在进程里，后面请求直接走宽松模式，不用每个请求先吃一次 400。
>
> 这类 bug 不是逻辑写错，是**状态没拆开**。后面接超时预算时也是同一个道理：预算和重试次数必须分开算。
