# 容器化 + 前端框架化改造手册

> 目的：把「一个 systemd 上跑着的 Python 服务 + 两页内联 HTML」改造成「docker compose 一键起 + React 单页应用」。
> 顺带补上 360 那个 JD 里的三条硬要求：Docker 打包部署、React 前端、CI/CD 基础流程。

预期工时：**Docker 化半天，前端改造 1~2 天**。每一步都给了验收标准，做完打勾。

---

## 一、Docker 化（约半天）

### Step 1 · 先想清楚「为什么要容器化」，再动手

面试官一定会问这个。别答「因为流行」，答这三点（对应你原来 systemd 部署的真实痛点）：

| 原来（systemd 直跑） | 容器化之后 |
|---|---|
| 换台机器要手工装 Python、装依赖、配 venv | `docker compose up -d` 一条命令，环境完全一致 |
| `.env` 里的密钥散落在服务器文件系统 | 密钥只走环境变量注入，**不进镜像**，`docker history` 可查 |
| 改了代码要 `git pull` + 手动重启 + 祈祷没装新依赖 | 依赖层和代码层分离，改代码只重建最后一层（3 秒内） |

### Step 2 · 三件套已经写好，直接看为什么这么写

- `Dockerfile`：`requirements.txt` 单独 COPY 再 `pip install`，**依赖层和代码层分离**。这是最重要的一行设计——只改业务代码时，`pip install` 那一层直接命中缓存，构建从 60s 降到 3s。
- `.dockerignore`：把 `.env`、`.venv`、`.git` 排掉。**`.env` 里有你的智谱 key，绝不能进镜像**——进一次就会被 `docker history` 扒出来。
- `docker-compose.yml`：`api` + `web` 两个服务，`env_file: .env` 注入密钥，`./data` 挂宿主机（数据集和向量缓存重启不丢）。

### Step 3 · 本地验证构建

```bash
cd agent-gatekeeper

# 只构建后端，先确认能过
docker build -t agent-gatekeeper-api:latest .

# 看镜像多大（python:3.12-slim + 依赖，实测 235MB）
docker images agent-gatekeeper-api

# 确认 .env 没被打进镜像 —— 应该只列出 app / data / scripts，没有 .env
docker run --rm agent-gatekeeper-api:latest ls -a /app
```

**验收**：`docker build` 成功，且 `ls -a /app` 里**不存在 `.env`**。

> 实测结果（本次已跑通）：后端镜像 235MB，`ls -a /app` 无 `.env`，`docker history` 无密钥残留。


### Step 4 · 起服务并逐项验收

```bash
docker compose up -d --build
docker compose ps                     # api 应为 (healthy)，web 应为 Up
docker compose logs -f api            # 看到 "RAG 值班助手就绪 | 模式: vector" 即正常
```

打开浏览器逐项过：

- [x] `http://localhost:8001` 打开是根因分类页
- [x] 点「案例：审批通过但没生成任务」→ 点「开始分类」→ 5~10 秒后出根因 + 置信度 + 依据
- [x] 顶部切到「值班助手」→ 点第一个案例 → 出处置建议 + 相似工单 Top3
- [x] `http://127.0.0.1:8002/docs` 后端 Swagger 可访问（容器网络正常）
- [ ] `docker compose restart api` 后，知识库条数不变（说明 `./data` 挂载生效，向量缓存没丢）
- [x] `docker compose down` 再 `up -d`，配置不丢（说明 `.env` 注入正确、不依赖镜像内文件）

> 本次实测（2026-10-26）：两个镜像均构建成功，`docker compose up` 后 `api` 一次通过 healthcheck、`web` 等 healthy 才启动；`/api/health` 返回 `llm_configured: true, llm_providers: [zhipu]`；`/api/classify` 端到端返回 `xflow_callback_lost / 0.95`。
> 另：启动时智谱 embedding 返回 429，RAG 自动降级为 `keyword+rerank`——这是原设计里的容灾分支，不是容器化引入的问题，但也说明**免费档 embedding 有速率上限，演示前先热一下**。


### Step 5 · 上服务器（124.222.174.133）

```bash
# 服务器上先装 docker（Ubuntu/Debian）
curl -fsSL https://get.docker.com | sh

# 把代码拉上去（不要拉 .env）
rsync -av --exclude='.venv' --exclude='node_modules' --exclude='.git' ./ root@124.222.174.133:/opt/agent-gatekeeper/
# .env 单独 scp，保证它只存在于服务器本地
scp .env root@124.222.174.133:/opt/agent-gatekeeper/.env

ssh root@124.222.174.133
cd /opt/agent-gatekeeper
chown -R 10001:10001 data      # 容器内是非 root 用户，Linux 上必须放权，否则写不了向量缓存

# 关键：旧的 systemd 服务还占着 8001，不停掉 compose 会报 "address already in use"
systemctl stop agent-gatekeeper && systemctl disable agent-gatekeeper

docker compose up -d --build
```

