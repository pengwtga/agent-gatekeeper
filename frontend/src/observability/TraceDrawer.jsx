import { fmtDur } from './ui.jsx'

/**
 * 链路抽屉：把一条请求的内部阶段画成瀑布条。
 *
 * 横轴是时间，条的左边缘 = 阶段开始时刻，宽度 = 持续时间。子阶段（rag.keyword）
 * 会落在父阶段（rag.retrieve）的区间里，靠缩进 + 位置就能看出嵌套关系，
 * 不需要画真正的树。
 */
export default function TraceDrawer({ trace, state, onClose, onCopy, copied }) {
  const spans = trace?.spans ?? []
  // 基准取「请求总耗时」和「最后一个 span 的结束时刻」的较大者：
  // 中间件计时包含序列化，正常会略大于 span 总跨度；取小了条会溢出右边界
  const total = Math.max(
    trace?.duration_ms || 0,
    ...spans.map((s) => s.start_ms + s.duration_ms),
    0.1
  )

  return (
    <div className="drawer-mask" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-h">
          <div>
            <div className="drawer-title">请求链路</div>
            {trace && <div className="mono dim">{trace.trace_id}</div>}
          </div>
          <button className="drawer-x" onClick={onClose} aria-label="关闭">×</button>
        </div>

        {state === 'loading' && <div className="hint">加载中…</div>}
        {state !== 'loading' && state !== 'idle' && <div className="warn">{state}</div>}

        {trace && (
          <>
            <div className="drawer-meta">
              <span className={`tag ${trace.status < 400 ? 't-hi' : trace.status < 500 ? 't-mid' : 't-lo'}`}>
                {trace.status}
              </span>
              <span className="mono">{trace.method} {trace.path}</span>
              <span className="dim">总耗时 {fmtDur(trace.duration_ms)}</span>
              <button className="mini" onClick={() => onCopy(trace.trace_id)}>
                {copied === trace.trace_id ? '已复制 ✓' : '复制 id'}
              </button>
            </div>

            {spans.length === 0 ? (
              <div className="hint">这次请求没有采集到 span</div>
            ) : (
              <>
                <div className="sub-h">阶段耗时（位置=开始时刻，宽度=耗时）</div>
                <div className="waterfall">
                  {spans.map((s, i) => {
                    // 缩进用后端给的 depth（真实嵌套层级），老数据没有该字段时退回数点号
                    const depth = s.depth ?? s.name.split('.').length - 1
                    const left = Math.min(100, (s.start_ms / total) * 100)
                    const width = Math.max((s.duration_ms / total) * 100, 0.8)
                    const tone =
                      s.status === 'error' ? 'bad'
                        : s.name.startsWith('llm') ? 'llm'
                          : s.name.startsWith('rag') ? 'rag' : 'base'
                    return (
                      <div className="wf-row" key={`${s.name}-${i}`}>
                        <span className="wf-name" style={{ paddingLeft: depth * 10 }} title={s.name}>{s.name}</span>
                        <div className="wf-track">
                          <i className={`wf-bar ${tone}`} style={{ left: `${left}%`, width: `${width}%` }} />
                        </div>
                        <span className="wf-dur">{fmtDur(s.duration_ms)}</span>
                      </div>
                    )
                  })}
                </div>

                <div className="sub-h">阶段属性</div>
                {spans.map((s, i) => (
                  <details key={`d-${s.name}-${i}`}>
                    <summary>
                      {s.name} · {fmtDur(s.duration_ms)}
                      {s.status === 'error' ? ' · 失败' : ''}
                    </summary>
                    <pre>{JSON.stringify(s.attrs, null, 2)}</pre>
                  </details>
                ))}
              </>
            )}

            <div className="drawer-foot">链路只保留最近 100 条，进程重启归零；更早的只能用 trace_id 去日志里查。</div>
          </>
        )}
      </aside>
    </div>
  )
}
