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

k8s 的 preStop sleep 方案解决的是「还没收到流量摘除信号就有新请求进来」，
单机场景 nginx→api 是静态 upstream，不存在摘除窗口问题，不需要。

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

## 5. 验收

需要 docker：发一个 /classify，1s 后 `docker restart`，断言该请求返回完整结果。
本机无 docker，逻辑正确性由参数对齐关系背书（25 > 12），容器化验证时执行。

## 6. 边界

- 25s 窗口内新请求依然会被接收（uvicorn 停止监听前的小窗口），极端情况存在
  「接受后立刻被关闭」的边缘——生产级修法是收到 SIGTERM 先摘流量再等，单机无此问题
- `stop_grace_period` 30s 意味着最坏重启时间 30s+；对个人项目可接受，
  追求秒级发版要上蓝绿/多实例

## 7. 面试讲法

> 传统接口重启没人感知，但 Agent 请求 5~12 秒，粗暴重启就是一批用户失败。
> 优雅退出是三个数字的对齐：Docker 宽限 30s > uvicorn 等待 25s > 请求预算 12s。
> 这也是长请求服务和 CRUD 服务在运维语义上的本质区别——**请求越长，
> 退出协议越重要**，这类问题在传统后端里根本不会出现。
