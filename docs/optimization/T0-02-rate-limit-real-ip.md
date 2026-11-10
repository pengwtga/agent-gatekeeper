# T0-02 限速改用真实客户端 IP

> 文件：`app/main.py`　函数：`_client_ip` / `_check_rate`
> 类型：逻辑错误（限速在生产环境实际退化为「全站 20 次/分钟」）+ 内存泄漏隐患

## 1. 问题

原实现：

```python
def _check_rate(ip: str) -> None: ...
...
_check_rate(request.client.host)
```

部署形态是 `nginx(80) → api(8001)`，后端只绑 `127.0.0.1:8002`（`docker-compose.yml`），
所有请求都从 nginx 容器转发进来。于是 `request.client.host` 拿到的是**nginx 容器的内网 IP**，
对所有真实用户都是同一个值。

后果很具体：`RATE_LIMIT_PER_MIN = 20` 从「单 IP 每分钟 20 次」变成「全站每分钟 20 次」。
任何一个爬虫或一个手快的人，就能把整站打到 429。限速在开发环境（直连 8001）完全正常，
一进容器就失效 —— 这类问题只有把部署形态画出来才能发现。

两个附带问题：

- `_rate_bucket` 只增不减，被扫描器扫一遍 URL 就会留下大量僵尸 key。内存泄漏是慢性的，
  但它同时也是一个攻击面：制造 key 的成本比服务端清理的收益低。
- nginx 已经注入了 `X-Real-IP` 与 `X-Forwarded-For`（`frontend/nginx.conf` 的 `/api/` location），代码完全没用。

## 2. 方案：取真实 IP，但要先判断「谁在说谎」

直接读 `X-Real-IP` 是另一种错误 —— 这个头是客户端可以随便写的。如果后端将来以任何方式
直接暴露，攻击者带一个随机的 `X-Real-IP` 就能无限刷，限速形同不存在。

所以判断顺序是**先看对端是谁，再看头**：

```
peer = request.client.host
peer 是 loopback / 内网？
  ├─ 是 → 相信转发头：X-Real-IP（nginx 注入）→ X-Forwarded-For 取第一个 → peer
  └─ 否 → 忽略转发头，直接用 peer
```

内网判定用 `ipaddress` 的 `is_loopback or is_private`，覆盖 `127.0.0.1`、`::1`、
`172.16/12`（Docker 默认网段）、`10/8`、`192.168/16`。这比维护一个代理白名单可靠，
也不会因为换机器就失效。

## 3. 改动

`app/main.py`：

- 顶部 `import ipaddress`、`import time`（原来在 `_check_rate` 里做局部 import，另有
  `/eval` 循环里也有一处 `import time`，一并收口到模块级）
- 新增 `_is_trusted_proxy(host)`、`_client_ip(request)`
- `_check_rate(ip: str)` → `_check_rate(request: Request)`
- 增加每 5 分钟一次的过期清理（`_SWEEP_INTERVAL_S`），只删「窗口内已无请求」的 key
- 3 个调用点（`/classify`、`/eval`、`/assist`）同步改为 `_check_rate(request)`
- 触发限速时打一条 `[RateLimit] ip=...` 的 warning，便于区分「被刷」和「用户手快」

清理逻辑放在检查之前、且有间隔限制，不用每次请求都遍历整个字典：

```python
if now - _last_sweep > _SWEEP_INTERVAL_S:
    for k in [k for k, v in _rate_bucket.items() if not v or now - v[-1] > _RATE_WINDOW_S]:
        _rate_bucket.pop(k, None)
    _last_sweep = now
```

## 4. 验收

用 `TestClient(client=("127.0.0.1", 12345))` 模拟 nginx 转发进来的请求：

```python
codes = [c.post("/classify", json={"incident": {"stage": "register"}},
                headers={"X-Real-IP": "1.1.1.1"}).status_code for _ in range(21)]
```

实测：

```
PASS T0-2 前 20 次未限速  [200 × 20]
PASS T0-2 第 21 次 429  429
PASS T0-2 另一个 IP 不受影响  200
PASS T0-2 不可信对端的转发头被忽略（不会按 9.9.9.9 记账）  ['8.8.8.8']
PASS T0-2 8.8.8.8 自己独立计数  ['8.8.8.8']
```

最后两条是关键：对端是 `8.8.8.8`（非内网）时，即使请求头写了 `X-Real-IP: 9.9.9.9`，
记账用的仍然是 `8.8.8.8` —— 伪造头绕不过限速。

真机验证（`docker compose up` 后）：

```bash
for i in $(seq 1 22); do
  curl -s -o /dev/null -w "%{http_code} " -H "X-Real-IP: 1.1.1.1" \
    -X POST localhost:8001/api/classify -d '{"incident":{}}' -H 'Content-Type: application/json'
done; echo
```

## 5. 边界与遗留

- **多实例必换 Redis**。这套是进程内滑动窗口，两个 worker 各算各的，等于阈值翻倍。
  当前是单容器部署，够用；扩容时必须换共享存储（`00` 文档 §6 已列）。
- **加 CDN 后要改信任链**。若前面再加一层 CDN，内网对端变成 CDN 节点，`X-Real-IP` 由 CDN 注入；
  此时应取 `X-Forwarded-For` 的**倒数第一个非可信地址**，而不是第一个 —— 第一个是客户端自己写的。
- **IPv6**：`is_private` 对 `fc00::/7` 返回 True，行为一致，无需特殊处理。
- 阈值 20/min 是拍出来的。真实值应由「正常值班用户的操作频率」决定，现在没有真实流量，
  先保守；T2 加指标后应该改成按 429 比例调。

## 6. 面试讲法（40 秒）

> 这个限速原来取 `request.client.host`，在 nginx 反代后面拿到的是 nginx 容器的 IP，
> 所有用户共用一个桶，20/min 变成全站 20/min。开发环境直连后端完全测不出来。
>
> 修的时候有个细节值得说：不能直接信 `X-Real-IP`，那个头客户端能随便写。所以要先用
> `ipaddress` 判断对端是不是内网/loopback，是才相信转发头。我用测试验证了伪造头的情况 ——
> 对端是公网 IP 时，头里的 IP 会被忽略。
>
> 顺带修了一个内存泄漏：原来的桶只增不减，被扫描器扫一遍就留一堆 key。现在每 5 分钟清理一次。
