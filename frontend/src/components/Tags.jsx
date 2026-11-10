// 置信度徽标：三档配色（0.85 / 0.70）。
// 注意：这里只是展示配色，不是门禁判定 —— 判定唯一来源是后端的 needs_human_review 字段
export function ConfTag({ confidence }) {
  if (confidence == null) return null
  const cls = confidence >= 0.85 ? 't-hi' : confidence >= 0.7 ? 't-mid' : 't-lo'
  return <span className={`tag ${cls}`}>置信度 {Math.round(confidence * 100)}%</span>
}

// 根因类别英文 key → 中文名
export const CLASS_NAME = {
  xflow_callback_lost: '审批回调丢失',
  ea_sync_timeout: 'EA 同步超时',
  duplicate_submit: '重复提交',
  data_inconsistency: '数据不一致',
  frontend_param_error: '前端参数错误',
  unknown: '未知（转人工）',
}

// 检索模式英文 key → 中文名
export function modeName(mode) {
  if (mode === 'vector') return '向量检索'
  if (mode === 'keyword+rerank') return '关键词+LLM重排'
  return mode || '-'
}
