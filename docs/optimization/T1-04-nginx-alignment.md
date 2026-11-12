# T1-4 nginx 超时与后端预算对齐

## 1. 问题

改前 nginx `proxy_read_timeout 120s`，注释写着「LLM 通道单次 5~10s，/eval 会循环 30 次，超时给足」。两个错误：

1. **语义错位**：120s 是「允许一个请求挂 2 分钟」，而 T1-1 之后后端承诺 12s 内必返回。
   上游允许的比下游承诺的多 10 倍，等于没有约束——慢请求长期占着 nginx→api 连接
2. **对 /eval 的误解**：/eval 循环 30 次，是 30 个**独立的** 12s 预算，不是一条 360s 的长连接。
   为不存在的场景放宽超时，是典型的「用猜测代替测量」

## 2. 判断

反向代理的超时不是「越大越安全」，它应该**略大于下游的最坏承诺**：

```
proxy_read_timeout(20s) > request_budget(12s) + 余量(8s)
```

- 小于下游最坏耗时 → 后端还在算，nginx 先断（用户看到 504，后端白算）
- 大太多 → 下游出问题时，用户挂 2 分钟才看到错误，连接池被慢请求占满

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ read 20s / connect 5s / send 20s | 与预算对齐，故障传导链路清晰 |
| ❌ 保持 120s | 慢请求占连接，且掩盖后端超时问题（没有 nginx 的 504 逼你修） |

nginx 先 504 有一个隐藏价值：**它逼着后端把总时长管起来**。把 nginx 调大等于拆掉报警器。

## 4. 改动

### 4.1 超时对齐（本轮主线）

```nginx
proxy_connect_timeout 5s;
proxy_send_timeout 20s;
proxy_read_timeout 20s;
```

注意 uvicorn 的 `--timeout-graceful-shutdown 25`（T1-5）> 20s：优雅退出的等待窗口
比正常请求超时略长，保证超时边缘的在途请求也能在重启时完成。

同时把 `proxy_connect_timeout` 从 10s 收到 5s：容器内网建连是毫秒级，5s 已经宽得离谱。

### 4.2 上游长连接

改 4.1 时发现一个相邻问题：配了 `proxy_http_version 1.1`，但既没有 `upstream` 长连接池，
也没有清掉 `Connection` 头，**每个请求仍然新建一条到 `api:8001` 的 TCP 连接**。

```nginx
upstream api_backend {
    server api:8001;
    keepalive 32;
    keepalive_requests 1000;
    keepalive_timeout 60s;
}

location /api/ {
    proxy_pass http://api_backend/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";   # 关键：清掉默认的 close
}
```

`proxy_http_version 1.1` 只是"允许"用 1.1，nginx 默认仍会发 `Connection: close`。
**两行必须同时存在**，少一行长连接池都是摆设。

> 注意区分：T1-2 优化的"共享连接池"是**后端 → LLM provider** 那一段；
> 这里是 **nginx → 后端** 那一段。两段都叫连接池，但属于不同的跳。

### 4.3 限流前移到 nginx

应用层 `_check_rate` 是**进程内**字典计数。单进程够用，但一旦 `--workers 4` 或水平扩容，
每个进程各算各的 20/min，实际放行量等于乘以进程数——**限流静默失效**。

```nginx
limit_req_zone $binary_remote_addr zone=api_rl:10m rate=20r/m;
limit_req_status 429;

location /api/ {
    limit_req zone=api_rl burst=5 nodelay;
}
```

三个参数的理由：

| 参数 | 取值 | 理由 |
|---|---|---|
| `rate=20r/m` | 与应用层同阈值 | 两个限流器阈值必须一致，否则宽松的那个是唯一有效的 |
| `burst=5` | 允许连点 5 次 | 演示时快速点几下不该 429；`nodelay` 让突发立即放行而不是排队 |
| `limit_req_status 429` | 默认是 503 | 503 会被前端当成"服务挂了"，429 才是"你太快了" |

`nodelay` 的取舍：不加它，突发请求会排队等待，响应变慢但不会拒绝；加了它，超出 burst 直接 429。
**用户要的是"立刻知道被限了"，而不是"等 3 秒再被限"**，所以选 nodelay。

### 4.4 429 响应与控制层同构

nginx 默认的 429 响应体是 HTML。前端 `api.js` 的逻辑是 `res.json().catch(() => ({}))`，
解析失败后只能回退到 `HTTP 429` 这种字符串——**同一个限流，用户会看到两种不同的错误文案**。

```nginx
location /api/ {
    error_page 429 = @rate_limited;
}

location @rate_limited {
    default_type application/json;
    add_header Retry-After 3 always;
    return 429 '{"detail":"请求过于频繁，请稍后再试"}';
}
```

形状和 `app/main.py` 的 `HTTPException(status_code=429, detail=...)` 完全一致，
前端不需要知道这个 429 是谁返回的。

