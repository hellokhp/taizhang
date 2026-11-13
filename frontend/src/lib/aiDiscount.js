/* ================================================================
   模式 D（下浮率 / 折扣率报价）结果适配层

   数据源：ai_summary.raw_result.discount —— AI 工具模式 D 的结构化产出。
   后端已把 raw_result 完整返回，过去前端只按通用 BIDDER 模板渲染，
   导致排名/下浮率/折扣/偏差/价格分/基准价/评分规则/产品清单全部丢失。

   本模块是纯函数、无 Vue 依赖，可用真实样本在 Node 里直接回归。
   —— 金额单位一律「元」，不做万/元换算。
   ================================================================ */

const num = (v) => {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
const str = (v, d = '') => (v === null || v === undefined ? d : String(v))
const arr = (v) => (Array.isArray(v) ? v : [])

export function fmtMoney(v, digits = 2) {
  const n = num(v)
  if (n === null) return '—'
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
export function fmtNum(v, digits = 4) {
  const n = num(v)
  return n === null ? '—' : n.toFixed(digits)
}
/** 百分数值（17.76 -> "17.76"），表头自带 % 单位 */
export function fmtPct(v, digits = 2) {
  const n = num(v)
  return n === null ? '—' : n.toFixed(digits)
}
/** 带符号偏差（0.9848 -> "+0.98"；-4.2216 -> "-4.22"） */
export function fmtSigned(v, digits = 2) {
  const n = num(v)
  if (n === null) return '—'
  return (n > 0 ? '+' : '') + n.toFixed(digits)
}
export function fmtScore(v, digits = 2) {
  const n = num(v)
  return n === null ? '—' : n.toFixed(digits)
}

function safeParse(s) {
  try { return JSON.parse(s) } catch (e) { return null }
}

/**
 * 把 summary 解析成模式 D 视图模型。
 * 缺字段一律降级为空值，绝不让模板因 undefined 崩掉。
 */
export function parseDiscount(summary) {
  const empty = {
    ok: false, rule: {}, bidders: [], products: [], totals: {},
    warnings: [], notes: [], files: [], tokens: {},
  }
  if (!summary) return empty
  let raw = summary.raw_result
  if (typeof raw === 'string') raw = safeParse(raw)
  if (!raw || typeof raw !== 'object') return empty
  const d = raw.discount
  if (!d || typeof d !== 'object') return empty

  const bidders = arr(d.bidders).map((b, i) => ({
    key: str(b.name) + '#' + i,
    rank: num(b.rank) ?? (i + 1),
    name: str(b.name, '—'),
    rate: num(b.rate),                 // 下浮率 %，如 17.76
    discount: num(b.discount),         // 折扣小数，如 0.8224
    reviewPrice: num(b.review_price),  // 评审价 = 折扣
    per100: num(b.per_100),
    deviation: num(b.deviation),       // 偏差 %
    priceScore: num(b.price_score),
    estTotal: num(b.est_total),        // 预估结算价（元）
    unitSum: num(b.unit_sum),
    flags: arr(b.flags).map(str),
    valid: b.valid !== false,
    overCap: !!b.over_cap,
    inBenchmark: b.in_benchmark !== false,
    publicityRank: num(b.publicity_rank),
    publicityRate: num(b.publicity_rate),
  })).sort((a, b) => a.rank - b.rank)

  const products = arr(d.products).map((p, i) => {
    const qty = num(p.qty)
    const cap = num(p.cap)
    return {
      key: str(p.seq) + '#' + i,
      seq: str(p.seq, String(i + 1)),
      name: str(p.name, '—'),
      spec: str(p.spec),
      unit: str(p.unit),
      qty,
      cap,
      amount: (qty !== null && cap !== null) ? qty * cap : null,
      sourcePage: str(p.source_page),
    }
  })

  const capSum = products.reduce((s, p) => s + (p.amount || 0), 0)
  const notes = []
  for (const b of bidders) {
    for (const f of b.flags) notes.push({ name: b.name, text: f })
    // flags 里通常已带「剔除」说明，避免同一投标人重复提示两次
    const hasTrim = b.flags.some(f => /剔除|去高|去低|最高\/最低/.test(f))
    if (!b.inBenchmark && !hasTrim) {
      notes.push({ name: b.name, text: '基准价计算时作为最高/最低报价剔除（仍参与价格评分）' })
    }
    if (b.overCap && !b.flags.some(f => /限价|无效/.test(f))) {
      notes.push({ name: b.name, text: '报价折扣超过最高投标折扣限价' })
    }
  }

  const warnings = [...arr(raw.warnings), ...arr(d.warnings)].map(str).filter(Boolean)

  return {
    ok: bidders.length > 0,
    rule: {
      rateKind: str(d.rate_kind, 'rebate'),
      rateLabel: d.rate_kind === 'discount_rate' ? '折扣率' : '下浮率',
      discountDef: str(d.discount_def),
      reviewPriceDef: str(d.review_price_def),
      unitPriceDef: str(d.unit_price_def),
      settlementDef: str(d.settlement_def),
      benchmark: num(d.benchmark),
      benchmarkN: num(d.benchmark_n),
      benchmarkRule: str(d.benchmark_rule),
      trimHigh: num(d.trim_high),
      trimLow: num(d.trim_low),
      trimGuard: num(d.trim_guard),
      fullScore: num(d.full_score),
      baseScore: num(d.base_score),
      aboveDeduct: num(d.above_deduct),
      belowDeduct: num(d.below_deduct),
      scoreKind: str(d.score_kind),
      step: num(d.step),
      formula: str(d.formula),
      basis: str(d.basis),
      confirmed: !!d.confirmed,
      ruleText: str(d.rule_text),
      ruleDetail: arr(d.rule_detail),
      ruleSourceFile: str(d.rule_source_file),
      ruleSourcePage: str(d.rule_source_page),
      llmPages: arr(d.llm_pages).map(str),
      capTotal: num(d.cap_total),
      capSum,
      statedCap: num(d.stated_cap),
      capLimitPct: num(d.cap_limit_pct),
      lowLimit: num(d.low_limit),
      openingFile: str(d.opening_file),
      productFile: str(d.product_file),
      publicityN: num(d.publicity_n),
      publicityFile: str(d.publicity_file),
    },
    bidders,
    products,
    totals: {
      bidderCount: bidders.length,
      productCount: products.length,
      capSum,
      capTotal: num(d.cap_total),
      benchmarkCount: num(d.benchmark_n),
      trimmedCount: bidders.filter(b => !b.inBenchmark).length,
      tokenIn: num(raw.tokens?.in),
      tokenOut: num(raw.tokens?.out),
      publicityN: num(d.publicity_n),
    },
    warnings,
    notes,
    files: arr(raw.files).map(f => ({
      name: str(f.name || f.rel), kind: str(f.kind),
      pages: num(f.page_count), status: str(f.status),
    })),
    tokens: { in: num(raw.tokens?.in), out: num(raw.tokens?.out) },
  }
}

/** 导出 Excel 用的行数据（多 Sheet，对齐独立 CLI 的汇总表完整度） */
export function discountSheets(vm) {
  if (!vm || !vm.ok) return []
  const r = vm.rule
  const meta = [
    ['开标汇总 · 折扣/下浮率报价'],
    ['报价口径', r.discountDef],
    ['评审价定义', r.reviewPriceDef],
    ['中标单价定义', r.unitPriceDef],
    ['评标基准价', r.benchmark === null ? '' : r.benchmark],
    ['参与基准价计算家数', r.benchmarkN ?? ''],
    ['去高去低', r.trimGuard === null ? '' : `有效报价 > ${r.trimGuard} 家，去 ${r.trimHigh ?? 0} 高 ${r.trimLow ?? 0} 低`],
    ['价格分满分', r.fullScore ?? ''],
    ['高于基准价扣分', r.aboveDeduct ?? ''],
    ['低于基准价扣分', r.belowDeduct ?? ''],
    ['评分公式', r.formula],
    ['规则校验', r.basis],
    ['规则来源', `${r.ruleSourceFile} P${r.ruleSourcePage}`.trim()],
    ['产品项数', vm.totals.productCount],
    ['Σ(限价×预估量)', vm.totals.capTotal ?? vm.totals.capSum],
    ['规则定位页', (r.llmPages || []).join(', ')],
  ]
  const bidHead = ['排名', '投标单位', `${r.rateLabel}(%)`, '折扣', '每百报价', '偏差(%)',
    '价格分', '预估结算价(元)', '参与基准价', '有效性', '标记']
  const bidRows = vm.bidders.map(b => [
    b.rank, b.name, b.rate, b.discount, b.per100, b.deviation,
    b.priceScore, b.estTotal, b.inBenchmark ? '是' : '否（去高去低剔除）',
    b.valid ? '有效' : '无效', b.flags.join('；'),
  ])
  const prodHead = ['序号', '品名', '规格', '单位', '预估量', '单价限价(元)', '限价小计(元)', '来源页']
  const prodRows = vm.products.map(p => [p.seq, p.name, p.spec, p.unit, p.qty, p.cap, p.amount, p.sourcePage])
  const ruleRows = [['招标文件评分规则原文'], [r.ruleText], ['评分公式'], [r.formula], ['基准价规则'], [r.benchmarkRule]]
  return [
    { name: '评审明细', data: [...meta, [], bidHead, ...bidRows] },
    { name: '产品清单', data: [prodHead, ...prodRows] },
    { name: '评分规则', data: ruleRows },
  ]
}
