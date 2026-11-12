import { useMemo } from 'react'
import { useMetrics } from '../../observability/MetricsProvider.jsx'
import { Bars, derive, fmtPct } from '../../observability/ui.jsx'

/**
 * 质量分析：回答「模型/链路退化的具体形态」——
 * 是没配 key、超时、还是 JSON 解析不出来；置信度落在哪一档。
 * 这一页不关心「快不快」，只关心「准不准、靠不靠得住」。
 */
export default function Quality() {
  const { data } = useMetrics()

  const confItems = useMemo(() => {
    const hist = data?.confidence_hist ?? {}
    return Object.keys(hist)
      .sort((a, b) => Number(a) - Number(b))
      .map((k) => ({
        name: `${Number(k).toFixed(1)}–${(Number(k) + 0.1).toFixed(1)}`,
        value: hist[k],
        // 置信度靠低端要显眼：<0.7 的样本是人工复核压力的直接来源
        color: Number(k) < 0.7 ? '#e8590c' : Number(k) < 0.85 ? '#f0a020' : '#2b8a3e',
      }))
  }, [data])

  if (!data) return <div className="card hint">加载中…</div>

  const degradeItems = Object.entries(data.degrade.by_level ?? {}).map(([name, value]) => ({
    name,
    value,
    color: name === 'none' ? '#2b8a3e' : '#c92a2a',
  }))
  const jsonItems = Object.entries(data.json.fail_by_kind ?? {}).map(([name, value]) => ({
    name,
    value,
    color: '#c92a2a',
  }))
  const recoveredItems = Object.entries(data.json.recovered_by_kind ?? {}).map(([name, value]) => ({
    name,
    value,
    color: '#0b7285',
  }))
  const cls = data.classification ?? { decisions: 0, unknown_count: 0, unknown_rate: 0, by_class: {} }
  const classItems = Object.entries(cls.by_class).map(([name, value]) => ({
    name,
    value,
    color: name === 'unknown' ? '#e8590c' : '#3b5bdb',
  }))

  const { degradeRate } = derive(data)

  return (
    <>
      <div className="grid-2">
        <div className="card">
          <div className="card-h">
            <span>降级构成</span>
            <span className="hint">当前降级率 {fmtPct(degradeRate)}</span>
          </div>
          <Bars items={degradeItems} />
          <div className="callout in-card">
            <span>
              <code>none</code> 是正常走通；<code>not_configured</code> 是没配 key；
              <code>provider_error</code> 是三家都试过并失败了；<code>budget_exhausted</code> 是时间预算用完主动放弃。
              四者处置完全不同：前两个查配置，后两个查上游。
            </span>
          </div>
        </div>

        <div className="card">
          <div className="card-h">
            <span>置信度分布</span>
            <span className="hint">低于 0.7 是人工复核的主要来源</span>
          </div>
          <Bars
            items={confItems}
            emptyText="规则基线通道不产出置信度——只有 LLM 通道才有，配好 API Key 后这里开始有数"
          />
          <div className="callout in-card">
            <span>
              低于 0.35 会被证据门禁压到 0.4 以内并强制转人工；0.35~0.85 之间是「有依据但不够确定」，
              建议人工确认；0.85 以上才允许按建议自动处置。
            </span>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <span>根因分类分布</span>
          <span className="hint">共 {cls.decisions} 次判定 · unknown 率 {fmtPct(cls.unknown_rate)}</span>
        </div>
        <Bars items={classItems} emptyText="还没有分类请求" />
        <div className="callout in-card">
          <span>
            <code>unknown</code> 不是模型坏——它是分类体系的「漏斗入口」：同一模式反复落入 unknown，
            说明出现了 5 类之外的新故障模式，是新增根因类别的第一手信号。
            模型质量看上面的降级率，体系覆盖率看这里的 unknown 率，两者要分开归因。
          </span>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <span>JSON 解析健康度</span>
          <span className="hint">失败不可怕，救不回来才可怕</span>
        </div>
        <div className="grid-2">
          <div>
            <div className="sub-h">解析失败（{data.json.fail_total}）</div>
            <Bars items={jsonItems} emptyText="没有解析失败" />
          </div>
          <div>
            <div className="sub-h">宽松提取救回</div>
            <Bars items={recoveredItems} color="#0b7285" emptyText="没有触发降级解析" />
          </div>
        </div>
        <div className="callout in-card">
          <span>
            小模型经常返回 ```json 包裹或前后带解释文字。失败按类型分开计数，是为了区分「模型不会按格式输出」
            和「输出被截断」——前者调 prompt，后者调 <code>max_tokens</code>。
          </span>
        </div>
      </div>
    </>
  )
}
