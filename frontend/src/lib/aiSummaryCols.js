/* ================================================================
   开标汇总表列定义：默认列（字段字典未就绪时兜底）+ 格式化工具
   列本身从后端 ai_field_template 动态生成，本文件只管显示口径
   ================================================================ */
export const FALLBACK_COLS = [
  { key: 'bidder_name', label: '投标人名称', type: 'text', width: '18%' },
  { key: 'bid_price', label: '投标报价（元）', type: 'money', width: '13%' },
  { key: 'duration', label: '工期/服务期', type: 'text', width: '13%' },
  { key: 'deposit', label: '投标保证金（元）', type: 'money', width: '12%' },
  { key: 'qualification', label: '资格响应', type: 'text', width: '9%' },
  { key: 'remark', label: '备注', type: 'text', width: '20%' },
]

export function colsWithWidth(cols) {
  const base = (cols && cols.length) ? cols : FALLBACK_COLS
  const w = Math.floor(100 / Math.max(1, base.length))
  return base.map((c, i) => ({
    ...c,
    width: c.width || (i === 0 ? (w + 6) + '%' : w + '%'),
  }))
}

export function fmtCell(v, type) {
  if (v === null || v === undefined || v === '') return '—'
  if (type === 'money') {
    const n = Number(String(v).replace(/,/g, ''))
    if (!isFinite(n)) return String(v)
    return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  }
  return String(v)
}

export function fmtTime(t) {
  if (!t) return '—'
  return new Date(Number(t) * 1000).toLocaleString('zh-CN', { hour12: false })
}

export const STAGE_LABEL = {
  QUEUED: '等待 Worker 领取', CLAIMED: 'Worker 已领取', SCANNING: '扫描并物化招标文件',
  PARSING: '解析文件内容', GROUPING: '归集投标人与标段', EXTRACTING: '模型理解并抽取字段',
  VALIDATING: '规则校验与价格分', SAVING: '写入标准化数据库', DONE: '完成',
  FAILED: '失败', CANCELED: '已取消', CANCEL_REQUESTED: '正在取消',
}
