import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { fetchMetrics, fetchTrace } from '../api.js'

/**
 * 可观测模块的共享状态。
 *
 * 拆页之后三个子页都要同一份 /metrics 快照。如果每页各自轮询：
 * - 切页会重新发请求，刚拉到的数据被丢掉；
 * - 停留久了就是三份定时器（只有一个在跑，另两个刚好被卸载，但切回来又各拉一次）。
 * 所以提到 shell 层拉一次，子页通过 context 取。
 *
 * 顺带把「链路抽屉」也放这里：抽屉是覆盖层，不属于任何一页，
 * 但入口（请求流水表）和展示（外壳）不在一处，只能共享。
 */

const Ctx = createContext(null)

export function MetricsProvider({ children }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [lastAt, setLastAt] = useState(null)
  const [refreshSec, setRefreshSec] = useState(5)
  const [copied, setCopied] = useState(null)

  // 抽屉：trace 是数据，state 是 'idle' | 'loading' | 错误文案
  const [trace, setTrace] = useState(null)
  const [traceState, setTraceState] = useState('idle')

  useEffect(() => {
    let alive = true
    const pull = async () => {
      try {
        const d = await fetchMetrics()
        if (!alive) return
        setData(d)
        setErr(null)
        setLastAt(new Date())
      } catch (e) {
        if (alive) setErr(e.message)
      }
    }
    pull()
    if (!refreshSec) return () => { alive = false }
    const timer = setInterval(pull, refreshSec * 1000)
    return () => { alive = false; clearInterval(timer) }
  }, [refreshSec])

  const openTrace = useCallback(async (tid) => {
    setTrace(null)
    setTraceState('loading')
    try {
      setTrace(await fetchTrace(tid))
      setTraceState('idle')
    } catch (e) {
      // 最常见的是 404：链路已滚出 100 条窗口，不是接口坏了
      setTraceState(e.message)
    }
  }, [])

  const closeTrace = useCallback(() => {
    setTrace(null)
    setTraceState('idle')
  }, [])

  const copy = useCallback(async (tid) => {
    try {
      await navigator.clipboard.writeText(tid)
      setCopied(tid)
      setTimeout(() => setCopied((c) => (c === tid ? null : c)), 1200)
    } catch {
      setCopied(null)
    }
  }, [])

  // ?trace=xxx 深链：值班时把链接发给同事，比「你去点那个 trace_id」靠谱
  // （流水 5 秒刷一次，手速跟不上）
  useEffect(() => {
    const tid = new URLSearchParams(window.location.search).get('trace')
    if (tid) openTrace(tid)
  }, [openTrace])

  return (
    <Ctx.Provider
      value={{
        data, err, lastAt, refreshSec, setRefreshSec,
        trace, traceState, openTrace, closeTrace, copied, copy,
      }}
    >
      {children}
    </Ctx.Provider>
  )
}

export function useMetrics() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useMetrics 必须在 MetricsProvider 内使用')
  return v
}