### 4.5 记下但没做：proxy_buffering

现在没有流式响应，`proxy_buffering` 保持默认 `on`（对非流式更好：让上游连接更早释放）。
但一旦上 SSE，必须同时打开两行，否则 token 会被攒成大块再发：

```nginx
# proxy_buffering off;
# add_header X-Accel-Buffering no;
```

先注释在这里，是为了让"以后加流式"的人**在同一个文件里看到这条约束**，
而不是上线后才发现打字机效果没了。

---

## 5. 验收（容器实测）

本机有 docker，所以这一轮不是推演，是实跑。用 `python:3.12-alpine` 起一个假上游，
`nginx:1.27-alpine` 挂上这份配置：

```bash
docker network create ssrm-ngx-test
docker run -d --name api --network ssrm-ngx-test --network-alias api \
  python:3.12-alpine python -c "..."          # 固定返回 200 {"ok":1}
docker run -d --name ssrm-ngx-test --network ssrm-ngx-test -p 18080:80 \
  -v "$PWD/frontend/nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine
```

| 检查项 | 命令 | 结果 |
|---|---|---|
| 语法 | `nginx -t` | `syntax is ok` / `test is successful` |
| 反代连通 | `curl /api/classify` | `200`（假上游返回 `{"ok":1}`） |
| 限流生效 | 连续 7 次 | `200 200 200 200 200 200 429`（rate 1 次 + burst 5） |
| 429 同构 | `curl -i` | `Content-Type: application/json`，body `{"detail":"请求过于频繁，请稍后再试"}`，`Retry-After: 3` |

**一个实战踩到的坑**：第一版 `add_header Retry-After 3;` 不生效。
原因是 **`add_header` 默认只对 2xx/3xx 生效**，429/4xx/5xx 必须加 `always`。
这种"配置写了但没生效"的问题，不实测根本发现不了——它是这份文档里最值得留的一条。

**超时链路的验证方式**（本轮未做，留给 T2-4 改造 /eval 时一起）：
造一个 sleep 30s 的接口，断言 nginx 在 ~20s 返回 504 且后端日志有"预算耗尽"。
逻辑上已由 T1-1 的预算验收背书（后端 12s 必返回，早于 nginx 的 20s），
但要证明"nginx 真的会 504"仍需一次真跑。

---

## 6. 边界

- **/eval 30 条串行 ≈ 30 × 几秒，依然会超过 20s**！这是本次发现的真问题：
  长循环接口不适配单请求超时模型。正确修法是改异步任务 + 轮询（T2-4），
  本轮先把超时对齐到单请求语义。
- **`proxy_read_timeout` 不是"总时长"**，是"两次读之间的空闲"。当前接口非流式
  （后端静默 12s 后一次性返回），所以两者数值接近；一旦改流式，token 不断到达会
  持续重置计时器，20s 就不会切断长回答了。**总时长的硬上界只有应用层 Budget 能给**，
  nginx 开源版没有"总时长"指令。
- **双重限流的关系**：nginx 是权威（全局、worker 共享），应用层是兜底（单进程有效）。
  两者同阈值，正常情况下 nginx 先拦，应用层几乎不会触发。如果以后去掉 nginx 直接暴露
  FastAPI，应用层限流会自动退回"每进程 20/min"——这一点要写进部署文档。
- **nginx 是唯一知道客户端还在不在的一层**。客户端断开时 nginx 记 499 并释放连接，
  但后端通常不知道，会把这次 LLM 调用跑完（白烧 token）。SSE 改造时要靠
  `request.is_disconnected()` 或写入失败来感知，这是 T1 遗留的一个真实缺口。
- 宿主机/云负载均衡那一层的超时**不在本仓库**，需要单独确认（云厂商常见默认 60s，
  比 20s 宽松，不会先断，但要写进部署清单）。

---

## 7. 面试讲法

> 超时要沿着调用链对齐：nginx 20s > 后端预算 12s > 单次 LLM 尝试 3s。
> 每一层都应该比下一层的最坏承诺略大，这样超时一定发生在离故障最近的那层——
> 用户拿到的是"降级结果 + 原因"，而不是一个 504。
>
> 顺带说一个容易误解的点：`proxy_read_timeout` 的语义是两次读之间的空闲，不是总时长。
> 我现在的接口是非流式的，所以两者数值恰好接近；一旦上 SSE，这个 20s 根本不会切断
> 长回答。所以真正管总时长的是应用层的 Budget，不是 nginx。
>
> 改这个配置的时候还牵出三件事：`proxy_http_version 1.1` 不等于长连接，必须配
> upstream keepalive 和 `Connection ""`；应用层限流在多 worker 下会静默失效，
> 所以限流要前移到 nginx；`add_header` 默认不给 4xx 加头，`Retry-After` 要加 always
> 才会生效——这条是不实测发现不了的。
