/* ================================================================
   字段配置（48 字段）、示例数据、导入解析归一化、行级纯函数
   ================================================================ */
import { pad, parseD } from './format'

export const F = [
  {k:'projectName', l:'项目名称',        t:'text',   g:'项目基础', req:1, span:2},
  {k:'bidNo',       l:'招标编号',        t:'text',   g:'项目基础'},
  {k:'pkg',         l:'标段/包号',       t:'text',   g:'项目基础'},
  {k:'agency',      l:'代理机构',        t:'text',   g:'项目基础', span:2},
  {k:'owner',       l:'采购单位（业主）', t:'text',   g:'项目基础', span:2},
  {k:'region',      l:'项目所在地',      t:'text',   g:'项目基础'},
  {k:'method',      l:'招标方式',        t:'select', g:'项目基础', opt:['公开招标','邀请招标','竞争性谈判','竞争性磋商','单一来源','框架入围','询价']},
  {k:'bizType',     l:'业务类型',        t:'text',   g:'项目基础'},
  {k:'projType',    l:'类型',            t:'select', g:'项目基础', opt:['工程','货物','服务']},
  {k:'industry',    l:'行业',            t:'select', g:'项目基础', opt:['政府机关','医疗健康','教育院校','交通运输','金融银行','能源电力','商业/地产','文旅酒店','工业企业','其他']},
  {k:'province',    l:'省份',            t:'select', g:'项目基础', span:2, opt:['北京市','天津市','河北省','山西省','内蒙古自治区','辽宁省','吉林省','黑龙江省','上海市','江苏省','浙江省','安徽省','福建省','江西省','山东省','河南省','湖北省','湖南省','广东省','广西壮族自治区','海南省','重庆市','四川省','贵州省','云南省','西藏自治区','陕西省','甘肃省','青海省','宁夏回族自治区','新疆维吾尔自治区','香港特别行政区','澳门特别行政区','台湾省']},
  {k:'deadline',    l:'投标截止时间',    t:'date',   g:'时间与地点'},
  {k:'openTime',    l:'开标时间',        t:'date',   g:'时间与地点', req:1},
  {k:'openAddr',    l:'开标地点',        t:'text',   g:'时间与地点', span:2},
  {k:'budget',      l:'预算/最高限价（万元）', t:'num', g:'金额与保证金'},
  {k:'ourPrice',    l:'我方报价（万元）', t:'num',   g:'金额与保证金'},
  {k:'bondType',    l:'保证金形式',      t:'select', g:'金额与保证金', opt:['银行转账','银行保函','保险保单','担保公司保函','现金','免缴']},
  /* 投标保证金：金额 / 状态 / 退回时间 同一行，下面一行是投标保函四项 */
  {k:'bondAmount',  l:'投标保证金金额（万元）', t:'num', g:'金额与保证金'},
  {k:'bondStatus',  l:'投标保证金状态',  t:'select', g:'金额与保证金', opt:['无需缴纳','未缴','已缴','已退','不予退还']},
  {k:'bondBackDate',l:'投标保证金退回时间', t:'date', g:'金额与保证金', cl:'投标退回时间'},
  {k:'bondIssuer',  l:'投标保函开具行 / 收款单位', t:'text', g:'金额与保证金', span:3, cl:'投标保函开具行'},
  {k:'bondNo',      l:'投标保函 / 凭证编号', t:'text', g:'金额与保证金', cl:'投标保函编号'},
  {k:'bondPayDate', l:'投标缴纳 / 开具日期', t:'date', g:'金额与保证金', cl:'投标缴纳日期'},
  {k:'bondDue',     l:'投标保函有效期',  t:'date',   g:'金额与保证金', cl:'投标保函有效期'},
  /* 履约保证金：与投标保证金同结构，退回时间与保函四项各自独立 */
  {k:'perfAmount',  l:'履约保证金金额（万元）', t:'num', g:'金额与保证金'},
  {k:'perfStatus',  l:'履约保证金状态',  t:'select', g:'金额与保证金', opt:['无需缴纳','未缴','已缴','已退','不予退还']},
  {k:'perfBackDate',l:'履约保证金退回时间', t:'date', g:'金额与保证金', cl:'履约退回时间'},
  {k:'perfIssuer',  l:'履约保函开具行 / 收款单位', t:'text', g:'金额与保证金', span:3, cl:'履约保函开具行'},
  {k:'perfNo',      l:'履约保函 / 凭证编号', t:'text', g:'金额与保证金', cl:'履约保函编号'},
  {k:'perfPayDate', l:'履约缴纳 / 开具日期', t:'date', g:'金额与保证金', cl:'履约缴纳日期'},
  {k:'perfDue',     l:'履约保函有效期',  t:'date',   g:'金额与保证金', cl:'履约保函有效期'},
  {k:'lead',        l:'标书负责人',      t:'text',   g:'人员分工', req:1},
  {k:'copilot',     l:'协作人',          t:'text',   g:'人员分工'},
  {k:'reviewer',    l:'审核人',          t:'text',   g:'人员分工'},
  /* 标书进度：状态由员工单向推进（未开始→进行中→已结束），回退仅管理员；
     三个时间点为状态切换时自动记录，ro=1 表示表单里默认只读（管理员可修正）；
     bidEffort 为派生字段（不落库），由 进行中时间 → 已结束时间/今天 计算天数 */
  {k:'bidStatus',   l:'标书状态',        t:'select', g:'标书进度', opt:['未开始','进行中','已结束']},
  {k:'bidWaitAt',   l:'未开始时间',      t:'date',   g:'标书进度', ro:1},
  {k:'bidStartAt',  l:'进行中时间',      t:'date',   g:'标书进度', ro:1},
  {k:'bidEndAt',    l:'已结束时间',      t:'date',   g:'标书进度', ro:1},
  {k:'bidEffort',   l:'标书投入时间',     t:'num', g:'标书进度', der:1},
  /* 项目开销：投标过程中的三项直接成本（万元），用于单项目 / 阶段成本核算 */
  {k:'docFee',      l:'购买标书费（万元）', t:'num', g:'项目开销'},
  {k:'travelFee',   l:'差旅费（万元）',   t:'num',   g:'项目开销'},
  {k:'sampleFee',   l:'样品费（万元）',   t:'num',   g:'项目开销'},
  {k:'result',      l:'开标结果',        t:'select', g:'开标结果', req:1, opt:['待开标','中标','未中标','废标','流标','不投','放弃']},
  {k:'winner',      l:'中标单位',        t:'text',   g:'开标结果', span:2},
  {k:'winAmount',   l:'中标金额（万元）', t:'num',   g:'开标结果'},
  {k:'rank',        l:'我方排名',        t:'text',   g:'开标结果', span:2},
  {k:'noticeUrl',   l:'中标公告链接',    t:'text',   g:'开标结果', span:3},
  {k:'remark',      l:'备注',            t:'text',   g:'开标结果', t2:'area', span:3}
];
export const FMAP = {}; F.forEach(f => FMAP[f.k] = f);
export const RESULT_OPTS = ['待开标','中标','未中标','废标','流标','不投','放弃'];
export const BID_STATUS_OPTS = ['未开始','进行中','已结束'];
export const BID_RANK = {'未开始':0, '进行中':1, '已结束':2};
export const METHOD_OPTS = FMAP.method.opt;
/* 编辑表单渲染项：分组标题 + 字段（派生字段不进表单，如「标书投入时间」） */
export const FORM_ITEMS = [];
{ let lastG = '';
  F.forEach(f => {
    if(f.der) return;
    if(f.g !== lastG){ FORM_ITEMS.push({type:'grp', key:'g-' + f.g, label:f.g}); lastG = f.g; }
    FORM_ITEMS.push({type:'fld', key:f.k, f});
  });
}

