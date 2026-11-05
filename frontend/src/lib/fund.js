/* ================================================================
   资金占用口径（看板「项目开销占用」KPI 与「资金状态」饼图共用）
   ---------------------------------------------------------------
   四类资金：
     已缴纳 paid —— 保证金状态「已缴」，且退回时间未到（钱压在业主处，还没到该退的时候）
     待缴纳 due  —— 保证金状态「未缴」（还没付出去，属于即将发生的支出）
     待退回 back —— 保证金状态不为「已退」，且退回时间已到 / 已过（逾期未退，资金仍被占用）
     项目开销 cost —— 购买标书费 + 差旅费 + 样品费（已花掉的直接成本）
   投标保证金与履约保证金各按自己的状态 + 退回时间字段分别归桶，互不混淆。
   「已退 / 无需缴纳 / 不予退还」三种状态不占资金，不计入任何一桶。
   ================================================================ */
import { num } from './format'

/* 当天日期（YYYY-MM-DD，用于与退回时间比较，ISO 字符串可直接按字典序比较） */
export function isoToday(){
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

/* 单项保证金归入哪一桶；返回 null 表示不计入。
   待退回口径：状态不是「已退」（钱确实还压着），且退回时间已到 / 已过（逾期）。 */
export function fundBucket(status, backDate, today){
  if(status === '未缴') return 'due';
  if(!status || status === '已退' || status === '无需缴纳' || status === '不予退还') return null;
  /* 走到这里 = 状态为「已缴」等仍在占用资金的状态：
     退回时间已到且仍未退 → 待退回；退回时间未到或未填 → 已缴纳 */
  if(backDate && today && backDate <= today) return 'back';
  return 'paid';
}

export const FUND_KEYS = ['paid', 'due', 'back', 'cost'];
export const FUND_META = {
  paid: {l:'已缴纳', c:'#2B5078', parts:[
    {k:'bond', l:'投标保证金', c:'#2B5078'},
    {k:'perf', l:'履约保证金', c:'#6E96BC'}
  ]},
  due: {l:'待缴纳', c:'#A96F14', parts:[
    {k:'bond', l:'投标保证金', c:'#A96F14'},
    {k:'perf', l:'履约保证金', c:'#D3A455'}
  ]},
  back: {l:'待退回', c:'#B08A44', parts:[
    {k:'bond', l:'投标保证金', c:'#B08A44'},
    {k:'perf', l:'履约保证金', c:'#DCC08A'}
  ]},
  cost: {l:'项目开销', c:'#3C7D63', parts:[
    {k:'doc',    l:'购买标书费', c:'#2F6B56'},
    {k:'travel', l:'差旅费',     c:'#5A9B80'},
    {k:'sample', l:'样品费',     c:'#9CC6B2'}
  ]}
};
/* 各明细项对应的台账字段（用于导出/校验时回溯，非金额字段为 null） */
export const FUND_FIELD = {
  bond:'bondAmount', perf:'perfAmount', doc:'docFee', travel:'travelFee', sample:'sampleFee'
};

/** 当前账号可参与汇总的明细项（隐藏 / 脱敏的字段不能进汇总，否则数字失真；
    待退回判定依赖退回时间字段，该字段不可汇总时对应保证金整体不进统计） */
export function fundAllow(canAgg){
  const a = [];
  if(canAgg('bondAmount') && canAgg('bondStatus') && canAgg('bondBackDate')) a.push('bond');
  if(canAgg('perfAmount') && canAgg('perfStatus') && canAgg('perfBackDate')) a.push('perf');
  if(canAgg('docFee'))    a.push('doc');
  if(canAgg('travelFee')) a.push('travel');
  if(canAgg('sampleFee')) a.push('sample');
  return a;
}

/**
 * 汇总一批台账行的资金占用。
 * @param rows  台账行（调用方决定范围：本期 / 全部）
 * @param allow 可汇总的明细项 key 列表（来自 fundAllow）
 * @param today 当天日期（YYYY-MM-DD，缺省取系统当天；用于待退回的逾期判断）
 * @returns {paid:{bond,perf,sum}, due:{...}, back:{...}, cost:{doc,travel,sample,sum}, total}
 */
export function fundStat(rows, allow, today){
  const t0 = today || isoToday();
  const ok = k => allow.indexOf(k) >= 0;
  const o = {};
  FUND_KEYS.forEach(c => {
    o[c] = {};
    FUND_META[c].parts.forEach(p => { o[c][p.k] = 0; });
  });
  (rows || []).forEach(r => {
    if(ok('bond')){ const b = fundBucket(r.bondStatus, r.bondBackDate, t0); if(b) o[b].bond += num(r.bondAmount); }
    if(ok('perf')){ const b = fundBucket(r.perfStatus, r.perfBackDate, t0); if(b) o[b].perf += num(r.perfAmount); }
    if(ok('doc'))    o.cost.doc    += num(r.docFee);
    if(ok('travel')) o.cost.travel += num(r.travelFee);
    if(ok('sample')) o.cost.sample += num(r.sampleFee);
  });
  FUND_KEYS.forEach(c => { o[c].sum = FUND_META[c].parts.reduce((s, p) => s + o[c][p.k], 0); });
  o.total = FUND_KEYS.reduce((s, c) => s + o[c].sum, 0);
  return o;
}

/** 顶层四类（按 allow 过滤明细项、去掉 0 值）→ 饼图与列表的数据源 */
export function fundCats(stat, allow){
  return FUND_KEYS.map(k => ({
    key:k, l:FUND_META[k].l, c:FUND_META[k].c, sum:stat[k].sum,
    parts:FUND_META[k].parts.filter(p => allow.indexOf(p.k) >= 0)
  })).filter(x => x.sum > 0);
}

/* ---------- 资金口径 → 项目列表（投标管理跳转） ----------
   口径编码：'paid.bond' / 'due.perf' / 'back.bond' / 'cost.doc' / 'cost.travel' / 'cost.sample' …
   前段 = 四类之一（cost = 项目开销），后段 = 明细项 key（投标/履约保证金或三项开销）。 */

/** 一条台账行是否属于该口径（与 fundStat 的归桶完全同源，保证列表金额之和 = 饼图扇块） */
export function rowInFund(r, code, today){
  if(!r || !code) return false;
  const dot = code.indexOf('.');
  if(dot < 0) return false;
  const cat = code.slice(0, dot), item = code.slice(dot + 1);
  if(cat === 'cost'){                      /* 项目开销：该费用字段有值即算 */
    const f = FUND_FIELD[item];
    return !!f && num(r[f]) > 0;
  }
  if(item === 'bond') return fundBucket(r.bondStatus, r.bondBackDate, today) === cat;
  if(item === 'perf') return fundBucket(r.perfStatus, r.perfBackDate, today) === cat;
  return false;
}

/** 口径编码 → 中文标签（如 'back.bond' → 「待退回 · 投标保证金」） */
export function fundLabel(code){
  const dot = String(code || '').indexOf('.');
  if(dot < 0) return '';
  const cat = code.slice(0, dot), item = code.slice(dot + 1);
  const m = FUND_META[cat];
  if(!m) return '';
  const p = m.parts.find(x => x.k === item);
  return p ? m.l + ' · ' + p.l : '';
}
