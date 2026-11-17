import { useEffect, useMemo, useState } from 'react'
import { useMetrics } from '../../observability/MetricsProvider.jsx'
import { fmtMs } from '../../observability/ui.jsx'
import { fetchTraces } from '../../api.js'

/**
 * 请求流水：一次请求一行，点 trace_id 下钻到各阶段耗时。
 * 拆出来单独一页，是因为表格需要纵向空间（50 行），和趋势图挤在一页必然要下滑。
 *
 * 两个数据源（T2-05 之后）：
 * - live：/metrics 里的环形缓冲，50 条，进程内，秒级刷新；
 * - history：/traces 查 SQLite，能查到重启前、滚出内存窗口之外的请求，保留 7 天。
 * 分开而不是合并，是因为两者语义不同：前者回答「刚才怎么样」，后者回答「昨天那批怎么样」。
 */
export default function Requests() {
  const { data, openTrace } = useMetrics()
  const [kw, setKw] = useState('')
  const [onlyFail, setOnlyFail] = useState(false)
  const [source, setSource] = useState(
    // ?source=history 深链：把「昨天那批慢请求」的视图直接发给同事，省一次点击
    () => (new URLSearchParams(window.location.search).get('source') === 'history' ? 'history' : 'live'),
  )
  const [limit, setLimit] = useState(50)

  const [hist, setHist] = useState(null)
  const [histErr, setHistErr] = useState(null)

  // 历史模式按需拉取：只在切到这个 tab 时查库，避免每次轮询都扫一遍 SQLite
  useEffect(() => {
    if (source !== 'history') return undefined
    let alive = true
    setHist(null)
    setHistErr(null)
    fetchTraces({ limit, only_errors: onlyFail || undefined })
      .then((d) => { if (alive) setHist(d.traces ?? []) })
      .catch((e) => { if (alive) setHistErr(e.message) })
    return () => { alive = false }
  }, [source, limit, onlyFail])

  const rows = source === 'history' ? (hist ?? []) : (data?.recent_requests ?? [])
  const shown = useMemo(() => {
    const q = kw.trim().toLowerCase()
    return rows.filter((r) => {
      if (onlyFail && r.status < 400) return false
      if (!q) return true
      return r.path.toLowerCase().includes(q) || r.trace_id.toLowerCase().includes(q)
    })
  }, [rows, kw, onlyFail])

  if (source === 'live' && !data) return <div className="card hint">加载中…</div>

  const title = source === 'live'
    ? `最近请求（${rows.length} 条，环形缓冲）`
    : `历史链路（${rows.length} 条，SQLite，保留 7 天）`

  return (
    <div className="card">
      <div className="card-h">
        <span>{title}</span>
        <span className="hint">点 trace_id 看这次请求的各阶段耗时</span>
      </div>

      <div className="filters">
        <div className="seg">
          <button
            className={`segbtn ${source === 'live' ? 'on' : ''}`}
            onClick={() => setSource('live')}
            title="进程内环形缓冲，秒级刷新"
          >
            实时
          </button>
          <button
            className={`segbtn ${source === 'history' ? 'on' : ''}`}
            onClick={() => setSource('history')}
            title="查 SQLite，含重启前的记录"
          >
            历史
          </button>
        </div>

        <input
          className="filter-input"
          value={kw}
          onChange={(e) => setKw(e.target.value)}
          placeholder="按路径或 trace_id 过滤…"
        />
        <label className="filter-check">
          <input type="checkbox" checked={onlyFail} onChange={(e) => setOnlyFail(e.target.checked)} />
          只看失败（4xx/5xx）
        </label>
        {source === 'history' && (
          <select
            className="filter-input"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            title="一次取多少条"
          >
            {[20, 50, 100, 200].map((n) => <option key={n} value={n}>最近 {n} 条</option>)}
          </select>
        )}
        {(kw || onlyFail) && (
          <span className="hint">
            {shown.length} / {rows.length}
          </span>
        )}
      </div>

      {histErr && <div className="hint">查询失败：{histErr}</div>}

      {source === 'history' && hist === null && !histErr ? (
        <div className="hint">查询中…</div>
      ) : rows.length === 0 ? (
        <div className="hint">
          {source === 'history' ? '这个条件下没有历史记录' : '还没有业务请求（探针请求不进这张表）'}
        </div>
      ) : shown.length === 0 ? (
        <div className="hint">没有匹配的请求</div>
      ) : (
        <div className="table-scroll tall">
          <table className="req-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>trace_id</th>
                <th>方法</th>
                <th>路径</th>
                <th>状态</th>
                <th>耗时</th>
                <th>token</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                <tr key={`${r.trace_id}-${i}`}>
                  <td className="mono dim">{source === 'history' ? (r.ts_iso || r.ts) : r.ts}</td>
                  <td>
                    <button className="tidbtn" onClick={() => openTrace(r.trace_id)} title="查看链路">
                      {r.trace_id}
                    </button>
                  </td>
                  <td className="dim">{r.method}</td>
                  <td className="mono">{r.path}</td>
                  <td>
                    <span className={`tag ${r.status < 400 ? 't-hi' : r.status < 500 ? 't-mid' : 't-lo'}`}>
                      {r.status}
                    </span>
                  </td>
                  <td className="mono">{fmtMs(r.duration_ms)}</td>
                  <td className="mono dim">{r.tokens ?? r.usage?.total_tokens ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="callout">
        <b>实时和历史差在哪</b>
        <span>
          「实时」读 /metrics 的环形缓冲（50 条、进程内、重启清空），用来回答「刚才怎么样」；
          「历史」查 SQLite（保留 7 天），能查到重启前和滚出内存窗口之外的请求。
          点 trace_id 走同一个详情接口：内存优先、DB 兜底，所以两边的链路都能打开。
        </span>
      </div>

      <div className="callout">
        <b>为什么这里看不到 /health</b>
        <span>
          探针、<code>/metrics</code>、<code>/traces/*</code> 都只服务于「看」这个动作，不进业务流水，
          否则点开一条链路，列表第一条就会变成「查这条链路的那次请求」。
        </span>
      </div>
    </div>
  )
}