**验收**：`http://124.222.174.133:8001` 能打开（端口和容器化之前一致，README 里的在线体验链接不用改），且 `curl http://127.0.0.1:8001/api/health` 返回 `llm_configured: true`。

> 端口规划说明：对外只开 **8001**（nginx），后端 8001 走容器内网；另映射 `127.0.0.1:8002` 给 Swagger 本地查看，绑 127.0.0.1 所以公网扫不到 `/docs`。保留 8001 的原因：README 在线链接、防火墙规则、肌肉记忆都不用改——**改造类项目不该顺手改掉对外契约**。


> 踩坑记录（面试可以讲）：宿主机 bind mount 到容器时，容器内 uid=10001 的用户对宿主目录没有写权限，RAG 启动写 `kb_vectors.json` 会 EACCES，表现是服务能起但 `/kb/stats` 一直是 `ready: false`。解法是 `chown 10001`。这个坑在 macOS 上不会出现（Docker Desktop 的 osxfs 会做 uid 映射），只在 Linux 服务器上暴露——这类「本地能跑、上线就炸」的问题，正是容器化需要真实部署一遍才能踩到的。

---

## 二、React 前端改造（约 1~2 天）

### 改造前是什么样

`app/main.py` 495 行里，有 300 行是 CSS 字符串 + 手写 HTML 字符串 + 字符串拼接的 JS。问题不在于丑，在于：

1. **状态靠手改 DOM**：`box.innerHTML = ...`，每次渲染都是从零拼字符串，事件要挂 `onclick="run()"` 全局函数
2. **两个页面各自复制一套壳**：`page_shell()` 拼头、拼 nav、拼 style，改一处要改两处
3. **后端变成模板引擎**：一个 Python 服务同时干 API 和页面渲染，前端同学没法独立改

### Step 1 · 建工程（10 分钟）

文件已经生成好了，`frontend/` 目录就是。跑起来：

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173
```

**验收**：5173 能打开页面、能请求通后端（Vite 代理已配好 `/api` → `localhost:8001`）。

> 关键设计：**所有请求统一走 `/api` 前缀**。dev 由 Vite proxy 转发，prod 由 nginx 反代。这样前端代码里没有 `if (import.meta.env.DEV)` 分支，也不会出现「开发环境写死 localhost，上线忘记改」的经典事故。

### Step 2 · 先抽接口层，别先搬 UI

看 `src/api.js`。改造顺序很关键：**先定义清楚「页面需要哪些数据」，再写页面**。

原来页面里是这样的：

```js
const resp = await fetch('/classify', {...});
const d = await resp.json();
if (d.detail) { box.innerHTML = '参数错误'; return; }
```

现在是 `classify()` 一个函数，异常在 `request()` 里统一归一化成 `Error`，页面只管 `try/catch` 显示 `e.message`。

**为什么先做这层**：等你要换 baseURL、加统一鉴权头、加超时重试的时候，只改一个文件。这是「接口层」和「组件层」的分界线。

### Step 3 · 迁设计系统（20 分钟）

`src/styles.css` 是从原 `PAGE_CSS` / `PLAYGROUND_CSS` / `ASSISTANT_CSS` **原样搬过来**的。

**这里刻意没有重新设计视觉**——改造类项目最忌讳「顺手把 UI 也改了」，出了问题分不清是重构引入的还是改样式引入的。先保证像素级一致，再谈优化。

### Step 4 · 拆组件：从「拼字符串」到「声明式」

原来 `page_shell()` 干的事，现在拆成 `components/Header.jsx`：

```jsx
<Header title="🔍 根因分类" desc="...">
  {kb && <span className="tag">📚 知识库 {kb.tickets} 条</span>}
