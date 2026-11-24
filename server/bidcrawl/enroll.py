# -*- coding: utf-8 -*-
"""采集条目 → 投标管理项目字段（报名用）。

投标管理（台账）用的是 51 个字段：projectName / bidNo / owner / deadline /
openTime …（前端 frontend/src/lib/fields.js 的 F）。采集侧自己的标准字段是
title / buyer / budget / open_at …（bid_field_def），两套 key 完全不同。

本模块只做一件事：把抓到的公告（列表行 + 详情 AI 抽取结果 bid_item.detail）
翻译成台账那套 key，**取不到的字段一律留空字符串**——保证报名进来的行在
表格里与手工录入的行完全同构，不会出现「少了一列 / undefined」。

不含任何站点分支：能推出来的（省份 / 行业 / 类型 / 招标方式）用通用规则推，
推不出来的就空着，由人补。这是刻意的——宁可空，也不要编。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date, datetime

# 与 frontend/src/lib/fields.js 的 F 完全一致（顺序也一致，便于前端比对）
PROJECT_FIELDS = (
    'projectName', 'bidNo', 'pkg', 'agency', 'owner', 'region', 'method',
    'bizType', 'projType', 'industry', 'province',
    'winnerCount', 'taxRate', 'proxyAgent',
    'deadline', 'openTime', 'openAddr',
    'budget', 'ourPrice',
    'bondType', 'bondAmount', 'bondStatus', 'bondBackDate',
    'bondIssuer', 'bondNo', 'bondPayDate', 'bondDue',
    'perfAmount', 'perfStatus', 'perfBackDate',
    'perfIssuer', 'perfNo', 'perfPayDate', 'perfDue',
    'lead', 'copilot', 'reviewer',
    'bidStatus', 'bidWaitAt', 'bidStartAt', 'bidEndAt',
    'docFee', 'travelFee', 'sampleFee',
    'result', 'winner', 'winAmount', 'rank', 'noticeUrl', 'remark',
)
# 派生字段（前端算，不落库）：bidEffort = 标书投入天数
DERIVED_FIELDS = ('bidEffort',)

METHOD_OPTS = ('公开招标', '邀请招标', '竞争性谈判', '竞争性磋商', '单一来源',
               '框架入围', '询价')

PROVINCES = (
    '北京市', '天津市', '河北省', '山西省', '内蒙古自治区', '辽宁省', '吉林省',
    '黑龙江省', '上海市', '江苏省', '浙江省', '安徽省', '福建省', '江西省',
    '山东省', '河南省', '湖北省', '湖南省', '广东省', '广西壮族自治区', '海南省',
    '重庆市', '四川省', '贵州省', '云南省', '西藏自治区', '陕西省', '甘肃省',
    '青海省', '宁夏回族自治区', '新疆维吾尔自治区', '香港特别行政区',
    '澳门特别行政区', '台湾省',
)
PROVINCE_ALIAS = {
    '北京': '北京市', '天津': '天津市', '上海': '上海市', '重庆': '重庆市',
    '河北': '河北省', '山西': '山西省', '内蒙': '内蒙古自治区', '辽宁': '辽宁省',
    '吉林': '吉林省', '黑龙江': '黑龙江省', '江苏': '江苏省', '浙江': '浙江省',
    '安徽': '安徽省', '福建': '福建省', '江西': '江西省', '山东': '山东省',
    '河南': '河南省', '湖北': '湖北省', '湖南': '湖南省', '广东': '广东省',
    '广西': '广西壮族自治区', '海南': '海南省', '四川': '四川省', '贵州': '贵州省',
    '云南': '云南省', '西藏': '西藏自治区', '陕西': '陕西省', '甘肃': '甘肃省',
    '青海': '青海省', '宁夏': '宁夏回族自治区', '新疆': '新疆维吾尔自治区',
    '香港': '香港特别行政区', '澳门': '澳门特别行政区', '台湾': '台湾省',
}

# 行业判定：按「优先级从上到下」匹配，先命中先生效（银行 > 医院 > 学校 …）
_INDUSTRY_RULES = (
    ('金融银行', ('银行', '农商', '农信', '信用社', '邮储', '保险', '证券', '信托',
                  '金融', '银联', '基金', '资产管理')),
    ('医疗健康', ('医院', '卫生院', '疾控', '医疗', '卫健', '保健院', '血站',
                  '医药', '疾病预防')),
    ('教育院校', ('大学', '学院', '学校', '教育局', '中学', '小学', '幼儿园',
                  '职业技术学院', '技师学院', '培训中心')),
    ('交通运输', ('交通', '地铁', '轨道', '公路', '公交', '机场', '铁路', '高速',
                  '港务', '航运', '停车')),
    ('能源电力', ('电力', '电网', '能源', '石油', '石化', '燃气', '水务', '发电',
                  '核电', '新能源', '供热')),
    ('政府机关', ('政府', '管委会', '政务', '街道办', '公安', '税务', '财政',
                  '法院', '检察', '管理局', '行政审批', '事业单位', '党群')),
    ('文旅酒店', ('文旅', '景区', '旅游', '酒店', '文化', '博物', '图书馆',
                  '剧院', '体育馆', '会展')),
    ('商业/地产', ('置业', '地产', '商业', '综合体', '物业', '商场', '购物中心',
                   '写字楼', '产业园', '建设投资')),
    ('工业企业', ('制造', '工业', '科技', '电子', '机械', '化工', '钢铁', '材料',
                  '装备', '实业', '集团股份')),
)

_PROJ_TYPE_RULES = (
    ('工程', ('工程', '施工', '安装', '改造', '装修', '建设', '土建', '维修',
              '拆除', '亮化', '钢结构')),
    ('服务', ('服务', '运维', '维保', '设计', '咨询', '保洁', '物业', '印刷',
              '制作', '策划', '运营', '租赁', '检测', '审计')),
    ('货物', ('采购', '供货', '设备', '材料', '标识', '标牌', '物料', '用品',
              '家具', '产品', '货物')),
)

_METHOD_RULES = (
    ('单一来源', ('单一来源', '直接采购', '采前公示')),
    ('竞争性磋商', ('竞争性磋商', '磋商')),
    ('竞争性谈判', ('竞争性谈判', '谈判')),
    ('询价', ('询价', '比价')),
    ('框架入围', ('框架', '入围', '协议供货', '定点')),
    ('邀请招标', ('邀请招标', '邀标')),
    ('公开招标', ('公开招标', '招标公告', '公开采购')),
)


# ------------------------------------------------------------------ 基础清洗
def _s(v) -> str:
    if v is None:
        return ''
    if isinstance(v, (dict, list)):
        return ''
    return re.sub(r'\s+', ' ', str(v)).strip()


def _loads(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith('{'):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def _ymd(v) -> str:
    """任意时间表示 → 'YYYY-MM-DD'；解析不出返回空。"""
    if v in (None, ''):
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    s = _s(v)
    if not s:
        return ''
    m = re.search(r'(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})', s)
    if m:
        return '%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r'(\d{4})(\d{2})(\d{2})', s)
    if m:
        return '%s-%s-%s' % (m.group(1), m.group(2), m.group(3))
    return ''


def _dt(v) -> str:
    """任意时间表示 → 'YYYY-MM-DD HH:MM:SS'（投标截止/开标精确到秒）。

    采集侧 detail.deadline 形如 '2026-12-01 15:00'，open_at 是带时区的 datetime。
    没有时分部分时补 00:00:00——与台账存储格式一致，报名后无需再转换。
    """
    if v in (None, ''):
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d 00:00:00')
    s = _s(v)
    if not s:
        return ''
    m = re.search(r'(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})'
                  r'(?:[日\sT]+(\d{1,2})\s*[:时]\s*(\d{1,2})(?:\s*[:分]\s*(\d{1,2}))?)?', s)
    if m:
        return '%04d-%02d-%02d %02d:%02d:%02d' % (
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0))
    m = re.search(r'(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})', s)
    if m:
        return '%s-%s-%s %s:%s:%s' % m.groups()
    return ''


def _money(v) -> str:
    """金额统一成「元」，去掉小数尾巴；解析不出返回空串（不写 0）。"""
    if v in (None, ''):
        return ''
    if isinstance(v, (int, float)):
        return str(int(round(float(v))))
    s = _s(v).replace(',', '').replace('，', '')
    m = re.search(r'(\d+(?:\.\d+)?)', s)
    if not m:
        return ''
    try:
        num = float(m.group(1))
    except ValueError:
        return ''
    if '亿' in s:
        num *= 100000000
    elif '万' in s:
        num *= 10000
    return str(int(round(num)))


# ------------------------------------------------------------------ 语义推断
def guess_province(*texts) -> str:
    for t in texts:
        s = _s(t)
        if not s:
            continue
        for p in PROVINCES:                 # 全名优先，避免"河北"误伤
            if p in s:
                return p
        for alias, full in PROVINCE_ALIAS.items():
            if alias in s:
                return full
    return ''


def guess_industry(*texts) -> str:
    blob = ' '.join(_s(t) for t in texts if t)
    if not blob:
        return ''
    for label, words in _INDUSTRY_RULES:
        if any(w in blob for w in words):
            return label
    return ''


def guess_proj_type(*texts) -> str:
    blob = ' '.join(_s(t) for t in texts if t)
    for label, words in _PROJ_TYPE_RULES:
        if any(w in blob for w in words):
            return label
    return ''


def norm_method(notice_type: str, *texts) -> str:
    """采购方式 → 台账下拉选项；认不出就空着，让录入的人自己选。"""
    for raw in [_s(notice_type)] + [_s(t) for t in texts]:
        if not raw:
            continue
        for label, words in _METHOD_RULES:
            if any(w in raw for w in words):
                return label
    return ''


def _fix_url(url: str, raw: dict) -> str:
    """详情链接里出现空参数（…/detail?id=）时，用原始行里的同名字段补上。

    列表接口常把 id 放在 newsId / projectId 这类字段里，而配方里的模板写成 {id}，
    取不到就拼出个空参数——点开是白页。raw 里找「以该参数名结尾」的非空值兜底。
    """
    u = _s(url)
    if not u or '=' not in u:
        return u
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        sp = urlsplit(u)
        pairs = parse_qsl(sp.query, keep_blank_values=True)
    except Exception:
        return u
    if not any(not v for _k, v in pairs):
        return u
    fixed, changed = [], False
    for k, v in pairs:
        if not v:
            cand = raw.get(k)
            if not isinstance(cand, (str, int, float)) or not str(cand).strip():
                cand = next((x for kk, x in raw.items()
                             if kk.lower().endswith(k.lower())
                             and isinstance(x, (str, int, float))
                             and str(x).strip()), '')
            if cand:
                v = str(cand).strip()
                changed = True
        fixed.append((k, v))
    if not changed:
        return u
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(fixed), sp.fragment))


def clean_bid_no(v) -> str:
    """招标编号：公告里常写成「0730-2613JS0099-01，交通银行编号：CGXM…」。

    台账只要一个可检索的编号，取逗号/分号前的第一段，超长截断。
    """
    s = _s(v)
    if not s:
        return ''
    s = re.split(r'[，,；;、\n]', s)[0].strip()
    s = re.sub(r'^(?:招标|项目|采购)?(?:编号|标号)\s*[:：]\s*', '', s)
    return s[:60]


def clean_title(title: str) -> str:
    """去掉标题尾部的公告类型后缀：「…项目招标公告」→「…项目」。"""
    s = _s(title)
    s = re.sub(r'[\s\-—_]*(?:招标|采购|比选|竞争性磋商|竞争性谈判|询价|竞价)?'
               r'(?:公告|公示|通知|邀请|文件)\s*$', '', s)
    return s.strip() or _s(title)


# ------------------------------------------------------------------ 主入口
def to_project(item: dict) -> dict:
    """bid_item 行 → 台账 51 字段（缺失为 ''）。

    item 可以是接口里 SELECT * 的 dict，也可以是带 detail 的任意 dict。
    """
    item = dict(item or {})
    detail = _loads(item.get('detail'))
    if isinstance(detail.get('ai'), dict):
        detail = dict(detail)
    ai = _loads(detail.get('ai')) or {}
    # detail 里既有归一化后的标准字段，也有 ai 原始结果；ai 只补空缺
    def pick(*keys):
        for k in keys:
            v = detail.get(k)
            if v not in (None, ''):
                return v
        for k in keys:
            v = ai.get(k)
            if v not in (None, ''):
                return v
        return ''

    title = _s(item.get('title'))
    buyer = _s(pick('buyer')) or _s(item.get('buyer'))
    region = _s(pick('region')) or _s(item.get('region'))
    scope = _s(pick('scope'))
    buyer_s = buyer

    p = {k: '' for k in PROJECT_FIELDS}
    p['projectName'] = clean_title(title)
    p['bidNo'] = clean_bid_no(pick('bid_no'))
    p['agency'] = _s(pick('agency'))
    p['owner'] = buyer_s
    p['region'] = region
    p['province'] = guess_province(region, title, buyer_s)
    p['industry'] = guess_industry(buyer_s, title)
    p['projType'] = guess_proj_type(title, scope)
    p['method'] = norm_method(_s(pick('notice_type')), title)

    # 时间：投标截止（deadline）与开标时间（openTime）分开取，精确到秒。
    # 详情里只有合并的 open_at 时两者同值——但不要因此少写一个。
    # 保持 YYYY-MM-DD HH:MM:SS，与台账表单/展示一致，报名后无需再转换格式。
    p['deadline'] = _dt(pick('deadline')) or _dt(item.get('doc_get_end')) \
        or _dt(pick('doc_get_end'))
    p['openTime'] = _dt(item.get('open_at')) or _dt(pick('open_at')) \
        or p['deadline']
    p['openAddr'] = _s(pick('bid_place'))

    # 基础信息新增三项：公告里通常只有中标人数量，税率/委托代理人多为人工补录
    p['winnerCount'] = _s(pick('bidder_limit'))
    p['taxRate'] = ''
    p['proxyAgent'] = ''

    budget = _money(item.get('budget_yuan')) or _money(pick('budget'))
    p['budget'] = budget

    p['noticeUrl'] = _fix_url(_s(item.get('url')), item.get('raw')
                              if isinstance(item.get('raw'), dict) else {})
    p['result'] = '待开标'
    p['bidStatus'] = '未开始'
    p['bondStatus'] = '未缴'
    p['bondType'] = '银行转账'
    p['perfStatus'] = '无需缴纳'
    # bizType 不在公告里，不猜（猜「标识标牌」对硬件维保项目就是错的）

    bits = []
    if scope:
        bits.append('采购范围：' + scope)
    if _s(pick('fee_note')):
        bits.append('报名费：' + _s(pick('fee_note')))
    if _s(pick('project_period')):
        bits.append('工期：' + _s(pick('project_period')))
    if _s(item.get('platform_id')):
        bits.append('采集自 ' + _s(item.get('platform_id')))
    p['remark'] = '；'.join(bits)[:500]
    return p


def dedupe_key(project: dict) -> str:
    """与台账 rowKey 同口径：招标编号+标段，退化到 项目名+开标时间。"""
    no = _s(project.get('bidNo')).lower()
    pkg = _s(project.get('pkg'))
    if no:
        return no + '|' + pkg
    return 'p|' + _s(project.get('projectName')) + '|' + _s(project.get('openTime'))


def new_rid() -> str:
    """与前端 uid() 同构：r + base36 时间戳 + 4 位随机。"""
    ts = ''
    n = int(time.time() * 1000)
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    while n:
        ts = digits[n % 36] + ts
        n //= 36
    return 'r' + (ts or '0') + uuid.uuid4().hex[:4]
