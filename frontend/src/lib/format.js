/* ==================== 日期与数字工具 ==================== */
export const TODAY = new Date(); TODAY.setHours(0,0,0,0);

export function pad(n){ return String(n).padStart(2,'0'); }
export function fmtYM(d){ return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
export function parseD(s){
  if(!s) return null;
  if(s instanceof Date) return isNaN(s) ? null : s;
  const m = String(s).match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})/);
  return m ? new Date(+m[1], +m[2]-1, +m[3]) : null;
}
export function ymOf(s){ const m = String(s||'').match(/(\d{4})\D+(\d{1,2})/); return m ? m[1] + '-' + pad(m[2]) : ''; }
export function yOf(s){ const m = String(s||'').match(/(\d{4})/); return m ? m[1] : ''; }
/* 月份串 YYYY-MM 偏移 n 个月 */
export function ymShift(ym, n){
  const d = new Date(+ym.slice(0,4), +ym.slice(5,7) - 1 + n, 1);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1);
}
/* 起止月份（含两端）之间的月份串列表 */
export function ymMonths(from, to){
  const out = [];
  if(!from || !to || from > to) return out;
  let y = +from.slice(0,4), m = +from.slice(5,7);
  const ty = +to.slice(0,4), tm = +to.slice(5,7);
  while(y < ty || (y === ty && m <= tm)){
    out.push(y + '-' + pad(m));
    if(++m > 12){ m = 1; y++; }
  }
  return out;
}
/* 区间文字：2026年10月 / 2026年1–3月 / 2026年全年 / 2025年12月 – 2026年3月 */
export function ymLabel(from, to){
  if(!from || !to) return '';
  if(from === to) return +from.slice(0,4) + '年' + +from.slice(5,7) + '月';
  const fy = +from.slice(0,4), fm = +from.slice(5,7), ty = +to.slice(0,4), tm = +to.slice(5,7);
  if(fy === ty && fm === 1 && tm === 12) return fy + '年全年';
  if(fy === ty) return fy + '年' + fm + '–' + tm + '月';
  return fy + '年' + fm + '月 – ' + ty + '年' + tm + '月';
}
export function dd(s){ const d = parseD(s); return d ? Math.round((d - TODAY) / 86400000) : null; }
export function num(v){ const n = parseFloat(v); return isNaN(n) ? 0 : n; }
export function fmtMoney(v){ const n = num(v); return n >= 10000 ? (n/10000).toFixed(2) + '亿' : n.toFixed(1) + '万'; }
export function fmtN(v){ const n = num(v); return n ? n.toLocaleString('zh-CN', {maximumFractionDigits:1}) : '—'; }
export function uid(){ return 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2,6); }