</Header>
```

**这就是 React 相对字符串拼接最实在的收益**：原来值班助手页要往 header 里塞一个知识库徽标，得用 `document.querySelector('header > div').appendChild(b)` 这种命令式操作，还带两行 `style.marginLeft/marginTop`；现在只是传 children。

> 面试讲点：**声明式 vs 命令式**。原来是「我先查出这个 DOM 节点，再告诉它变成什么样」；现在是「我描述这个位置的 UI 应该取决于哪些状态」。状态变了 React 自己算 diff。这不是审美问题，是「状态和视图不同步」这一类 bug 的根因——原来 `render()` 里少写一个分支，UI 就停在上一次的状态。

### Step 5 · 两个页面组件化

`pages/Playground.jsx` / `pages/Assistant.jsx`。核心转变：

| 原来 | 现在 |
|---|---|
| `document.getElementById('payload').value` | `const [payload, setPayload] = useState(...)` |
| `box.innerHTML = '<span class="typing">分类中…</span>'` | `{loading && <span className="typing">…</span>}` |
| `btn.disabled = true` | `disabled={loading}` |
| `chat.appendChild(div)` | `setMessages(prev => [...prev, msg])` |
| 页面跳转整页刷新 | `react-router` 客户端路由，Header 不重新挂载 |

**验收**：
- [x] 两个页面互相切换没有整页白屏闪烁（F12 Network 里不应有新的 document 请求）
- [x] 分类页点案例卡片 → textarea 内容变、上一次结果被清空
- [x] 分类中按钮置灰、重复点击不会发第二个请求（`if (loading) return`）
- [x] 助手页发送后自动滚到底部
- [x] 后端挂掉时页面显示可读错误，不是白屏

> 实测（`npm run build`）：39 个模块，产物 `index.js` 175KB / gzip 58KB，`index.css` 5.7KB，构建 0.9s。
> 视觉上和改造前的服务端渲染版本逐屏比对过，结果态（根因卡片 / 对话气泡 / 相似工单进度条）一致。


### Step 6 · 生产接法

`frontend/nginx.conf` 做了三件事，每一件对应一种线上事故：

1. `location /api/ → proxy_pass http://api:8001/`：容器间用服务名通信，不用写 IP
2. `try_files $uri $uri/ /index.html`：SPA 路由回退。不加这条，用户在 `/assistant` 刷新会 404——因为 nginx 找不到叫 `assistant` 的文件
3. `proxy_read_timeout 120s`：`/eval` 会循环调 30 次 LLM，默认 60s 会超时

`frontend/Dockerfile` 是多阶段构建：`node:20-alpine` 编译出 `dist/`，再拷进 `nginx:alpine`。**最终镜像里没有 node_modules，也没有源码**，从 500MB+ 降到 60MB 左右。

---

## 三、这套改造在面试里怎么讲

不要讲「我用了 React 和 Docker」，讲**你解决了什么问题、付出了什么代价**：

1. **为什么值得改**：原来 495 行的 `main.py` 里 300 行是前端代码，后端加个字段要同时改 HTML 字符串和 JS 拼接，前后端没法并行。现在后端只返回 JSON，`main.py` 里那 300 行可以直接删掉。

2. **代价是什么（一定要主动说）**：多了一个前端工程要维护、多了一个 Node 构建环节、CI 时间变长。**只讲收益不讲代价的候选人，面试官会认为你没真正做过取舍。** 你的判断是：这个项目本来就是给人看的 demo，前端体验和可维护性的权重高于运维复杂度，所以值得。

3. **最容易被追问的两个点，提前准备**：
   - 「为什么不用 Next.js / SSR？」→ 这个应用没有 SEO 需求，两个页面都是强交互的后台工具，CSR 足够；SSR 会引入 Node 运行时和两套部署链路，收益不匹配成本。
   - 「Docker 化之后怎么发布？」→ `git pull` + `docker compose up -d --build`。再进一步是 GitHub Actions：push tag → build & push 镜像到 registry → 服务器 `docker compose pull && up -d`。**这一步你还没做就别说做了**，说「目前是手动，下一步会接 Actions」反而加分。

---

## 附：文件清单

```
agent-gatekeeper/
├── Dockerfile               新增  后端镜像（非 root、健康检查、分层缓存）
├── .dockerignore            新增  排除 .env / .venv / frontend
├── docker-compose.yml       新增  api + web 两个服务
├── frontend/                新增  React 前端
│   ├── Dockerfile                多阶段：node 构建 → nginx 托管
│   ├── nginx.conf                /api 反代 + SPA 路由回退 + gzip
│   ├── vite.config.js            dev 代理
│   ├── src/api.js                统一请求层，错误归一化
│   ├── src/styles.css            原 CSS 原样迁移
│   ├── src/components/           Header / Tags
│   └── src/pages/                Playground / Assistant
├── app/main.py              待改  确认新前端稳定后，可删除 page_shell 与两个页面路由
└── data/                    忽略  kb_vectors.json 已加入 .gitignore
```
