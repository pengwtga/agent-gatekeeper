import { useMemo, useState } from 'react'
import { useMetrics } from '../../observability/MetricsProvider.jsx'
import { fmtMs } from '../../observability/ui.jsx'

/**
 * 请求流水：一次请求一行，点 trace_id 下钻到各阶段耗时。
 * 拆出来单独一页，是因为表格需要纵向空间（50 行），和趋势图挤在一页必然要下滑。
 */
export default function Requests() {
  const { data, openTrace } = useMetrics()
  const [kw, setKw] = useState('')
  const [onlyFail, setOnlyFail] = useState(false)

  const rows = data?.recent_requests ?? []
  const shown = useMemo(() => {
    const q = kw.trim().toLowerCase()
    return rows.filter((r) => {
      if (onlyFail && r.status < 400) return false
      if (!q) return true
      return r.path.toLowerCase().includes(q) || r.trace_id.toLowerCase().includes(q)
    })
  }, [rows, kw, onlyFail])

  if (!data) return <div className="card hint">加载中…</div>

  return (
    <div className="card">
      <div className="card-h">
        <span>最近请求（{rows.length} 条，环形缓冲）</span>
        <span className="hint">点 trace_id 看这次请求的各阶段耗时</span>
      </div>

      <div className="filters">
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
        {(kw || onlyFail) && (
          <span className="hint">
            {shown.length} / {rows.length}
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        <div className="hint">还没有业务请求（探针请求不进这张表）</div>
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
                  <td className="mono dim">{r.ts}</td>
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
                  <td className="mono dim">{r.tokens || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

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
