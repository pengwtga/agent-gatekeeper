# T1-5 优雅退出

## 1. 问题

Agent 请求是长请求（预算 12s）。`docker restart` / 发版时 Docker 发 SIGTERM，
uvicorn 默认配置会**立即开始关闭**，在途请求被掐断——用户看到连接重置，
而传统 100ms 接口根本感知不到这个问题。**长请求服务必须重新审视退出语义**。

## 2. 判断

优雅退出的三个环节必须同时对齐，缺一不可：

```
Docker: stop_grace_period 30s        ← SIGTERM 到 SIGKILL 的窗口
uvicorn: --timeout-graceful-shutdown 25   ← 等在途请求完成的窗口
后端预算: 12s                          ← 单请求最坏耗时
```

约束链：`graceful_shutdown(25s) > request_budget(12s)`：保证最后一个进来的请求
也能跑完；`stop_grace_period(30s) > graceful_shutdown(25s)`：保证 uvicorn 自己
不会被 Docker 先杀。

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ uvicorn 优雅退出 + compose stop_grace_period | 两个参数，零代码 |
| ❌ 依赖 lifespan aclose 就够 | aclose 关的是连接池，不管在途请求；uvicorn 才管 |
| ❌ preStop hook + 睡眠（K8s 模式） | 单机 docker compose 用不上，过度设计 |

k8s 的 preStop sleep 方案解决的是「还没收到流量摘除信号就有新请求进来」。实测发现
**单机也存在同样的窗口**（SIGTERM 后监听 socket 立即关闭，nginx 仍会转发过来），
但单机没有"把 upstream 标 down"的机制，要解决只能改 nginx 配置或上双实例 ——
超出本轮范围，记在 §6 边界里。本轮先保证「在途请求不被掐断」这个主目标。

## 4. 改动

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--timeout-graceful-shutdown", "25"]
```

```yaml
# docker-compose.yml api 服务
stop_grace_period: 30s
```

配套：lifespan shutdown 里 `await llm_client.aclose()` / `await rag.aclose()`
（T1-2），在途请求结束后释放连接池，进程干净退出。

## 5. 验收（容器实测，2026-11-12）

用一个最小镜像复现退出语义：FastAPI + `/slow?sec=N` 挂起 N 秒，
`--timeout-graceful-shutdown 25`、`--stop-timeout 30`（等价 compose 的 `stop_grace_period`），
每次 `docker stop` 计时并记录在途请求的最终状态。

| 场景 | 在途请求 | `docker stop` 实际耗时 | 在途请求结果 |
|---|---|---|---|
| **A 无在途** | 无 | **0.34s** | — |
| **B 8s 请求，1s 后 stop** | 剩余 ~7s | **7.25s** | ✅ `200 {"slept":8.0}` 完整返回 |
| **C 40s 请求，1s 后 stop** | 超过 25s 窗口 | **25.46s** | ❌ `500`，日志 `CancelledError: Task cancelled, timeout graceful shutdown exceeded` |
| **D 停机中新建请求** | — | — | ❌ 连接被拒（curl `000`，0.017s），日志无 access 记录 |

**四个结论：**

1. **不是固定延迟。** A 场景 0.34s 就退出——`stop_grace_period: 30s` 是 SIGKILL
   的最后期限（deadline），不是强制等待时长。没有在途请求时立刻退。
2. **是「等到没事干」为止。** B 场景等 7.25s 就返，不是等满 25s。uvicorn 收到
   SIGTERM 后进入 `Waiting for connections to close`，最后一个连接关闭就走下一步。
3. **25s 是强制取消，不是强制等待。** C 场景超窗后 uvicorn 主动 cancel 任务，
   客户端收到 500 而不是被静默掐断——**这比 SIGKILL 的直接断连好**，因为调用方
   至少知道「这次不是我网络问题」。25 < 30 的意义就在这里：让 uvicorn 有机会
   自己收尾，而不是被 Docker 一刀砍掉。
4. **停机期间不接新请求。** D 场景连接被立刻拒绝（0.017s），不是「先接受再关闭」。
   uvicorn 收到 SIGTERM 后**立即关闭监听 socket**。

## 6. 边界

- **优雅退出只保护在途请求，不保护新请求。** 上面 D 场景说明：一旦 SIGTERM，
  新连接直接被拒。此时 nginx 还在把请求转发到这个已经关掉的端口，用户看到的是
  **502**。`docker compose up -d` 重建 api 时，「旧容器已退出、新容器还没监听」的
  这个窗口（Python 启动 + RAG warmup，约 1~5s）就是一段真实的 502 窗口。
  **真正的解法是先摘流量再退出**（nginx 把该 upstream 标 down，或起双实例蓝绿），
  单靠 `stop_grace_period` 做不到。个人项目暂不处理，但要明确知道它的存在。
- `stop_grace_period` 30s 意味着最坏 `docker stop` 阻塞 30s；对个人项目可接受，
  追求秒级发版要上蓝绿/多实例。
- **`/eval` 会撞上 C 场景。** 30 条串行 ≈ 100s+，远超 25s 窗口，重启时必然被
  cancel 成 500。这不是优雅退出没做好，而是长循环接口不该用单请求模型
  （T2-4 改异步任务 + 轮询）。
- 进程内限速字典、连接池都在进程内，退出即丢，无状态迁移问题；一旦上多实例，
  这里会变成「重启丢配额计数」，需要外置到 Redis。

## 7. 面试讲法

> 传统接口重启没人感知，但 Agent 请求 5~12 秒，粗暴重启就是一批用户失败。
> 优雅退出是三个数字的对齐：Docker 宽限 30s > uvicorn 等待 25s > 请求预算 12s。
>
> 但这三个数字都是**上限，不是等待时长**。我实测过：没有在途请求时 0.34 秒就退，
> 有 7 秒的请求就等 7 秒——它是"等到没事干为止"，不是"固定 sleep 30 秒"。
> 超过 25 秒没跑完的会被主动 cancel 并返回 500，而不是让 Docker SIGKILL 直接断连，
> 调用方至少能知道是自己慢，不是网络问题。
>
> 我也踩到一个边界：**优雅退出只保护在途请求，不保护新请求**。SIGTERM 一响监听
> socket 就关了，nginx 还在往这边转发，用户看到 502。真正的解法是先摘流量再退出，
> 单机 compose 做不到，得上双实例——这个我知道，只是没到那个规模。
>
> 这也是长请求服务和 CRUD 服务在运维语义上的本质区别——**请求越长，
> 退出协议越重要**，这类问题在传统后端里根本不会出现。