/* ==================== 行默认值与示例数据 ==================== */
export function S0(o){ return Object.assign({
  projectName:'',bidNo:'',pkg:'',agency:'',owner:'',region:'',method:'公开招标',bizType:'标识标牌',
  projType:'',industry:'',province:'',
  deadline:'',openTime:'',openAddr:'',
  budget:'',ourPrice:'',bondAmount:'',bondType:'银行转账',bondStatus:'已缴',perfAmount:'',perfStatus:'无需缴纳',
  bondIssuer:'',bondNo:'',bondPayDate:'',bondBackDate:'',bondDue:'',
  perfBackDate:'',perfIssuer:'',perfNo:'',perfPayDate:'',perfDue:'',
  lead:'',copilot:'',reviewer:'',
  bidStatus:'未开始',bidWaitAt:'',bidStartAt:'',bidEndAt:'',
  docFee:'',travelFee:'',sampleFee:'',
  result:'待开标',winner:'',winAmount:'',rank:'',noticeUrl:'',remark:''
}, o); }
const SAMPLE = [
  S0({projectName:'市人民医院导视系统升级改造',bidNo:'ZB-2026-0107',agency:'中招国际招标有限公司',owner:'市人民医院',region:'市区',method:'公开招标',deadline:'2026-01-07',openTime:'2026-01-09',budget:210,ourPrice:186,bondAmount:4,bondStatus:'已退',bondPayDate:'2026-01-04',bondBackDate:'2026-03-02',lead:'张伟',copilot:'陈曦',reviewer:'李静',result:'中标',winner:'本公司',winAmount:186,rank:'1'}),
  S0({projectName:'城投集团VI视觉识别系统',bidNo:'ZB-2026-0115',owner:'城投集团',region:'市区',method:'竞争性磋商',deadline:'2026-02-03',openTime:'2026-02-05',budget:120,ourPrice:108,bondAmount:2,bondStatus:'已退',bondPayDate:'2026-01-28',bondBackDate:'2026-04-10',lead:'李静',copilot:'王磊',reviewer:'张伟',result:'未中标',winner:'华艺设计顾问',rank:'2'}),
  S0({projectName:'区政务服务中心标识标牌采购',bidNo:'ZB-2026-0203',owner:'区行政审批局',region:'高新区',method:'公开招标',deadline:'2026-03-10',openTime:'2026-03-12',budget:95,ourPrice:82.6,bondAmount:2,bondStatus:'已退',bondPayDate:'2026-03-05',bondBackDate:'2026-05-08',lead:'王磊',copilot:'陈曦',reviewer:'李静',result:'中标',winner:'本公司',winAmount:82.6,rank:'1'}),
  S0({projectName:'高速服务区导视改造工程',bidNo:'ZB-2026-0221',owner:'省交投集团',region:'外市',method:'公开招标',deadline:'2026-03-24',openTime:'2026-03-26',budget:340,ourPrice:318,bondAmount:6,bondType:'银行保函',bondStatus:'已退',bondIssuer:'工商银行××分行',bondNo:'BH-2026-0302',bondPayDate:'2026-03-18',bondBackDate:'2026-06-15',bondDue:'2026-07-31',lead:'陈曦',copilot:'张伟',reviewer:'王磊',result:'废标',winner:'',rank:'',remark:'未按格式提供业绩证明'}),
  S0({projectName:'大学新校区楼体字工程',bidNo:'ZB-2026-0308',owner:'××大学',region:'外市',method:'公开招标',deadline:'2026-04-14',openTime:'2026-04-16',budget:150,ourPrice:142,bondAmount:3,bondStatus:'已退',bondPayDate:'2026-04-08',bondBackDate:'2026-07-02',lead:'张伟',copilot:'李静',reviewer:'陈曦',result:'未中标',winner:'天工标识工程',rank:'3'}),
  S0({projectName:'地铁站内导向标识一期',bidNo:'ZB-2026-0315',pkg:'一标段',agency:'建元工程咨询有限公司',owner:'市轨道交通集团',region:'市区',method:'公开招标',deadline:'2026-04-26',openTime:'2026-04-28',budget:460,ourPrice:436,bondAmount:8,bondType:'银行保函',bondStatus:'已退',bondIssuer:'招商银行××支行',bondNo:'BH-2026-0411',bondPayDate:'2026-04-20',bondBackDate:'2026-08-11',bondDue:'2026-08-30',lead:'李静',copilot:'王磊',reviewer:'张伟',result:'中标',winner:'本公司',winAmount:436,rank:'1'}),
  S0({projectName:'农商银行网点门头改造',bidNo:'ZB-2026-0402',owner:'农商银行',region:'市区',method:'框架入围',deadline:'2026-05-12',openTime:'2026-05-14',budget:150,ourPrice:128,bondAmount:3,bondType:'保险保单',bondStatus:'已缴',bondIssuer:'中国人保财险××分公司',bondNo:'PB-2026-0506',bondPayDate:'2026-05-06',bondDue:'2027-05-05',lead:'王磊',copilot:'陈曦',reviewer:'李静',result:'中标',winner:'本公司',winAmount:128,rank:'1',remark:'框架协议，保证金尚未退还'}),
  S0({projectName:'文旅景区标识系统提升',bidNo:'ZB-2026-0421',owner:'文旅投资公司',region:'外市',method:'竞争性谈判',deadline:'2026-05-25',openTime:'2026-05-27',budget:88,ourPrice:81,bondAmount:1.5,bondStatus:'已退',bondPayDate:'2026-05-18',bondBackDate:'2026-08-06',lead:'陈曦',copilot:'张伟',reviewer:'王磊',result:'未中标',winner:'卓越环境艺术',rank:'2'}),
  S0({projectName:'开发区道路指示牌制作安装',bidNo:'ZB-2026-0509',owner:'开发区管委会',region:'高新区',method:'公开招标',deadline:'2026-06-09',openTime:'2026-06-11',budget:110,ourPrice:97.5,bondAmount:2,bondStatus:'已退',bondPayDate:'2026-06-03',bondBackDate:'2026-09-02',lead:'张伟',copilot:'王磊',reviewer:'李静',result:'中标',winner:'本公司',winAmount:97.5,rank:'1'}),
  S0({projectName:'医院文化墙及宣传栏设计制作',bidNo:'ZB-2026-0518',owner:'市中医院',region:'市区',method:'竞争性磋商',deadline:'2026-06-22',openTime:'2026-06-24',budget:42,ourPrice:38,bondAmount:1,bondStatus:'已退',bondPayDate:'2026-06-16',bondBackDate:'2026-08-20',lead:'李静',copilot:'陈曦',reviewer:'张伟',result:'流标',winner:'',rank:'',remark:'有效供应商不足三家'}),
  S0({projectName:'商业综合体室内导视系统',bidNo:'ZB-2026-0607',owner:'××置业',region:'市区',method:'邀请招标',deadline:'2026-07-07',openTime:'2026-07-09',budget:260,ourPrice:238,bondAmount:4,bondStatus:'已退',bondPayDate:'2026-07-01',bondBackDate:'2026-10-09',lead:'王磊',copilot:'张伟',reviewer:'陈曦',result:'未中标',winner:'锐视标识科技',rank:'2'}),
  S0({projectName:'公交站台标识系统更新',bidNo:'ZB-2026-0626',owner:'市公交集团',region:'市区',method:'公开招标',deadline:'2026-07-21',openTime:'2026-07-23',budget:230,ourPrice:214,bondAmount:7,bondStatus:'已退',bondPayDate:'2026-07-15',bondBackDate:'2026-10-14',lead:'陈曦',copilot:'李静',reviewer:'王磊',result:'中标',winner:'本公司',winAmount:214,rank:'1'}),
  S0({projectName:'学校校园文化标识设计',bidNo:'ZB-2026-0703',owner:'区教育局',region:'高新区',method:'公开招标',deadline:'2026-08-11',openTime:'2026-08-13',budget:76,ourPrice:71,bondAmount:1.5,bondStatus:'已退',bondPayDate:'2026-08-05',bondBackDate:'2026-10-16',lead:'张伟',copilot:'王磊',reviewer:'李静',result:'未中标',winner:'博文文化传播',rank:'2'}),
  S0({projectName:'政务大厅窗口标识采购',bidNo:'ZB-2026-0725',owner:'区政务服务中心',region:'市区',method:'竞争性磋商',deadline:'2026-08-18',openTime:'2026-08-20',budget:70,ourPrice:63.8,bondAmount:1,bondStatus:'已退',bondPayDate:'2026-08-12',bondBackDate:'2026-10-19',lead:'李静',copilot:'张伟',reviewer:'陈曦',result:'中标',winner:'本公司',winAmount:63.8,rank:'1'}),
  S0({projectName:'园区智慧导视系统集成',bidNo:'ZB-2026-0806',owner:'××产业园',region:'外市',method:'公开招标',deadline:'2026-09-15',openTime:'2026-09-17',budget:320,ourPrice:305,bondAmount:6,bondType:'银行保函',bondStatus:'已缴',bondIssuer:'建设银行××支行',bondNo:'BH-2026-0909',bondPayDate:'2026-09-09',bondDue:'2026-11-20',lead:'王磊',copilot:'陈曦',reviewer:'李静',result:'未中标',winner:'中科智显科技',rank:'2'}),
  S0({projectName:'集团总部展厅布展及标识',bidNo:'ZB-2026-0822',owner:'××集团',region:'市区',method:'公开招标',deadline:'2026-09-22',openTime:'2026-09-24',budget:420,ourPrice:356,bondAmount:8,bondType:'保险保单',bondStatus:'已缴',bondIssuer:'中国平安财险××支公司',bondNo:'PA-2026-0916',bondPayDate:'2026-09-16',bondDue:'2026-12-31',lead:'陈曦',copilot:'王磊',reviewer:'张伟',result:'中标',winner:'本公司',winAmount:356,rank:'1'}),
  S0({projectName:'市人民医院标识标牌采购',bidNo:'ZB-2026-0905',agency:'中招国际招标有限公司',owner:'市人民医院',region:'市区',method:'公开招标',deadline:'2026-10-09',openTime:'2026-10-12',budget:190,ourPrice:168,bondAmount:3.8,bondStatus:'已退',bondPayDate:'2026-10-02',bondBackDate:'2026-10-24',lead:'张伟',copilot:'陈曦',reviewer:'李静',result:'中标',winner:'本公司',winAmount:168,rank:'1'}),
  S0({projectName:'集团园区导视系统改造',bidNo:'ZB-2026-0918',owner:'××集团',region:'市区',method:'公开招标',deadline:'2026-10-19',openTime:'2026-10-21',budget:145,ourPrice:136,bondAmount:2.9,bondStatus:'已缴',bondPayDate:'2026-10-12',lead:'李静',copilot:'王磊',reviewer:'张伟',result:'未中标',winner:'华艺广告有限公司',rank:'2'}),
  S0({projectName:'区政务中心宣传物料年度采购',bidNo:'ZB-2026-0925',owner:'区政务服务中心',region:'市区',method:'竞争性磋商',deadline:'2026-10-13',openTime:'2026-10-15',budget:60,ourPrice:54,bondAmount:1.2,bondStatus:'已退',bondPayDate:'2026-10-06',bondBackDate:'2026-10-26',lead:'王磊',copilot:'李静',reviewer:'陈曦',result:'未中标',winner:'恒信印务',rank:'3'}),
  S0({projectName:'地铁二号线导向标识补充',bidNo:'ZB-2026-0930',pkg:'二标段',owner:'市轨道交通集团',region:'市区',method:'公开招标',deadline:'2026-10-17',openTime:'2026-10-19',budget:105,ourPrice:92,bondAmount:2,bondStatus:'已缴',bondPayDate:'2026-10-10',lead:'陈曦',copilot:'张伟',reviewer:'王磊',result:'中标',winner:'本公司',winAmount:92,rank:'1'}),
  S0({projectName:'商业步行街导视标识改造',bidNo:'ZB-2026-1008',owner:'××街道办',region:'市区',method:'公开招标',deadline:'2026-10-21',openTime:'2026-10-23',budget:80,ourPrice:74,bondAmount:1.5,bondStatus:'已缴',bondPayDate:'2026-10-15',lead:'王磊',copilot:'陈曦',reviewer:'李静',result:'未中标',winner:'天工标识工程',rank:'2'}),
  S0({projectName:'学校楼体字及门牌维护',bidNo:'ZB-2026-1015',owner:'区教育局',region:'高新区',method:'竞争性磋商',deadline:'2026-10-25',openTime:'2026-10-27',budget:36,ourPrice:33,bondAmount:0.8,bondStatus:'已缴',bondPayDate:'2026-10-20',lead:'张伟',copilot:'李静',reviewer:'王磊',result:'待开标',winner:'',rank:''}),
  S0({projectName:'某新区党群服务中心标识采购',bidNo:'ZB-2026-1021',agency:'中招国际招标有限公司',owner:'新区管委会',region:'高新区',method:'公开招标',openTime:'2026-10-29',bondStatus:'无需缴纳',result:'不投',lead:'',remark:'已报名，因工期冲突决定不投（不计入投标数与废标率）'}),
  S0({projectName:'政务中心形象墙设计施工',bidNo:'ZB-2026-1019',owner:'区行政审批局',region:'市区',method:'竞争性磋商',deadline:'2026-10-27',openTime:'2026-10-29',budget:48,ourPrice:44,bondAmount:1,bondStatus:'未缴',lead:'王磊',copilot:'张伟',reviewer:'陈曦',result:'待开标',winner:'',rank:''}),
  S0({projectName:'城市书房标识配置',bidNo:'ZB-2026-1021',owner:'市文旅局',region:'市区',method:'公开招标',deadline:'2026-10-29',openTime:'2026-10-30',budget:52,ourPrice:47,bondAmount:1,bondStatus:'已缴',bondPayDate:'2026-10-22',lead:'李静',copilot:'陈曦',reviewer:'王磊',result:'待开标',winner:'',rank:''}),
  S0({projectName:'银行网点标识年度框架',bidNo:'ZB-2026-1022',owner:'××银行分行',region:'市区',method:'框架入围',deadline:'2026-11-10',openTime:'2026-11-12',budget:180,ourPrice:0,bondAmount:0,bondStatus:'无需缴纳',lead:'张伟',copilot:'王磊',reviewer:'李静',result:'待开标',winner:'',rank:''})
];
export { SAMPLE };

