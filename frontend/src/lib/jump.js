/* ================================================================
   看板 → 标源总览 的跳转参数
   单月：y + m（到月并展开该月）；整年：仅 y；其他区间：取结束月所属年
   ================================================================ */
import { ST } from '../stores/bid'

export function timelineQuery(extra){
  const a = String(ST.ymFrom || '');
  const b = String(ST.ymTo || a);
  const q = {};
  if(/^\d{4}-\d{2}$/.test(a) && a === b){
    q.y = a.slice(0, 4);
    q.m = a.slice(5, 7);
  }else if(/^\d{4}-\d{2}$/.test(a) && a.slice(0, 4) === b.slice(0, 4)){
    q.y = a.slice(0, 4);
  }else if(/^\d{4}-\d{2}$/.test(b)){
    q.y = b.slice(0, 4);
  }
  return Object.assign(q, extra || {});
}
