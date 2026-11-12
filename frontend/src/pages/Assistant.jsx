import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Header from '../components/Header.jsx'
import { ConfTag, modeName } from '../components/Tags.jsx'
import { assist, kbStats } from '../api.js'

const PRESETS = [
  { ico: '📮', label: '审批通过但没生成任务', q: '审批已经通过了，但 SSRM 里一直没生成注册任务，等了一个小时了' },
  { ico: '⏱️', label: 'EA 同步一直失败', q: '调用 EA 的 updateSupplier 接口一直超时，重试了 4 次还是失败' },
  { ico: '🔁', label: '怀疑重复提交', q: '供应商说提交注册时报错申请已存在，怀疑是重复提交' },
  { ico: '🔎', label: 'Dora 查不到联系人', q: '在 Dora 上查不到这个供应商的联系人信息' },
]

const WELCOME = {
  role: 'bot',
  intro: true,
}

export default function Assistant() {
  const [params] = useSearchParams()
  const [messages, setMessages] = useState([WELCOME])
  const [question, setQuestion] = useState('')
  const [pending, setPending] = useState(false)
  const [kb, setKb] = useState(null)
  const bottomRef = useRef(null)

  // 知识库状态：条数 + 当前检索模式（向量 / 关键词+重排）
  useEffect(() => {
    kbStats().then(setKb).catch(() => {})
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  const send = async (text) => {
    const q = (text ?? question).trim()
    if (!q || pending) return
    setQuestion('')
    setPending(true)
    setMessages((prev) => [...prev, { role: 'user', text: q }, { role: 'bot', loading: true }])
    try {
      const data = await assist(q)
      setMessages((prev) => [...prev.slice(0, -1), { role: 'bot', data }])
    } catch (e) {
      setMessages((prev) => [...prev.slice(0, -1), { role: 'bot', error: e.message }])
    } finally {
      setPending(false)
    }
  }

  // ?demo=1 自动问一个预设问题（README 截图 / 面试演示用）
  // 必须用 ref 去重：StrictMode 在开发态会把 effect 执行两次，
  // 否则同一句话会被发两遍，页面上出现两条一模一样的提问（还白烧一次 LLM 配额）
  const demoRan = useRef(false)
  useEffect(() => {
    if (demoRan.current) return
    demoRan.current = true
    if (params.has('demo')) send(PRESETS[0].q)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="wrap">
      <Header
        title="SSRM 值班助手"
        desc="值班 oncall 时用一句话描述问题，自动检索相似历史工单并给出处置建议"
      >
        {kb && (
          <span className="tag t-mode" style={{ marginTop: 8 }}>
            知识库 {kb.tickets} 条 · {modeName(kb.mode)}
          </span>
        )}
      </Header>

      <div className="chips">
        {PRESETS.map((p) => (
          <button key={p.label} type="button" onClick={() => send(p.q)} disabled={pending}>
            <span className="ico">{p.ico}</span>
            <span>{p.label}</span>
          </button>
        ))}
      </div>

      <div className="chat" style={{ marginTop: 16 }}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="avatar">{m.role === 'user' ? '🧑‍💻' : '🤖'}</div>
            <div className="bubble">
              {m.intro && <Intro />}
              {m.text}
              {m.loading && <span className="typing">正在检索历史工单并生成建议…（约 5~10 秒）</span>}
              {m.error && <span className="err">{m.error}</span>}
              {m.data && <Answer data={m.data} />}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="inputbar">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') send()
          }}
          placeholder="输入你遇到的问题，回车发送…"
        />
        <button type="button" onClick={() => send()} disabled={pending}>
          发送
        </button>
      </div>
    </div>
  )
}

function Intro() {
  return (
    <>
      你好！我是 SSRM 值班助手。遇到线上问题直接用一句话描述（也可以点上面的案例卡片），我会：
      <br />
      1. 检索最相似的历史工单
      <br />
      2. 结合 LLM 给出处置建议
      <br />
      <span className="hint">置信度低于 55% 时会明确提示转人工，不硬答。</span>
    </>
  )
}

function Answer({ data }) {
  // 后端约定：/assist 的失败信息放在 body.error 里且仍返回 200，需要单独拦一次
  if (data.error) return <span className="err">{data.error}</span>

  const answerText = typeof data.answer === 'string' ? data.answer : '（无建议）'
  const lowConfidence = data.confidence != null && data.confidence < 0.55

  return (
    <>
      <div className="meta">
        <span className="tag t-mode">检索: {modeName(data.mode)}</span>
        {data.llm_provider && <span className="tag t-llm">LLM: {data.llm_provider}</span>}
        <ConfTag confidence={data.confidence} />
      </div>

      <div className="sec">处置建议</div>
      <div className="advice">{answerText}</div>

      {lowConfidence && (
        <div className="warn">⚠️ 相似度低于采信阈值（0.55），以上建议仅供参考，请人工判断。</div>
      )}

      {data.cases?.length ? (
        <>
          <div className="sec">相似历史工单（LLM 暂不可用，返回原始 case）</div>
          {data.cases.map((c) => (
            <div className="case" key={c.ticket_id}>
              <b className="tid2">{c.ticket_id}</b> · {c.title}
              <br />
              <span className="k">现象：</span>
              {c.symptom}
              <br />
              <span className="k">处置：</span>
              {c.resolution}
            </div>
          ))}
        </>
      ) : data.sources?.length ? (
        <>
          <div className="sec">相似历史工单 Top {data.sources.length}</div>
          {data.sources.map((s) => {
            const pct = Math.round((s.score || 0) * 100)
            const hasDetail = Boolean(data.sources[0].title)
            return (
              <div className="src" key={s.ticket_id}>
                <span className="tid">{s.ticket_id}</span>
                {hasDetail && (
                  <>
                    <span className="title">{s.title}</span>
                    <span className="tag t-cls">{s.root_cause_class}</span>
                  </>
                )}
                <div className="bar">
                  <i style={{ width: `${pct}%` }} />
                </div>
                <span className="pct">{pct}%</span>
              </div>
            )
          })}
        </>
      ) : (
        <div className="hint" style={{ marginTop: 10 }}>
          （未检索到相似工单，建议升级人工并把 case 沉淀进知识库）
        </div>
      )}
    </>
  )
}