/* ==================== 行级纯函数 ==================== */
export function bondState(r){
  const bs = r.bondStatus || '';
  if(bs === '无需缴纳' || bs === '不予退还' || bs === '已退' || bs === '未缴') return bs;
  if(bs === '已缴') return (r.result === '待开标') ? '已缴' : '待退';
  return '未填';
}
/* 「不投」= 报名了但决定不投标：不进任何分母，仅单独计数展示 */
export function isNoBid(r){
  if(!r) return false;
  if(r.result === '不投') return true;
  return r.__bid === false;
}
export function isDecided(r){ return ['中标','未中标','废标','流标'].indexOf(r.result) >= 0; }

/* ==================== 标书进度（状态 + 投入时间） ==================== */
/* 老数据没有该字段时按「未开始」处理 */
export function bidStatusOf(r){ return (r && r.bidStatus) || '未开始'; }
/* 标书投入时间（天）：进行中时间 → 已结束时间（未结束则算到今天），不足一天按一天 */
export function bidEffortDays(r){
  const s = parseD(r && r.bidStartAt);
  if(!s) return '';
  const now = new Date(); now.setHours(0, 0, 0, 0);
  const e = parseD(r.bidEndAt) || now;
  return Math.max(1, Math.ceil((e.getTime() - s.getTime()) / 86400000));
}
/* 目标状态是否被锁定（非管理员不可回退；管理员任意改） */
export function bidStatusLocked(target, cur, isAdmin){
  if(isAdmin) return false;
  const a = BID_RANK[target], b = BID_RANK[cur || '未开始'];
  return a !== undefined && b !== undefined && a < b;
}
/* 状态变化时写入对应状态时间点（就地修改 o，返回是否有变化）：
   进行中 → 记录开始时间；已结束 → 记录结束时间（缺开始时间则补齐）；回退到未开始 → 清空后续时间 */
