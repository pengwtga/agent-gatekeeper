import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Header from '../components/Header.jsx'
import { CLASS_NAME, ConfTag } from '../components/Tags.jsx'
import { classify } from '../api.js'

// 预置案例：purpose 是让人 3 秒内看懂「这个 Agent 解决什么问题」
const CASES = {
  callback: {
    ico: '📮',
    label: '案例：审批通过但没生成任务',
    incident: {
      stage: 'register',
      source_system: 'xflow',
      error_code: null,
      error_message: '审批单 FT20261025088 状态为 APPROVED 但 SSRM 无对应注册任务',
      wait_minutes: 55,
      retry_count: 0,
      downstream: 'ssrm',
      http_status: null,
    },
  },
  ea: {
    ico: '⏱️',
    label: '案例：EA 同步一直超时',
    incident: {
      stage: 'change',
      source_system: 'ssrm',
      error_code: 'EA_TIMEOUT',
      error_message: '调用 EA updateSupplier 超时 5s',
      wait_minutes: 3,
      retry_count: 4,
      downstream: 'ea',
      http_status: 504,
    },
  },
  dup: {
    ico: '🔁',
    label: '案例：重复提交冲突',
    incident: {
      stage: 'register',
      source_system: 'dora',
      error_code: null,
      error_message: '同一供应商 10 秒内提交了两次注册，第二次提示申请已存在',
      wait_minutes: 0,
      retry_count: 0,
      downstream: 'ssrm',
      http_status: 409,
    },
  },
  hard: {
    ico: '🧩',
    label: '案例：口语化困难样本',
    incident: {
      stage: 'register',
      source_system: 'portal',
      error_code: 'JOB_STUCK',
      error_message: '用户反馈提交后页面一直显示审批中，但后台查审批记录其实早已通过',
      wait_minutes: 95,
      retry_count: 0,
      downstream: 'ssrm',
      http_status: null,
    },
  },
}

const emptyPayload = () => JSON.stringify({ incident: CASES.callback.incident }, null, 2)

export default function Playground() {
  const [params] = useSearchParams()
  const [payload, setPayload] = useState(emptyPayload)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  const fill = (key) => {
    setPayload(JSON.stringify({ incident: CASES[key].incident }, null, 2))
    setResult(null)
    setError('')
  }

  const run = async (raw = payload) => {
    if (loading) return
    let body
    try {
      body = JSON.parse(raw)
    } catch (e) {
      setError(`JSON 解析失败：${e.message}`)
      return
    }
    setLoading(true)
    setError('')
    setResult(null)
    try {
      setResult(await classify(body.incident))
    } catch (e) {
      setError(`请求失败：${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  // ?demo=1 自动跑一个预设案例（README 截图 / 面试现场演示用）
  // ref 去重：StrictMode 开发态会执行两次 effect，否则会重复分类并浪费一次 LLM 调用
  const demoRan = useRef(false)
  useEffect(() => {
    if (demoRan.current) return
    demoRan.current = true
    if (params.has('demo')) run(JSON.stringify({ incident: CASES.callback.incident }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="wrap">
      <Header
        title="根因分类"
        desc="线上由 SSRM 卡单页在供应商求助时调用同一接口；这里是不接上游的手动入口——粘贴一条事件上下文，就能看到根因、置信度、判断依据与处置建议"
      />

      <div className="chips">
        {Object.entries(CASES).map(([key, c]) => (
          <button key={key} type="button" onClick={() => fill(key)}>
            <span className="ico">{c.ico}</span>
            <span>{c.label}</span>
          </button>
        ))}
      </div>

      <div className="card">
        <div className="sec">事件上下文 JSON（可编辑）</div>
        <textarea
          value={payload}
          onChange={(e) => setPayload(e.target.value)}
          placeholder='{"incident": {...}}'
        />
        <div className="actions">
          <button className="primary" disabled={loading} onClick={() => run()}>
            {loading ? '分类中…' : '开始分类'}
          </button>
          <span className="hint">
            LLM 通道约 5~10 秒 · 字段含义：stage 阶段 / source_system 来源 / wait_minutes 已等待分钟数 /
            retry_count 重试次数
          </span>
        </div>
      </div>

      <div className="card">
        {error && <span className="err">{error}</span>}
        {!error && !result && (
          <span className="hint">
            {loading
              ? '分类中…（LLM 通道约 5~10 秒）'
              : '点击上方案例卡片，或粘贴自己的 incident JSON，然后点「开始分类」'}
          </span>
        )}
        {result && <Result data={result} />}
      </div>
    </div>
  )
}

function Result({ data }) {
  const confidence = data.confidence ?? 0
  const cls = data.root_cause_class || 'unknown'
  // 门禁判定唯一来源是后端（needs_human_review 由 classifier 统一计算）。
  // 前端不再兜底计算 —— 判定逻辑复制两份，改一处忘一处是必然事故。
  // 字段缺失时按「需要人工」处理（fail-safe），绝不在前端猜一个放行。
  const needsReview = data.needs_human_review !== false

  return (
    <>
      <div className="meta">
        <span className="tag t-cls">{cls}</span>
        {data.source === 'llm' ? (
          <span className="tag t-llm">通道: LLM · {data.llm_provider || '?'}</span>
        ) : (
          <span className="tag t-mode">通道: 规则基线</span>
        )}
        <ConfTag confidence={confidence} />
      </div>

      <div className="clsname">{CLASS_NAME[cls] || cls}</div>

      <div className="crow">
        <div className="rbar">
          <i style={{ width: `${Math.round(confidence * 100)}%` }} />
        </div>
        <span className="rpct">{Math.round(confidence * 100)}%</span>
      </div>

      <div className="sec">判断依据</div>
      <div className="advice">{data.evidence || '（无）'}</div>

      <div className="sec">处置建议</div>
      <div className="advice">{data.suggested_action || '（无）'}</div>

      {needsReview ? (
        <div className="warn">⚠️ 置信度未达自动处置门禁，建议人工确认后再执行处置</div>
      ) : (
        <div className="okv">✅ 置信度达到自动处置门禁，可按建议执行处置</div>
      )}

      <details>
        <summary>查看原始 JSON</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    </>
  )
}
