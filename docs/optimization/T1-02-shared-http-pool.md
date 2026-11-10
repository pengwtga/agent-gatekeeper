# T1-2 httpx 共享连接池 + 三段超时

## 1. 问题

原实现每次调用都 `async with httpx.AsyncClient(...)`：

1. **无连接复用**：每次请求完整走一遍 DNS → TCP → TLS 握手。对 LLM API 这类
   同一个 host 的高频调用，TLS 握手占总延迟 30%+（实测视网络 100~300ms）
2. **单一 `timeout=30` 无法区分两类失败**：
   - **connect 慢/不通** → provider 挂了或网络问题，应该**立即切下一家**
   - **read 慢** → 模型在生成，值得等
   两类失败混在一个 30s 里：provider 挂掉时要白等 30s 才容灾。

## 2. 判断

连接池是「进程级资源」，不是「调用级资源」。`AsyncClient` 官方文档明确建议
单进程共享一个实例。三段超时的本质是把「故障」和「慢」分类——**故障要快败，
慢要按预算等**（见 T1-1）。

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ `@property client` 惰性单例 + `aclose()` | 改动最小；惰性避免 import 期建连接；测试可直接注入 mock client |
| ❌ 模块级 `client = AsyncClient()` | import 期创建事件循环绑定问题；测试难替换 |
| ❌ 每请求新建（原实现） | 就是问题本身 |

## 4. 改动

```python
# llm_client.LLMClient / embeddings.EmbeddingClient
timeout=httpx.Timeout(connect=3.0, read=…, write=10.0, pool=2.0)
limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
```

- `connect=3.0`：连不上 = provider 挂了，快速失败切下一家
- `read`：默认兜底 `llm_timeout_seconds`（离线评测用）；线上带预算的调用按
  T1-1 的切片在每次请求时覆盖
- `pool=2.0`：等空闲连接的最长时间，防止并发打满连接池时无限排队
- `main.lifespan` 退出时 `await llm_client.aclose()` + `await rag.aclose()`，
  避免悬挂连接拖慢优雅退出（与 T1-5 配套）

## 5. 验收

```
3.1 多次调用共享同一连接池（client 身份相同）   PASS
3.2 aclose 后 _client 置空                      PASS
```

连接复用的量化验证（`ss -tn state established | grep :443 | wc -l` 在压测下
远小于请求数）需要真实外网 API key，列入 T2 上观测后补。

## 6. 边界

- `max_keepalive_connections=20`：免费档 API 可能主动断开长连接，httpx 会自动重建，无需处理
- 连接池是进程内的：多 worker 部署时每个 worker 各一份（内存换延迟，可接受）
- 优雅退出强依赖 lifespan 被执行：`docker stop` 发 SIGTERM → uvicorn 触发 lifespan shutdown

## 7. 面试讲法

> 我把「连不上」和「算得慢」拆成两个超时：connect 3 秒快速失败切容灾链，
> read 按预算切片。这个区分直接影响可用性——provider 挂掉时，旧实现要白等 30 秒
> 才切下一家，新实现 3 秒。连接池改成进程级共享后，省掉的是每次调用的 TLS 握手。