export function stampBidStatus(o, prev){
  const cur = String(o.bidStatus || '').trim();
  const old = String(prev || '').trim();
  if(!cur || cur === old) return false;
  const d = new Date();
  const today = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  if(cur === '进行中'){
    if(!o.bidStartAt) o.bidStartAt = today;
    o.bidEndAt = '';
  }else if(cur === '已结束'){
    o.bidEndAt = today;
    if(!o.bidStartAt) o.bidStartAt = today;
  }else{
    o.bidWaitAt = today;
    o.bidStartAt = '';
    o.bidEndAt = '';
  }
  return true;
}

const GUAR_TYPES = ['银行保函','保险保单','担保公司保函'];
export function bondKind(r){
  const t = r.bondType || '';
  if(t === '免缴' || r.bondStatus === '无需缴纳') return 'none';
  if(GUAR_TYPES.indexOf(t) >= 0) return 'guar';
  return 'fund';
}
export function shortType(t){
  const m = {'银行转账':'转账','银行保函':'保函','保险保单':'保单','担保公司保函':'担保保函','现金':'现金','免缴':'免缴'};
  return m[t] || String(t||'');
}

/* ==================== 导入：列名映射与归一化 ==================== */
export const ALIAS = {
 '项目':'projectName','工程名称':'projectName','标段名称':'projectName','项目/标段':'projectName',
 '项目编号':'bidNo','编号':'bidNo','招标文件编号':'bidNo',
 '标段':'pkg','包号':'pkg','标段号':'pkg','包件':'pkg',
 '采购单位':'owner','业主':'owner','招标人':'owner','采购人':'owner','建设单位':'owner','业主单位':'owner','客户':'owner',
 '所在地':'region','地区':'region','项目地址':'region',
 '代理机构':'agency','招标代理':'agency','代理公司':'agency','代理单位':'agency','招标代理机构':'agency','代理':'agency','代理方':'agency',
 '是否投标':'__bid','是否参与':'__bid','投标意向':'__bid','是否跟进':'__bid','投标决定':'__bid','决定':'__bid','参与情况':'__bid','是否报名':'__bid',
 '采购方式':'method','招标类型':'method',
 '投标截止':'deadline','投标截止日期':'deadline','递交截止时间':'deadline','交标时间':'deadline','截标时间':'deadline',
 '开标日期':'openTime','开标':'openTime','开标时间地点':'openTime',
 '预算':'budget','预算金额':'budget','最高限价':'budget','控制价':'budget','项目预算':'budget','限价':'budget',
 '投标报价':'ourPrice','报价':'ourPrice','我方报价':'ourPrice','投标价':'ourPrice','投标总价':'ourPrice',
 '保证金':'bondAmount','保证金金额':'bondAmount','投标保证金':'bondAmount','投标保证金金额':'bondAmount','保证金(元)':'bondAmount',
 '缴纳状态':'bondStatus','保证金缴纳状态':'bondStatus','保证金状态':'bondStatus','投标保证金状态':'bondStatus',
 '履约保证金':'perfAmount','履约保证金金额':'perfAmount','履约保证金缴纳状态':'perfStatus','履约保证金状态':'perfStatus',
 '项目类型':'projType','采购类型':'projType','行业':'industry','所属行业':'industry','行业类型':'industry',
 '省份':'province','所在省份':'province','所属省份':'province',
 '保证金形式':'bondType','缴纳形式':'bondType','保证金方式':'bondType','保证金类型':'bondType','保函类型':'bondType','形式':'bondType',
 /* 投标保函四项（旧的通用列名一律落到投标侧，保持历史表格可导入） */
 '开具行':'bondIssuer','保函开具行':'bondIssuer','出具单位':'bondIssuer','开户行':'bondIssuer','收款单位':'bondIssuer','保函出具银行':'bondIssuer',
 '投标保函开具行':'bondIssuer','投标保函开具行/收款单位':'bondIssuer','投标收款单位':'bondIssuer',
 '保函编号':'bondNo','凭证编号':'bondNo','保函数号':'bondNo','流水号':'bondNo',
 '投标保函编号':'bondNo','投标凭证编号':'bondNo','投标保函/凭证编号':'bondNo',
 '保函有效期':'bondDue','保函有效期至':'bondDue','保函到期日':'bondDue','有效期至':'bondDue',
 '投标保函有效期':'bondDue','投标保函有效期至':'bondDue',
 '缴纳日期':'bondPayDate','缴纳时间':'bondPayDate','开具日期':'bondPayDate',
 '投标缴纳日期':'bondPayDate','投标缴纳/开具日期':'bondPayDate',
 '退回日期':'bondBackDate','退款日期':'bondBackDate','解保日期':'bondBackDate',
 '投标保证金退回时间':'bondBackDate','投标退回时间':'bondBackDate','投标退回日期':'bondBackDate',
 /* 履约保函四项 */
 '履约保函开具行':'perfIssuer','履约保函开具行/收款单位':'perfIssuer','履约收款单位':'perfIssuer','履约开具行':'perfIssuer',
 '履约保函编号':'perfNo','履约凭证编号':'perfNo','履约保函/凭证编号':'perfNo',
 '履约缴纳日期':'perfPayDate','履约开具日期':'perfPayDate','履约缴纳/开具日期':'perfPayDate',
 '履约保函有效期':'perfDue','履约保函有效期至':'perfDue','履约保函到期日':'perfDue',
 '履约保证金退回时间':'perfBackDate','履约退回时间':'perfBackDate','履约退回日期':'perfBackDate',
 '负责人':'lead','标书负责人':'lead','编制人':'lead','制作人':'lead','责任人':'lead','标书制作人':'lead',
 '标书状态':'bidStatus','标书进度':'bidStatus','编写状态':'bidStatus','制作状态':'bidStatus',
 '未开始时间':'bidWaitAt','进行中时间':'bidStartAt','已结束时间':'bidEndAt',
 '开始编写时间':'bidStartAt','编写完成时间':'bidEndAt','标书开始时间':'bidStartAt','标书完成时间':'bidEndAt',
 '结果':'result','开标结果':'result','中标结果':'result','投标结果':'result',
 '购买标书费':'docFee','标书费':'docFee','标书购买费':'docFee','文件费':'docFee','购买招标文件费':'docFee','招标文件费':'docFee','标书工本费':'docFee',
 '差旅费':'travelFee','差旅':'travelFee','差旅费用':'travelFee','交通差旅费':'travelFee','出差费':'travelFee',
 '样品费':'sampleFee','样品费用':'sampleFee','打样费':'sampleFee','样品制作费':'sampleFee',
 '中标人':'winner','中标供应商':'winner','中标方':'winner','中标企业':'winner','中标公司':'winner',
 '中标金额':'winAmount','中标价':'winAmount','中标金额(万元)':'winAmount',
 '排名':'rank','名次':'rank',
 '公告链接':'noticeUrl','中标公告链接':'noticeUrl','中标公示链接':'noticeUrl','结果公告链接':'noticeUrl','公告网址':'noticeUrl','链接':'noticeUrl',
 '备注':'remark','说明':'remark','业务类型':'bizType','类型':'bizType'
};
export function normResult(v){
  const s = String(v||'').trim();
  if(!s) return '待开标';
  if(/不投|不参与|不报名|不跟|无意向|不投标|放弃投标|不参加/.test(s)) return '不投';
  if(/废标|无效标/.test(s)) return '废标';
  if(/流标|废流/.test(s)) return '流标';
  if(/放弃|弃标/.test(s)) return '放弃';
  if(/未中|没中|落标|失标/.test(s)) return '未中标';
  if(/中/.test(s) && !/未|没/.test(s)) return '中标';
  if(/待|未开|进行/.test(s)) return '待开标';
  return '待开标';
}
export function normBidStatus(v){
  const s = String(v||'').trim();
  if(!s) return '未开始';
  if(/未开始|未启动|待开始|未编写|未动|未着手/.test(s)) return '未开始';
  if(/已结束|结束|完成|定稿|已交|已递/.test(s)) return '已结束';
  if(/进行|编写|编制|制作|开始|启动|在写/.test(s)) return '进行中';
  return '未开始';
}
export function normBond(v){
  const s = String(v||'').trim();
  if(!s) return '未缴';
  if(/无需|不收|免交|不缴/.test(s)) return '无需缴纳';
  if(/不退|没收|扣除/.test(s)) return '不予退还';
  if(/已退|退回|退还|已返/.test(s)) return '已退';
  if(/已缴|已交|已付|已纳|已收/.test(s)) return '已缴';
  if(/未缴|未交|待缴|待交|应缴/.test(s)) return '未缴';
  return s;
}
export function normBondType(v){
  const s = String(v||'').trim();
  if(!s) return '银行转账';
  if(/担保公司|担保机构|担保保函/.test(s)) return '担保公司保函';
  if(/保函/.test(s)) return '银行保函';
  if(/保险|保单|保证保险/.test(s)) return '保险保单';
  if(/现金/.test(s)) return '现金';
  if(/免|不收|不缴/.test(s)) return '免缴';
  if(/转账|电汇|汇款|网银|银行缴/.test(s)) return '银行转账';
  return '银行转账';
}
export function normNum(v){
  if(v == null || v === '') return '';
  if(typeof v === 'number') return Math.round(v*100)/100;
  const s = String(v).replace(/[¥￥,\s]/g,'').replace(/万元|万/g,'');
  const m = s.match(/-?\d+(\.\d+)?/);
  return m ? Math.round(parseFloat(m[0])*100)/100 : '';
}
export function normDate(v){
  const d = parseD(v);
  return d ? d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) : '';
}
export function rowsFromMatrix(mtx){
  if(!mtx || !mtx.length) return [];
  let hi = 0;
  for(let i=0; i<Math.min(6, mtx.length); i++){
    const hit = mtx[i].filter(c => {
      const t = String(c||'').trim();
      return FMAP[ALIAS[t]] || F.some(f => f.l === t);
    }).length;
    if(hit >= 3){ hi = i; break; }
  }
  const head = mtx[hi].map(c => String(c||'').trim());
  const map = head.map(h => {
    const hit = F.find(f => f.l === h || f.k === h);
    if(hit) return hit.k;
    return ALIAS[h] || null;
  });
  const out = [];
  for(let i=hi+1; i<mtx.length; i++){
    const row = mtx[i];
    if(!row || !row.length) continue;
    const o = {}; let filled = 0;
    map.forEach((k,j) => {
      if(!k) return;
      if(FMAP[k] && FMAP[k].der) return;      // 派生字段不从表格导入（如标书投入时间）
      let v = row[j];
      if(v == null || v === '') return;
      if(k === '__bid'){
        o.__bid = !/^(否|不投|不参与|放弃|不跟|暂无|待定|n|no)$/i.test(String(v).trim());
        return;
      }
      if(k === 'result') v = normResult(v);
      else if(k === 'bidStatus') v = normBidStatus(v);
      else if(k === 'bondStatus' || k === 'perfStatus') v = normBond(v);
      else if(k === 'bondType') v = normBondType(v);
      else if(['budget','ourPrice','bondAmount','perfAmount','winAmount','docFee','travelFee','sampleFee'].indexOf(k) >= 0) v = normNum(v);
      else if(['deadline','openTime','bondPayDate','bondBackDate','bondDue',
               'perfPayDate','perfBackDate','perfDue','bidWaitAt','bidStartAt','bidEndAt'].indexOf(k) >= 0) v = normDate(v);
      else v = String(v).trim();
      o[k] = v; filled++;
    });
    /* 只标「不投」、没填开标结果时，结果落为「不投」而不是「待开标」 */
    if(o.__bid === false && (!o.result || o.result === '待开标')) o.result = '不投';
    if(filled && o.projectName) out.push(S0(o));
  }
  return out;
}

/* ==================== 去重键与清洗 ==================== */
export function rowKey(r){
  const no = String(r.bidNo||'').trim().toLowerCase();
  const pg = String(r.pkg||'').trim();
  if(no) return no + '|' + pg;
  return 'p|' + String(r.projectName||'').trim() + '|' + String(r.openTime||'');
}
export function cleanRow(r){
  const o = {};
  Object.keys(r).forEach(k => { if(k.indexOf('__') !== 0) o[k] = r[k]; });
  return o;
}
