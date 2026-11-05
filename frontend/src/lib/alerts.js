/* ================================================================
   需要处理事项：统一计算（纯函数）
   看板「需要处理」面板 与 「需要处理」详情页 共用，避免两处口径漂移。
   权限不在这里判断，由调用方按字段四档传入 bid / bond / perf 三个开关。
   ================================================================ */
import { dd, num } from './format'
import { isNoBid, bondState, bondKind, shortType, bidStatusOf } from './fields'

/* 事项类型：lv = 排序等级（1 提醒 / 2 紧急 / 3 关注），sev = 视觉与统计等级（缺省同 lv）。
   标书未准备 lv 保持 1（列表中紧跟「即将开标」，不改变现有顺序），但 sev=2 → 红色、计入「紧急」。 */
export const ALERT_TYPES = [
  {k:'soon',   l:'待开标',     tag:'即将开标',   lv:1, hint:'7 天内开标，标书仍需递交'},
  {k:'prep',   l:'标书未准备', tag:'标书未准备', lv:1, sev:2, hint:'今天及以后开标，标书仍未开始编写（紧急）'},
  {k:'pay',    l:'待缴费',     tag:'待缴费',     lv:2, hint:'投标 / 履约保证金尚未缴纳'},
  {k:'refund', l:'待退费',     tag:'待退费',     lv:2, hint:'投标 / 履约保证金已缴但尚未退回'},
  /* 关注级：明细台账质量问题，仅详情页展示（看板面板的「关注」计数即这批） */
  {k:'info',   l:'信息缺失',   tag:'信息缺失',   lv:3, minor:true, hint:'缺负责人 / 招标编号 / 开标时间'},
  {k:'amount', l:'待补金额',   tag:'待补金额',   lv:3, minor:true, hint:'已中标但未填中标金额'},
  {k:'guar',   l:'保函到期',   tag:'保函到期',   lv:3, minor:true, hint:'保函 30 天内到期或已过期，需延期 / 解保'}
];
export const ALERT_TYPE_MAP = {};
ALERT_TYPES.forEach(t => { ALERT_TYPE_MAP[t.k] = t; });

function money(v){
  const n = num(v);
  return n > 0 ? n.toLocaleString('zh-CN', {maximumFractionDigits:2}) + ' 万' : '';
}
function leadOf(r){ return r.lead || '未指定负责人'; }
function openTxt(d){ return d === 0 ? '今天开标' : d + ' 天后开标'; }
function gapTxt(d){
  if(d === null) return '';
  return d >= 0 ? (d === 0 ? '今天开标' : '距开标 ' + d + ' 天') : '已开标 ' + (-d) + ' 天';
}

/**
 * 生成事项列表
 * @param rows 台账行
 * @param opt  { bid, bond, perf, minor }  bid/bond/perf = 对应字段是否可参与统计（canAgg）；
 *             minor = 是否附带「关注」级小事项（信息缺失 / 待补金额 / 保函到期）
 */
