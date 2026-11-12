import TrendChart from '../../components/TrendChart.jsx'
import { useMetrics } from '../../observability/MetricsProvider.jsx'
import { Kpi, axisMs, derive, fmtMs, fmtNum, fmtPct, toneOf } from '../../observability/ui.jsx'

/**
 * 概览：只回答「现在健不健康、从什么时候开始不健康」。
 * 明细（哪条请求、哪类失败）都在另外两页，避免一屏塞太多需要下滑。
 */
export default function Overview() {
  const { data } = useMetrics()
  if (!data) return <div className="card hint">加载中…</div>

  const { req, lat, points, business, errorRate, degradeRate } = derive(data)
  const lastPoint = points[points.length - 1]

  return (
    <>
      <div className="kpi-grid">
        <Kpi
          label="请求总数"
          value={fmtNum(req.total)}
          sub={`ok ${req.ok} · 4xx ${req.client_error} · 5xx ${req.server_error}`}
        />
        <Kpi
          label="当前 QPS"
          value={lastPoint?.qps ?? 0}
          unit="/s"
          sub={`本分钟 ${lastPoint?.count ?? 0} 次`}
        />
        <Kpi
          label="P95 延迟"
          value={fmtMs(lat.p95)}
          sub={`P50 ${fmtMs(lat.p50)} · P99 ${fmtMs(lat.p99)}`}
          tone={toneOf(lat.p95, 4000, 8000)}
        />
        <Kpi
          label="错误率"
          value={fmtPct(errorRate, 2)}
          sub={`${req.client_error + req.server_error} 笔失败 / ${business} 笔业务请求`}
          tone={toneOf(errorRate, 0.005, 0.02)}
        />
        <Kpi
          label="降级率"
          value={fmtPct(degradeRate)}
          sub={`${data.degrade.total} / ${data.degrade.denominator} 笔业务请求`}
          tone={toneOf(degradeRate, 0.05, 0.2)}
        />
        <Kpi
          label="近 1h token"
          value={fmtNum(data.tokens.last_hour.total)}
          sub={`${data.tokens.last_hour_calls} 次调用 · 均 ${data.tokens.avg_per_call}/次`}
        />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-h">
            <span>请求量</span>
            <span className="hint">每分钟请求数</span>
          </div>
          <TrendChart
            points={points}
            lines={[{ key: 'count', name: '请求数', color: '#2456e6', fill: true }]}
            unit="次/分钟"
            yFormat={(v) => fmtNum(Math.round(v))}
          />
        </div>

        <div className="card">
          <div className="card-h">
            <span>延迟</span>
            <span className="hint">均值会被慢请求拖高，max 看尖刺</span>
          </div>
          <TrendChart
            points={points}
            lines={[
              { key: 'lat_avg', name: '平均', color: '#2456e6' },
              { key: 'lat_max', name: '最大', color: '#e8590c' },
            ]}
            unit="ms"
            yFormat={axisMs}
          />
        </div>

        <div className="card">
          <div className="card-h">
            <span>错误率</span>
            <span className="hint">按分钟算，异常时先看这根线起来的时刻</span>
          </div>
          <TrendChart
            points={points.map((p) => ({ ...p, err_pct: +(p.error_rate * 100).toFixed(2) }))}
            lines={[{ key: 'err_pct', name: '错误率', color: '#c92a2a', fill: true }]}
            unit="%"
            yFormat={(v) => `${v.toFixed(0)}%`}
            emptyText="近 1 小时没有错误请求"
          />
        </div>

        <div className="card">
          <div className="card-h">
            <span>降级率</span>
            <span className="hint">降级 = 没走通 LLM，退回规则基线</span>
          </div>
          <TrendChart
            points={points.map((p) => ({ ...p, deg_pct: +(p.degrade_rate * 100).toFixed(2) }))}
            lines={[{ key: 'deg_pct', name: '降级率', color: '#e8590c', fill: true }]}
            unit="%"
            yFormat={(v) => `${v.toFixed(0)}%`}
            emptyText="近 1 小时没有降级"
          />
        </div>
      </div>

      <div className="callout">
        <b>怎么看这一页</b>
        <span>
          先看降级率和 P95：降级率起来说明能力在退（LLM 不可用 → 退回规则），延迟跟着涨说明在等超时。
          错误率是结果，前面两个是原因。逐条定位去「请求流水」，失败类型去「质量分析」。
        </span>
      </div>
    </>
  )
}
