/** 可观测模块的公共格式化与小组件（拆页后三页共用） */

export const fmtMs = (v) => (v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`)
// 轴刻度单独一套：0.5 / 0.25 / 0 用 fmtMs 会被四舍五入成 "1ms/0ms/0ms" 三个标签两个重复
export const axisMs = (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : v >= 10 ? `${Math.round(v)}` : v > 0 ? v.toFixed(1) : '0')
// span 粒度常是亚毫秒（关键词召回 0.1ms），整数格式化会把它们全显示成 0ms
export const fmtDur = (v) => (v >= 1000 ? `${(v / 1000).toFixed(2)}s` : v >= 10 ? `${Math.round(v)}ms` : `${v.toFixed(1)}ms`)
export const fmtPct = (v, digits = 1) => `${(v * 100).toFixed(digits)}%`
export const fmtNum = (v) => (v >= 10000 ? `${(v / 1000).toFixed(1)}k` : String(v))

// 阈值只用来上色，不用来决定要不要报警——单点阈值报警是下一步的事
export const toneOf = (v, warn, danger) => (v >= danger ? 'bad' : v >= warn ? 'warn' : 'good')

export function Kpi({ label, value, unit, sub, tone }) {
  return (
    <div className={`kpi ${tone ?? ''}`}>
      <div className="kpi-k">{label}</div>
      <div className="kpi-v">
        {value}
        {unit && <span>{unit}</span>}
      </div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

/** 横向条形组：降级构成 / JSON 失败类型 / 置信度分布都用它 */
export function Bars({ items, color = '#2456e6', emptyText = '暂无数据' }) {
  const max = Math.max(...items.map((i) => i.value), 0)
  if (!items.length || max === 0) return <div className="hint">{emptyText}</div>
  return (
    <div className="bars">
      {items.map((i) => (
        <div className="bar-row" key={i.name}>
          <span className="bar-k" title={i.name}>{i.name}</span>
          <div className="bar-track">
            <i style={{ width: `${(i.value / max) * 100}%`, background: i.color || color }} />
          </div>
          <span className="bar-v">{i.value}</span>
        </div>
      ))}
    </div>
  )
}

/** 从 /metrics 快照里算出三页都要用到的派生值 */
export function derive(data) {
  const req = data?.requests
  // 分母扣掉 /health、/metrics、/traces 这类探针：它们永远 200，混进来会让错误率虚低
  const business = req ? Math.max(0, req.total - req.excluded_from_latency) : 0
  return {
    req,
    lat: data?.latency_ms,
    points: data?.series?.points ?? [],
    business,
    errorRate: business && req ? (req.client_error + req.server_error) / business : 0,
    degradeRate: data?.degrade?.rate ?? 0,
  }
}
