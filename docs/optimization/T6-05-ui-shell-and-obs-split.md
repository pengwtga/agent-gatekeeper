# T6-5 左侧栏导航 + 可观测拆页

> 起因：可观测面板把所有东西堆在一页（6 个 KPI、4 张图、3 组分布、50 行流水），
> 一屏放不下必须下滑；导航还是每页顶部的一排胶囊按钮，页面一多就排不开，
> 也无法表达「可观测」下面还有子页这一层。

## 1. 问题

### 1.1 导航长在页面里，不是外框

原来 `Header` 组件内部渲染 `<nav>`，意味着：

- 每个页面都要重复挂载一遍导航，位置还跟着标题高度浮动；
- 加页面就要改 `Header` 里的常量数组，导航和路由两处维护；
- 胶囊只能平铺，表达不了层级。「可观测」实际是三个视角，被压成一个入口。

### 1.2 一页塞太多，首屏看不到趋势

拆之前的顺序是：6 KPI → 4 张趋势图（2×2）→ 降级构成 → 置信度 → JSON 健康度 → 流水表。
即使是 1440px 宽的桌面，也要滚 2 屏多。而值班时的关注点是分层的：

- 出事了先看**整体健不健康**（KPI + 趋势）；
- 定位到某个时刻，再看**这一分钟里发生了什么**（流水 + 链路）；
- 复盘时才关心**失败形态**（降级原因、JSON 解析、置信度分布）。

三件事发生在不同时机，硬放在一页就是每次都把另外两页也加载一遍眼力。

### 1.3 顺手暴露的两个小问题

- `/kb/stats`、`/stats` 这类元数据端点也进了业务流水，表里出现一行 `GET /kb/stats 0ms`，
  和「用户请求」混在一起；
- 开发态 `?demo=1` 会把同一个问题发两遍 —— StrictMode 下 effect 执行两次，
  页面上出现两条相同的提问，还白烧一次 LLM 配额。

## 2. 决定

| 决策 | 理由 |
|---|---|
| 导航抽成 `Layout` 外框 | 导航只挂一次；路由层级 = 导航层级，一处维护 |
| 可观测拆成 三个子路由 | 按「看的时间点」拆，不是按「数据类型」拆 |
| 数据拉到 shell 层，子页用 context | 三个子页要同一份快照；各自轮询会在切页时丢数据、重复请求 |
| 链路抽屉放 shell | 入口在流水页、展示是覆盖层，不归属任何一页 |
| 页面标题去掉渐变大色块 | 侧栏布局里大色块会和导航抢视线，每切一次页闪一次 |
| 去掉页面内 emoji，统一用 SVG 线性图标 | 混排时 emoji 的字重和基线都不一样，侧栏里尤其明显 |

## 3. 改动

### 3.1 结构

```
src/
├── components/
│   ├── Layout.jsx          # 左侧栏 + <Outlet />
│   └── Header.jsx          # 只剩标题/描述/徽标，导航搬走
├── observability/
│   ├── MetricsProvider.jsx # 轮询一次 + 抽屉状态 + 复制
│   ├── TraceDrawer.jsx     # 瀑布图（从页面里搬出来）
│   └── ui.jsx              # 格式化函数 + Kpi + Bars + derive()
└── pages/
    ├── Observability.jsx   # 壳：标题 + 子页 tab + 刷新间隔 + 抽屉
    ├── observability/
    │   ├── Overview.jsx    # KPI + 4 张趋势图
    │   ├── Requests.jsx    # 流水表 + 过滤 + 下钻
    │   └── Quality.jsx     # 降级构成 / 置信度 / JSON 健康度
    ├── Playground.jsx
    └── Assistant.jsx
```

路由：

```jsx
<Route element={<Layout />}>
  <Route path="/" element={<Navigate to="/assistant" replace />} />
  <Route path="/assistant" element={<Assistant />} />
  <Route path="/playground" element={<Playground />} />
  <Route path="/observability" element={<Observability />}>
    <Route index element={<Overview />} />
    <Route path="requests" element={<Requests />} />
    <Route path="quality" element={<Quality />} />
  </Route>
</Route>
```

