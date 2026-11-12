/**
 * 零依赖折线图：手写 SVG，不引 echarts/recharts。
 *
 * 为什么不上图表库：这里只需要「折线 + 面积 + 三根网格线 + 轴标签」，
 * 而 echarts 会往包里塞 300KB+（gzip 后也有 100KB），页面首屏为它多等一秒不值得。
 * 什么时候该换：要缩放、要框选、要几十个序列叠加时，手写 SVG 会失控，那时候换库是对的。
 *
 * 坐标系统：viewBox 固定 420×150，靠父容器宽度整体缩放。
 * 卡片内每列约 400px，缩放比接近 1:1，10px 的字号在屏幕上就是 10px。
 */
const W = 420
const H = 150
const PAD = { l: 36, r: 8, t: 10, b: 20 }

function niceMax(raw) {
  // 全零时给个 1，避免除零把整条线算成 NaN 直接不画
  if (!raw || raw <= 0) return 1
  const pow = 10 ** Math.floor(Math.log10(raw))
  const n = raw / pow
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
  return step * pow
}

export default function TrendChart({ points = [], lines = [], unit = '', yFormat = (v) => v, emptyText = '暂无数据' }) {
  const n = points.length
  const allValues = lines.flatMap((l) => points.map((p) => Number(p[l.key]) || 0))
  const yMax = niceMax(Math.max(...allValues, 0))
  const hasData = allValues.some((v) => v > 0)

  const innerW = W - PAD.l - PAD.r
  const innerH = H - PAD.t - PAD.b
  const x = (i) => PAD.l + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW)
  const y = (v) => PAD.t + innerH - (Math.max(0, Math.min(v, yMax)) / yMax) * innerH

  const pathOf = (key) =>
    points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(Number(p[key]) || 0).toFixed(1)}`).join(' ')

  const areaOf = (key) =>
    `${pathOf(key)} L${x(n - 1).toFixed(1)},${PAD.t + innerH} L${x(0).toFixed(1)},${PAD.t + innerH} Z`

  // X 轴只标 4 个点：60 个都标会糊成一片
  const tickIdx = n ? [0, Math.floor(n / 3), Math.floor((2 * n) / 3), n - 1] : []

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img">
        {[0, 0.5, 1].map((r) => (
          <g key={r}>
            <line
              x1={PAD.l} x2={W - PAD.r}
              y1={PAD.t + innerH * r} y2={PAD.t + innerH * r}
              stroke="#eef0f4" strokeWidth="1"
            />
            <text x={PAD.l - 6} y={PAD.t + innerH * r + 3.5} textAnchor="end" fontSize="9" fill="#9aa2ae">
              {yFormat(yMax * (1 - r))}
            </text>
          </g>
        ))}

        {/* 面积先画、折线后画：反过来的话面积会盖住自己的描边 */}
        {lines.map((l) =>
          l.fill && hasData ? (
            <path key={`a-${l.key}`} d={areaOf(l.key)} fill={l.color} opacity="0.09" />
          ) : null
        )}
        {lines.map((l) => (
          <path
            key={`l-${l.key}`} d={pathOf(l.key)} fill="none"
            stroke={l.color} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round"
          />
        ))}

        {tickIdx.map((i) => (
          <text key={i} x={x(i)} y={H - 6} textAnchor="middle" fontSize="9" fill="#9aa2ae">
            {points[i]?.label ?? ''}
          </text>
        ))}

        {!hasData && (
          <text x={W / 2} y={PAD.t + innerH / 2} textAnchor="middle" fontSize="11" fill="#b6bcc6">
            {emptyText}
          </text>
        )}
      </svg>

      <div className="legend">
        {lines.map((l) => (
          <span key={l.key}>
            <i style={{ background: l.color }} />
            {l.name}
          </span>
        ))}
        {unit && <em>单位：{unit}</em>}
      </div>
    </div>
  )
}