export function buildAlerts(rows, opt){
  opt = opt || {};
  const bid   = opt.bid   !== false;
  const bond  = opt.bond  !== false;
  const perf  = opt.perf  !== false;
  const minor = !!opt.minor;
  const out = [];

  (rows || []).forEach(r => {
    if(!r || isNoBid(r)) return;
    const d = dd(r.openTime);
    const pending = r.result === '待开标';
    const future = d !== null && d >= 0;
    const dateTxt = (r.openTime || '').slice(5);

    /* 1. 待开标（即将开标）：7 天内开标 */
    if(pending && future && d <= 7){
      out.push({ type:'soon', lv:1, tag:'即将开标', r, d, daysPast:0,
        reason:'距开标 ' + d + ' 天',
        sub:dateTxt + ' 开标（' + openTxt(d) + '） · ' + leadOf(r) });
    }

    /* 2. 标书未准备：今天及以后开标、结果仍待开标，但标书状态还是「未开始」
       视觉上按紧急（红色）提示，排序仍与「即将开标」同级，保持既有展示顺序 */
    if(bid && pending && future && bidStatusOf(r) === '未开始'){
      out.push({ type:'prep', lv:1, sev:2, tag:'标书未准备', r, d, daysPast:0,
        reason:'标书仍未开始编写',
        sub:dateTxt + ' 开标（' + openTxt(d) + '） · 标书仍未开始编写 · ' + leadOf(r) });
    }

    /* 3. 待缴费：待开标且投标 / 履约保证金尚未缴纳 */
    const payBid  = bond && r.bondStatus === '未缴';
    const payPerf = perf && r.perfStatus === '未缴';
    if(pending && (payBid || payPerf)){
      const parts = [];
      if(payBid)  parts.push('投标保证金' + (money(r.bondAmount) ? ' ' + money(r.bondAmount) : '') + '未缴');
      if(payPerf) parts.push('履约保证金' + (money(r.perfAmount) ? ' ' + money(r.perfAmount) : '') + '未缴');
      out.push({ type:'pay', lv:2, tag:'待缴费', r, d, daysPast:0,
        reason:parts.join(' / '),
        sub:parts.join(' · ') + (d !== null ? ' · ' + gapTxt(d) : '') + ' · ' + leadOf(r) });
    }

    /* 4. 待退费：开标结果已定（或已过开标）仍是「已缴」——投标 / 履约保证金未退回 */
    const backBid  = bond && bondState(r) === '待退';
    const backPerf = perf && r.perfStatus === '已缴' && !pending;
    if(backBid || backPerf){
      const past = (d !== null && d < 0) ? -d : 0;
      const names = [], amts = [];
      if(backBid){ names.push('投标保证金'); if(money(r.bondAmount)) amts.push(money(r.bondAmount)); }
      if(backPerf){ names.push('履约保证金'); if(money(r.perfAmount)) amts.push(money(r.perfAmount)); }
      out.push({ type:'refund', lv: past > 30 ? 2 : 3, tag:'待退费', r, d, daysPast:past,
        reason:(past ? '已开标 ' + past + ' 天仍未退回' : '开标结果已定，仍待退回') + ' · ' + names.join(' / '),
        sub:(past ? '已开标 ' + past + ' 天仍未退回' : '开标结果已定，仍待退回') + ' · ' +
            names.join(' / ') + (amts.length ? ' ' + amts.join(' / ') : '') + ' · ' + leadOf(r) });
    }

    /* 以下为「关注」级事项（信息缺失 / 待补金额 / 保函到期） */
    if(!minor) return;
    if(!r.lead || !r.bidNo || !r.openTime){
      const miss = [!r.lead && '负责人', !r.bidNo && '招标编号', !r.openTime && '开标时间'].filter(Boolean).join('、');
      out.push({ type:'info', lv:3, tag:'信息缺失', r, d, daysPast:0, reason:'缺少：' + miss, sub:'缺少：' + miss });
    }
    if(r.result === '中标' && !num(r.winAmount)){
      out.push({ type:'amount', lv:3, tag:'待补金额', r, d, daysPast:0, reason:'已中标但未填中标金额', sub:'已中标但未填中标金额' });
    }
    if(bondKind(r) === 'guar' && r.bondStatus === '已缴' && bondState(r) !== '待退'){
      const ddue = dd(r.bondDue);
      if(ddue !== null && ddue <= 30){
        out.push({ type:'guar', lv: ddue < 0 ? 2 : 3, tag: ddue < 0 ? '保函已过期' : '保函将到期', r, d, daysPast:0,
          reason: shortType(r.bondType) + '有效期至 ' + (r.bondDue || '').slice(5) + (ddue < 0 ? '（已过期 ' + (-ddue) + ' 天）' : '（' + ddue + ' 天后）'),
          sub: shortType(r.bondType) + '有效期至 ' + (r.bondDue || '').slice(5) + (ddue < 0 ? '（已过期 ' + (-ddue) + ' 天）' : '（' + ddue + ' 天后）') + ' · 需延期或解保' });
      }
    }
  });

  /* 排序：先按等级（紧急→提醒→关注），再按开标时间由近到远，待退费按逾期天数 */
  out.sort((a, b) => {
    if(a.lv !== b.lv) return a.lv - b.lv;
    if(a.type === 'refund' && b.type === 'refund') return b.daysPast - a.daysPast;
    if(a.d === null && b.d === null) return 0;
    if(a.d === null) return 1;
    if(b.d === null) return -1;
    return a.d - b.d;
  });
  return out;
}

/** 视觉 / 统计等级：优先 sev（如「标书未准备」排序算提醒、显示算紧急），缺省 lv */
export function sevOf(o){ return o.sev || o.lv; }

/** 各类型计数（用于筛选项上的数字） */
export function countByType(list){
  const m = { all: 0 };
  ALERT_TYPES.forEach(t => { m[t.k] = 0; });
  (list || []).forEach(o => {
    m.all++;
    if(m[o.type] === undefined) m[o.type] = 0;
    m[o.type]++;
  });
  return m;
}
