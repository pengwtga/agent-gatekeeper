import { NavLink, Outlet } from 'react-router-dom'
import Header from '../components/Header.jsx'
import TraceDrawer from '../observability/TraceDrawer.jsx'
import { MetricsProvider, useMetrics } from '../observability/MetricsProvider.jsx'

/**
 * 可观测外壳：负责「拉一次数据、渲染标题与子页导航、挂链路抽屉」。
 * 三个子页（概览 / 请求流水 / 质量分析）只画内容，不碰请求与轮询。
 */

const TABS = [
  { to: '/observability', label: '概览', end: true },
  { to: '/observability/requests', label: '请求流水' },
  { to: '/observability/quality', label: '质量分析' },
]

const REFRESH_OPTIONS = [
  { label: '5 秒', value: 5 },
  { label: '15 秒', value: 15 },
  { label: '暂停', value: 0 },
]

function Shell() {
  const { err, lastAt, refreshSec, setRefreshSec, trace, traceState, closeTrace, copied, copy } = useMetrics()

  return (
    <div className="wrap wide">
      <Header
        title="可观测"
        desc="请求量、延迟分位、降级率、JSON 解析健康度与 token 消耗；每条请求都能下钻到各阶段耗时"
      >
        <div className="head-chips">
          <span className="tag t-ok">数据自进程启动以来</span>
          {lastAt && (
            <span className="tag t-mode">更新于 {lastAt.toLocaleTimeString('zh-CN', { hour12: false })}</span>
          )}
        </div>
      </Header>

      <div className="subtabs">
        <nav className="subtabs-nav">
          {TABS.map(({ to, label, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? 'on' : undefined)}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="seg">
          {REFRESH_OPTIONS.map((o) => (
            <button
              key={o.value}
              className={refreshSec === o.value ? 'on' : ''}
              onClick={() => setRefreshSec(o.value)}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="warn">指标拉取失败：{err}</div>}

      <Outlet />

      <div className="foot">
        <div>
          数据来源：进程内内存（<code>app/metrics.py</code>）· 重启归零 · 多 worker 不聚合 ·
          轮询不低于 5s，nginx 对该路径限流 20 次/分钟
        </div>
        <div>
          trace_id 与 JSON 日志同源，可在{' '}
          <code>docker compose logs api | jq 'select(.trace_id=="…")'</code> 里还原整条链路
        </div>
      </div>

      {(trace || traceState !== 'idle') && (
        <TraceDrawer
          trace={trace}
          state={traceState}
          onCopy={copy}
          copied={copied}
          onClose={closeTrace}
        />
      )}
    </div>
  )
}

export default function Observability() {
  return (
    <MetricsProvider>
      <Shell />
    </MetricsProvider>
  )
}
