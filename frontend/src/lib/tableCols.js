/* ================================================================
   台账表格列目录 + 列配置默认值与归一化
   列 key 优先复用字段 key；project / time / amount 为组合列。
   ================================================================ */
import { F } from './fields'

/* 默认展示的 8 列（与原有表格一致） */
const BASE = [
  {k:'project',    l:'项目 / 招标编号', t:'project', sk:'projectName', w:300, g:'常用列'},
  {k:'time',       l:'时间',           t:'time',                        w:104, g:'常用列'},
  {k:'method',     l:'招标方式',       t:'text',                        w:100, g:'常用列'},
  {k:'lead',       l:'负责人',         t:'lead',                        w:110, g:'常用列'},
  {k:'result',     l:'开标结果',       t:'result',                      w:92,  g:'常用列'},
  {k:'winner',     l:'中标单位',       t:'winner',                      w:180, g:'常用列'},
  {k:'amount',     l:'金额（万元）',   t:'amount', sortable:false,      w:110, g:'常用列'},
  {k:'bondStatus', l:'保证金',         t:'bond',                        w:100, g:'常用列'}
];

/* 已被组合列/默认列覆盖的字段不再单独列出，避免重复勾选 */
const IN_BASE = ['projectName','ourPrice','winAmount','method','lead','result','winner','bondStatus'];

const WIDE = ['agency','owner','openAddr','bondIssuer','perfIssuer','noticeUrl'];

/* 表头空间有限：字段可用 cl 指定较短的列名（表单/详情/权限仍用完整 l） */
const EXTRA = F.filter(f => IN_BASE.indexOf(f.k) < 0).map(f => ({
  k: f.k,
  l: (f.cl || f.l).replace('（万元）', ''),
  t: f.t,
  g: f.g,
  w: f.k === 'remark' ? 240 : (WIDE.indexOf(f.k) >= 0 ? 160 : (f.t === 'num' || f.t === 'date' ? 110 : 120)),
  der: f.der                       // 派生列（不落库、不可排序，如「标书投入时间」）
}));

export const TABLE_COLS = BASE.concat(EXTRA);
export const TABLE_MAP = {};
TABLE_COLS.forEach(c => { TABLE_MAP[c.k] = c; });

export const DEFAULT_COLS = BASE.map(c => c.k);
export const DEFAULT_WIDTHS = {};
TABLE_COLS.forEach(c => { DEFAULT_WIDTHS[c.k] = c.w; });

export const MIN_W = 64;
export const MAX_W = 900;
export const MIN_ROW_H = 26;
export const MAX_ROW_H = 120;

export function clampWidth(w){ return Math.max(MIN_W, Math.min(MAX_W, Math.round(Number(w) || 0))); }
export function clampRowH(h){ return Math.max(MIN_ROW_H, Math.min(MAX_ROW_H, Math.round(Number(h) || 0) || 40)); }

export function defaultTableCfg(){
  return {
    activeId: '',
    cols: DEFAULT_COLS.slice(),
    widths: Object.assign({}, DEFAULT_WIDTHS),
    rowH: 40,
    list: []
  };
}

function normCols(arr){
  const out = [];
  (Array.isArray(arr) ? arr : []).forEach(k => {
    if(TABLE_MAP[k] && out.indexOf(k) < 0) out.push(k);
  });
  return out.length ? out : DEFAULT_COLS.slice();
}
function normWidths(w){
  const out = Object.assign({}, DEFAULT_WIDTHS);
  if(w && typeof w === 'object'){
    TABLE_COLS.forEach(c => {
      if(w[c.k] !== undefined) out[c.k] = clampWidth(w[c.k]);
    });
  }
  return out;
}
function normOne(c){
  return {
    id: String(c.id || ''),
    name: String(c.name || '').trim().slice(0, 30),
    cols: normCols(c.cols),
    widths: normWidths(c.widths),
    rowH: clampRowH(c.rowH)
  };
}

/* 兼容旧缓存 / 字段增删：未知列丢弃，缺失宽度补默认值 */
export function normalizeTableCfg(raw){
  const d = defaultTableCfg();
  if(!raw || typeof raw !== 'object') return d;
  const list = (Array.isArray(raw.list) ? raw.list : [])
    .filter(x => x && String(x.name || '').trim())
    .map(normOne);
  let activeId = String(raw.activeId || '');
  if(activeId && !list.some(x => x.id === activeId)) activeId = '';
  return {
    activeId,
    cols: normCols(raw.cols),
    widths: normWidths(raw.widths),
    rowH: clampRowH(raw.rowH),
    list
  };
}
