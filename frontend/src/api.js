// 统一请求层：所有接口都走 /api 前缀（dev 由 Vite 代理，生产由 nginx 反代）
// 把「错误归一化」收在这里，页面组件只管拿数据或拿到一个可展示的 message
const BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function request(path, options = {}) {
  let res
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (e) {
    throw new Error(`网络异常：${e.message}`)
  }

  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = data?.detail ?? data?.error ?? `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return data
}

export const classify = (incident) =>
  request('/classify', { method: 'POST', body: JSON.stringify({ incident }) })

export const assist = (question) =>
  request('/assist', { method: 'POST', body: JSON.stringify({ question }) })

export const kbStats = () => request('/kb/stats')

export const health = () => request('/health')

// 指标快照：含累计计数、滑动窗口分位数、60 分钟时序、最近 50 条请求流水
export const fetchMetrics = () => request('/metrics')

// 单次请求链路：各阶段 span 与耗时。查不到时后端返回 404（已滚出窗口）
export const fetchTrace = (traceId) => request(`/traces/${encodeURIComponent(traceId)}`)
