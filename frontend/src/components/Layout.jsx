import { NavLink, Outlet } from 'react-router-dom'

/**
 * 应用外框：左侧固定导航 + 右侧内容区。
 *
 * 原来导航是塞在每页顶部的胶囊按钮（Header 里的 nav），有两个问题：
 * 1) 页面一多就横着排不下，且「可观测」本身又有子页，胶囊表达不了层级；
 * 2) 每个页面都要重复渲染一遍导航，位置还跟着 Header 的高度浮动。
 * 抽成外框后，导航只挂载一次，页面只管内容。
 */

const ICONS = {
  assist: 'M4 5.5h16v10H9.5L4 19.5z',
  classify: 'M11 3.5a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM20.5 20.5l-4.2-4.2',
  obs: 'M3 17.5l5.5-6.5 4 3.5 3.5-6 5 4.5',
}

function Icon({ name }) {
  return (
    <svg className="ico" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICONS[name]} />
    </svg>
  )
}

const MAIN_NAV = [
  { to: '/assistant', label: '值班助手', icon: 'assist', hint: '描述问题，检索历史工单' },
  { to: '/playground', label: '根因分类', icon: 'classify', hint: '结构化事件 → 根因 + 置信度' },
]

const OBS_NAV = [
  { to: '/observability', label: '概览', end: true },
  { to: '/observability/requests', label: '请求流水' },
  { to: '/observability/quality', label: '质量分析' },
]

export default function Layout() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">S</div>
          <div>
            <div className="brand-name">SSRM Ops</div>
            <div className="brand-sub">供应商系统运维 Agent</div>
          </div>
        </div>

        <nav className="side-nav">
          {MAIN_NAV.map(({ to, label, icon, hint }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `side-link ${isActive ? 'on' : ''}`}>
              <Icon name={icon} />
              <span className="side-text">
                <span className="side-label">{label}</span>
                <span className="side-hint">{hint}</span>
              </span>
            </NavLink>
          ))}

          <div className="side-group">
            <div className="side-group-h">
              <Icon name="obs" />
              可观测
            </div>
            {OBS_NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) => `side-sub ${isActive ? 'on' : ''}`}
              >
                {label}
              </NavLink>
            ))}
          </div>
        </nav>

        <div className="side-foot">
          <span className="dot" />
          本地开发 · 数据存进程内存
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