默认落地页从 `/playground` 改成 `/assistant`：值班场景第一动作是「问一句」，
不是「填表」。根因分类是拿到结构化事件之后才用的。

### 3.2 数据只拉一次

```jsx
export function MetricsProvider({ children }) {   // 轮询 /metrics
  ...
  return <Ctx.Provider value={{ data, err, lastAt, refreshSec, setRefreshSec,
                                trace, traceState, openTrace, closeTrace, copied, copy }}>
```

三个子页通过 `useMetrics()` 取同一份快照。切 tab 时 shell 不卸载，轮询不中断，
所以从概览跳到流水看到的是同一个时刻的数据，不会出现「两张图差 5 秒」。

### 3.3 元数据端点排除

```python
counted = not (path in ("/health", "/metrics", "/stats", "/kb/stats")
               or path.startswith("/traces/"))
```

判断标准是「这个请求是否产生业务价值」。探针、指标快照、链路查询、元数据统计
都只服务于「看」这个动作，混进来会把真实请求稀释掉。

### 3.4 demo 去重

```jsx
const demoRan = useRef(false)
useEffect(() => {
  if (demoRan.current) return
  demoRan.current = true
  ...
}, [])
```

StrictMode 是**故意**双跑 effect 来暴露副作用没清理的问题。这里不是要清理，
而是要「只执行一次」——ref 是正确工具，`useState` 会多一次渲染。

## 4. 验收

| # | 项 | 结果 |
|---|---|---|
| 1 | 左侧栏三个一级入口 + 可观测三个子项 | ✅ 当前页高亮，可观测组随子路由高亮 |
| 2 | 默认路由 | ✅ `/` → `/assistant` |
| 3 | 概览页 | ✅ 无需滚动即可看到 6 KPI + 4 张图（1440×900） |
| 4 | 流水页 | ✅ 13 行业务请求，`GET /kb/stats` 已消失 |
| 5 | 质量页 | ✅ 降级构成 / 置信度 / JSON 健康度三块 |
| 6 | 链路抽屉跨页可用 | ✅ `/observability/requests?trace=xxx` 直接打开瀑布图 |
| 7 | 切 tab 不断轮询 | ✅ shell 不卸载，数据不闪空 |
| 8 | demo 不再重复 | ✅ 截图中只有一条提问，一次 LLM 调用 |
| 9 | 打包体积 | ✅ 192.6 KB / gzip 64.8 KB（+0 KB 依赖，图表仍是手写 SVG） |
| 10 | 窄屏 | ✅ <860px 侧栏变横向条，子项收起 |

## 5. 边界

- **刷新间隔仍是最低 5s**：nginx 对该路径限流 20 次/分钟，面板每秒刷会自己把配额吃满。
- **子页共享一份快照的代价**：在流水页停留时也在拉指标（三个 tab 一样），
  但只有一张表在用。真在意流量的话应该按页订阅字段，现在没必要。
- **`?trace=` 只在可观测壳内生效**：在 `/assistant` 打开带 trace 的链接不会弹抽屉。
  这是有意的 —— 抽屉是观测视图的一部分，不该在别的页面凭空出现。
- **左侧栏没有折叠态**：只有三个入口，折叠按钮是负资产。入口涨到 8 个以上再考虑。

## 6. 面试讲法

> 「原来的可观测是一页堆完：6 个 KPI、4 张图、3 组分布、50 行流水，必须滚两屏。
> 拆页的时候我没有按『数据类型』拆（图表一页、表格一页），而是按**看的时间点**拆：
> 出事时先看整体，定位到某一分钟再看流水和链路，复盘才看失败形态。
>
> 数据层做了个取舍：三个子页共享 shell 层的一次轮询，用 context 下发。
> 如果每页自己拉，切 tab 会丢刚拉到的数据、重复发请求，还会出现两张图时间不一致。
>
> 顺带修了两个观测口径问题：`/kb/stats` 这类元数据端点原来也进业务流水，
> 表里会和真实请求混在一起；开发态 `?demo=1` 因为 StrictMode 双跑 effect，
> 同一个问题被问了两遍 —— 这两个都是『打开页面才发现』的问题，靠单元测试发现不了。」
