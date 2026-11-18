# -*- coding: utf-8 -*-
"""P0 自检：不依赖外网，用本地 fixture 服务验证抽取 / 翻页 / 页宽探测 / 归一化 / 过滤。

    python -m server.bidcrawl.selftest
    set BIDCRAWL_SELFTEST_DB=1 && python -m server.bidcrawl.selftest   # 额外验证任务幂等
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault('BIDCRAWL_RATE_SCALE', '0')

from . import config, filter as bfilter, normalize, paginate, recipe as recipe_mod  # noqa: E402
from .transport import Fetcher  # noqa: E402

TOTAL = 120
HARD_CAP = 50


def _make_items(page: int, size: int) -> list[dict]:
    start = (page - 1) * size
    out = []
    for i in range(start, min(start + size, TOTAL)):
        n = i + 1
        out.append({
            'id': n,
            'noticeTitle': '门头标识灯箱制作项目 %03d' % n,
            'purchaser': ('某某烟草公司' if n % 2 else '某某银行') + str(n),
            'publishTime': '2026-11-%02d 09:30:00' % (10 + n % 9),
            'signEndTime': '2026-12-%02d 17:00:00' % (1 + n % 20),
            'areaName': '江苏南京',
        })
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = 'application/json'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/api/list':
            return self._send(404, b'{}')
        length = int(self.headers.get('content-length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        page = int(body.get('page') or 1)
        size = min(int(body.get('pageSize') or 20), HARD_CAP)
        items = _make_items(page, size)
        payload = {'code': 0, 'data': {'total': TOTAL, 'list': items, 'page': page,
                                       'pageSize': size}}
        self._send(200, json.dumps(payload, ensure_ascii=False).encode())

    def do_GET(self):
        u = urlparse(self.path)
        m = re.match(r'^/html/list(?:_(\d+))?\.jspx$', u.path)
        if u.path == '/html/list' or m:
            qs = parse_qs(u.query)
            page = int(m.group(1)) if m else int((qs.get('page') or ['1'])[0])
            rows = ''.join(
                '<li class="row"><a href="/notice/%d">标识标牌采购 %03d</a>'
                '<span class="buyer">中烟集团</span>'
                '<span class="date">2026-11-12</span></li>' % (n, n)
                for n in range(page * 10 + 1, page * 10 + 6))
            html = ('<html><body><ul id="list">%s</ul>'
                    '<a href="/html/list_%d.jspx">下一页</a></body></html>'
                    % (rows, page + 1))
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if u.path.startswith('/notice/'):
            html = ('<html><body><div class="purchaser">某某烟草公司</div>'
                    '<div class="content">  采购范围：门头灯箱制作及安装  </div>'
                    '<p>预算金额：123.45万元</p>'
                    '<p>获取时间：2026-11-10 至 2026-11-20</p>'
                    '<p>开标时间：2026-12-01 14:00</p></body></html>')
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if u.path == '/waf/detail':
            # 阿里云 WAF 的 JS 挑战页：没有正文，只有 arg1 + 写 cookie 的脚本
            html = ("<html><script>var arg1='30B869B3DAED0C5F9EAEB03B6621FED4D2A43728';"
                    "function setCookie(n,v){document.cookie=n+'='+v;}"
                    "function reload(x){setCookie('acw_sc__v2',x);"
                    "document.location.reload();}</script></html>")
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if u.path == '/waf/rishu':
            # 瑞数：412 + $_ts 动态脚本
            html = ("<html><head><meta id='abc123' content='xyz' r='m'>"
                    "<script>$_ts=window['$_ts'];if(!$_ts)$_ts={};"
                    "$_ts.nsd=86073;$_ts.cd='qEe3rcqlEfVqkkWm';</script>"
                    "</head><body></body></html>")
            return self._send(412, html.encode(), 'text/html; charset=utf-8')
        return self._send(404, b'{}')


def _fixture_recipe(base: str) -> dict:
    return {
        'meta': {'id': 'fixture', 'name': '本地夹具', 'host': '127.0.0.1'},
        'transport': {'kind': 'http', 'timeout_s': 10, 'trust_env': False,
                      'headers': {'Content-Type': 'application/json'},
                      'rate_limit': {'qps': 0}},
        'discovery': {
            'mode': 'json_api',
            'request': {'url': base + '/api/list', 'method': 'POST',
                        'body': {'keyword': '{keyword}', 'page': '{page}',
                                 'pageSize': '{size}'}},
            'pagination': {'type': 'page', 'page_param': 'page', 'start': 1,
                           'size_param': 'pageSize', 'size': 50,
                           'size_probe': [20, 50, 100], 'size_max': 50,
                           'size_cap_behavior': 'ok', 'total_path': '$.data.total',
                           'max_pages': 10,
                           'stop_when': ['empty_items', 'page_unchanged', 'reached_total']},
            'items_path': '$.data.list[*]',
            'fields': {
                'title': {'path': 'noticeTitle'},
                'url': {'template': base + '/notice/{id}'},
                'buyer': {'path': 'purchaser'},
                'publish_at': {'path': 'publishTime'},
                'doc_get_end': {'path': 'signEndTime'},
                'region': {'path': 'areaName'},
                'source_name': {'const': '夹具'},
            },
        },
        'detail': {'enabled': True,
                   'request': {'url': '{url}', 'method': 'GET'},
                   'extract': {'scope': 'html', 'fields': {
                       'budget': {'regex': '预算金额[:：]\\s*([\\d.,]+\\s*万元?)', 'group': 1},
                       'open_at': {'regex': '开标时间[:：]\\s*([^\\n<]+)', 'group': 1},
                       'scope': {'css': '.content', 'strip': True},
                   }}},
        'normalize': {'filters': {'inherit': 'global'}},
        'validation': {'min_items_per_keyword': 1, 'page2_must_differ': True,
                       'expected_nonempty': {'buyer': 0.6, 'doc_get_end': 0.6}},
    }


def _check(name, cond, detail=''):
    print('%s %s%s' % ('PASS' if cond else 'FAIL', name,
                       ('  → ' + str(detail)) if detail and not cond else ''))
    return bool(cond)


def _generator_selftest(base: str) -> bool:
    """验证新版生成器：语义护栏、配方结构、真实试跑校验（仅打本地夹具）。"""
    from . import ai_analyze as ai

    ok = True
    # 1. 模型串味映射必须被护栏挡下，精确匹配不能被覆盖
    heu, strong = ai._heuristic_map(['userName', 'bidsNo'], ai.DETAIL_FIELD_MAP)
    merged = ai._merge_map(heu, strong,
                           {'agency': 'userName', 'contact': 'userName',
                            'bid_no': 'bidsNo'}, ['userName', 'bidsNo'])
    ok &= _check('生成器 字段语义护栏',
                 merged == {'bid_no': 'bidsNo'}, merged)

    ev = {
        'entry': {'title': '本地夹具'},
        'entry_url': base + '/list',
        'js_signals': {'state_defaults': {'searchNoticeType': '1'}},
        'list': {
            'url': base + '/api/list', 'method': 'POST',
            'payload': {'noticeType': '', 'page': '{page}',
                        'pageSize': '{size}', 'briefContent': '{keyword}'},
            'payload_kind': 'json',
            'items_path': '$.data.list[*]', 'total_path': '$.data.total',
            'roles': {'page': 'page', 'size': 'pageSize',
                      'keyword': 'briefContent'},
            'keys': ['id', 'noticeTitle', 'purchaser', 'publishTime',
                     'signEndTime', 'areaName'],
            'rows_sample': [{'id': 1, 'noticeTitle': '门头灯箱项目',
                             'purchaser': '某某银行'}],
            'keyword_key': 'briefContent', 'keyword_hit': 1.0,
            'page2_differs': True, 'endpoint_path': '/api/list',
            'count': 50, 'total': TOTAL,
        },
        'detail': None,
        'detail_url_template': base + '/notice/{id}',
    }
    recipe, source = ai.build_recipe(ev, base + '/list', '门头', with_llm=False)
    errs = recipe_mod.validate_recipe(recipe)
    ok &= _check('生成器 配方结构合法', not errs, errs)
    ok &= _check('生成器 json_api + 字段映射',
                 recipe['discovery']['mode'] == 'json_api' and
                 recipe['discovery']['fields']['title']['path'] == 'noticeTitle' and
                 recipe['discovery']['fields']['buyer']['path'] == 'purchaser' and
                 source == 'heuristic', recipe['discovery']['fields'])
    ok &= _check('生成器 详情页 URL 模板',
                 recipe['discovery']['fields']['url']['template'] ==
                 base + '/notice/{id}',
                 recipe['discovery']['fields']['url'])
    ok &= _check('生成器 回填 JS 默认值',
                 recipe['discovery']['request']['body']['noticeType'] == '1',
                 recipe['discovery']['request']['body'])
    ok &= _check('生成器 默认关闭采购人白名单',
                 recipe['normalize']['filters']['buyer_whitelist'] is False and
                 recipe['meta']['status'] == 'paused', recipe['normalize'])
    ok &= _check('生成器 网络条件与探测一致',
                 recipe['transport']['trust_env'] is False,
                 recipe['transport'].get('trust_env'))
    vr = ai.verify_recipe(recipe, '门头')
    ok &= _check('生成器 试跑校验通过', vr['valid'] and vr['items'] > 0 and
                 vr['field_coverage'].get('title') == 1.0, vr)
    # 配方必须能被生产链路的 Fetcher / paginate 直接执行（含 trust_env 条件）
    compiled = recipe_mod.compile_recipe(recipe)
    with Fetcher(compiled) as f:
        first = paginate.fetch_first(compiled, f, '门头')
    ok &= _check('生成器 配方可被生产链路直接执行',
                 len(first.items) > 0 and bool(first.items[0].get('title')),
                 (len(first.items), first.items[0].get('title') if first.items else ''))
    return ok


def _html_pdf_selftest() -> bool:
    """HTML 列表 + goSearch/page + PDF 正文形态（cg.11185.cn）离线回归。"""
    from parsel import Selector

    from . import ai_analyze as ai, detail_ai, pdf_text
    from .extract.html_list import extract_items
    from .transport import FetchResult

    ok = True
    viewer = ('https://cg.11185.cn/resource/site_1/gxyz/css/pdfjs/web/viewer.html'
              '?file=https://cg.11185.cn/dzzb/cgUploadController.do?'
              'openFileById%26id%3D8a846633a1b039ac01a1d3f9a69b2ad4&page=1')
    detail_html = ("<div id='showPdf'><iframe id='pdfContainer' src='%s'></iframe></div>"
                   % viewer)
    pdf_cfg = {
        'enabled': True,
        'file_id_regex': r'openFileById(?:%26|&)(?:id|fileId)(?:%3D|=)([A-Za-z0-9_-]+)',
        'url_template': 'https://cg.11185.cn/dzzb/cgUploadController.do?openFileById&id={file_id}',
        'iframe_css': 'iframe#pdfContainer',
    }
    purl = pdf_text.pdf_url_from_html(pdf_cfg, detail_html, 'https://cg.11185.cn/x.html')
    ok &= _check('PDF viewer 反转义出直链',
                 purl.endswith('id=8a846633a1b039ac01a1d3f9a69b2ad4'), purl)
    ok &= _check('PDF 通用 viewer 解析（无配方声明）',
                 pdf_text.pdf_url_from_html({}, detail_html,
                                            'https://cg.11185.cn/x.html') == purl)
    ok &= _check('PDF 非文件字节优雅降级',
                 pdf_text.bytes_to_text(b'not a pdf') == '')
    ok &= _check('中文「点」时刻转 ISO',
                 detail_ai.cn_dt_to_iso('2026年12月04日09点30分') == '2026-12-04 09:30',
                 detail_ai.cn_dt_to_iso('2026年12月04日09点30分'))

    row_html = ('<li><a href="https://x.cn/biddingBulletin/2026-11-11/142770.html">'
                '<h1>门头灯箱项目招标公告</h1><ul class="newsinfo">'
                '<li>项目编号：<span>ABC202611019</span></li>'
                '<li>采购方式：<span>公开招标</span></li></ul>'
                '<div class="newsDate"><div>2026</div> 11/11</div></a></li>')
    field_specs = {
        'title': {'css': 'h1::text', 'strip': True},
        'url': {'css': 'a', 'attr': 'href'},
        'external_id': {'regex': r'/(\d{3,})\.html', 'group': 1},
        'publish_at': {'regex': r'\b(20\d{2})[^\d]{1,40}?(\d{1,2})[/-](\d{1,2})\b',
                       'template': '{1}-{2}-{3} 00:00'},
        'bid_no': {'css': 'li span::text', 'nth': 0, 'strip': True},
        'notice_type': {'css': 'li span::text', 'nth': 1, 'strip': True},
    }
    ok &= _check('regex+template 拼接跨标签日期',
                 recipe_mod.resolve_field(field_specs['publish_at'], {},
                                           text=row_html) == '2026-11-11 00:00',
                 recipe_mod.resolve_field(field_specs['publish_at'], {}, text=row_html))
    row_sel = Selector(text=row_html).css('li')[0]
    ok &= _check('CSS nth 取重复结构第 2 项',
                 recipe_mod.resolve_field(field_specs['notice_type'], {},
                                           selector=row_sel) == '公开招标',
                 recipe_mod.resolve_field(field_specs['notice_type'], {}, selector=row_sel))

    ev = {
        'entry': {'title': '本地夹具'},
        'entry_url': 'https://x.cn/',
        'html_list': {
            'search': {'url': 'https://x.cn/search.html',
                       'params': {'goSearch': '{keyword}', 'page': '{page}'},
                       'keyword_param': 'goSearch', 'page_param': 'page', 'raw': ''},
            'search_url': 'https://x.cn/search.html?goSearch=x&page=1',
            'row_selector': 'ul.newslist > li',
            'fields': field_specs,
            'rows': 3, 'total_pages': 1, 'page2_differs': True,
            'first_url': 'https://x.cn/biddingBulletin/2026-11-11/142770.html',
            'detail_path': '/biddingBulletin/2026-11-11/142770.html',
            'sample': [{'title': '门头灯箱项目招标公告'}],
            'keyword_hit': 1.0,
            'pdf': dict(pdf_cfg, pdf_url=purl, viewer_url=viewer),
        },
    }
    recipe, _src = ai.build_html_recipe(ev, 'https://x.cn/', '门头', with_llm=False)
    errs = recipe_mod.validate_recipe(recipe)
    ok &= _check('HTML+PDF 配方结构合法', not errs, errs)
    ok &= _check('HTML+PDF 配方启用 pdf 与 ai',
                 recipe['detail']['pdf']['enabled'] and
                 recipe['detail']['ai']['enabled'] and
                 recipe['discovery']['mode'] == 'html_list')
    rec = recipe_mod.compile_recipe(recipe)
    res = FetchResult(ok=True, status=200, url='https://x.cn/search.html',
                      text='<ul class="newslist">%s</ul>' % (row_html * 3),
                      content_type='text/html')
    items = extract_items(rec, res)
    ok &= _check('HTML+PDF 配方抽到 3 条', len(items) == 3, len(items))
    ok &= _check('HTML+PDF 配方日期与 ID 非空',
                 bool(items) and all(it.get('publish_at') and it.get('external_id')
                                     for it in items),
                 [it.get('publish_at') for it in items])

    # detail_ai 必须走 PDF 分支；用假文本替身避免真实下载
    fake_text = '预算金额：100万元 开标时间：2026年12月04日09点30分'
    orig = pdf_text.text_from_result
    pdf_text.text_from_result = lambda *a, **k: (fake_text, purl)
    try:
        text = detail_ai.content_from_result(
            recipe, FetchResult(ok=True, status=200, url='https://x.cn/d.html',
                                text=detail_html, content_type='text/html'))
    finally:
        pdf_text.text_from_result = orig
    ok &= _check('detail_ai 正文取自 PDF', text == fake_text, text[:80])
    fb = detail_ai.regex_fallback(fake_text, recipe['detail']['ai']['fallback_regex'])
    ok &= _check('PDF 文本降级正则抽到金额',
                 '100万元' in str(fb.get('budget') or ''), fb)

    # ---- PDF 解析多后端降级 / 失败可诊断（完全离线，不联网） ----
    def tiny_pdf(text='BID TEST 2026'):
        objs = [b'<< /Type /Catalog /Pages 2 0 R >>',
                b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                b'/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>']
        stream = ('BT /F1 12 Tf 72 720 Td (%s) Tj ET\n' % text).encode()
        objs.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'endstream')
        objs.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
        buf = bytearray(b'%PDF-1.4\n')
        offsets = []
        for i, obj in enumerate(objs, 1):
            offsets.append(len(buf))
            buf += ('%d 0 obj\n' % i).encode() + obj + b'\nendobj\n'
        xref = len(buf)
        buf += ('xref\n0 %d\n' % (len(objs) + 1)).encode() + b'0000000000 65535 f \n'
        for off in offsets:
            buf += ('%010d 00000 n \n' % off).encode()
        buf += ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
                % (len(objs) + 1, xref)).encode()
        return bytes(buf)

    raw_pdf = tiny_pdf()
    ex = pdf_text.extract_text_ex(raw_pdf)
    ok &= _check('PDF 多后端自动降级抽到文本',
                 ('BID' in ex['text'] and '2026' in ex['text']) or
                 'pip install pypdf' in ex['reason'], ex)
    ok &= _check('缺 PDF 解析库时给出可操作提示',
                 'pip install pypdf' in
                 pdf_text.extract_text_ex(raw_pdf, backends=[])['reason'])
    ok &= _check('bytes_to_text 兼容旧接口',
                 'BID' in pdf_text.bytes_to_text(raw_pdf) or
                 not pdf_text.extract_text_ex(raw_pdf)['available'])

    class _PdfResp:
        status_code = 200
        headers = {'content-type': 'application/pdf;charset=utf-8',
                   'content-disposition': 'attachment;filename="notice.pdf"'}
        content = raw_pdf

    class _HtmlResp:
        status_code = 200
        headers = {'content-type': 'text/html'}
        content = b'<!DOCTYPE html><html>login</html>'

    orig_get = pdf_text.httpx.get
    try:
        pdf_text.httpx.get = lambda *a, **k: _PdfResp()
        text, pmeta = pdf_text.download_and_extract('https://x.cn/dzzb?id=abc', {})
        ok &= _check('附件下载链接按二进制解析并读出文件名',
                     pmeta['filename'] == 'notice.pdf' and
                     pmeta['bytes'] == len(raw_pdf) and
                     bool(pmeta['backend']) and 'BID' in text, pmeta)
        pdf_text.httpx.get = lambda *a, **k: _HtmlResp()
        _t, hmeta = pdf_text.download_and_extract('https://x.cn/dzzb?id=abc', {})
        ok &= _check('非 PDF 响应给出明确原因',
                     '不是 PDF' in hmeta['reason'], hmeta['reason'])
    finally:
        pdf_text.httpx.get = orig_get
    return ok


def _nested_api_selftest() -> bool:
    """嵌套 dto 接口 / 列表页隐藏域 / HTML 正文（cg.psbc.com 形态）离线回归。"""
    from . import ai_analyze, detail_ai, probe
    from .normalize import parse_datetime
    from .transport import FetchResult, _render_deep

    ok = True
    js = ('function getData(){ $.ajax({type:"POST",'
          'url:"/cms/api/dynamicData/queryContentPage",'
          'contentType:"application/json; charset=utf-8",'
          'data: JSON.stringify({pageNo:1,pageSize:10,'
          'dto:{siteId:siteId,categoryId:"222",isIndex:"1"}})});}')
    sig = probe.parse_js_signals([{'url': '/x/index.js', 'tag': 'list', 'text': js}])
    tpl = next((t for t in sig['templates'] if 'queryContentPage' in t['url']), None)
    ok &= _check('JS 嵌套 dto 请求模板解析',
                 bool(tpl) and tpl['data']['dto'].get('categoryId') == '222'
                 and tpl['content_type'] == 'json', tpl)

    hidden = ('<input type="hidden" name="chunkSiteId" value="725"/>'
              '<input type="hidden" name="chunkId" value="222"/>'
              '<input type="hidden" name="cms" value="/cms"/>')
    defaults = probe.parse_hidden_defaults(hidden)
    ok &= _check('列表页隐藏域别名映射',
                 defaults.get('siteId') == '725'
                 and defaults.get('categoryId') == '222'
                 and defaults.get('cms') == '/cms', defaults)

    nav = ('<a href="/cms/default/webfile/1ywgg1/index.html">招标信息</a>'
           '<a href="/cms/default/webfile/1ywgg2/20261113/'
           '1305955610516258816.html">某公告</a>')
    pages = probe.discover_list_pages(nav, 'https://cg.psbc.com/')
    ok &= _check('导航发现列表页并排除文章页',
                 pages == ['https://cg.psbc.com/cms/default/webfile/1ywgg1/index.html'],
                 pages)

    js2 = ('function toSearch(){ var config={url: server+cms+"/api/search/searchKeyWord",'
           'data:{pageNo:pageNo,pageSize:pageSize,'
           'dto:{keyWords:keyword,siteId:siteId,categoryId:categoryId}}};'
           'EasyAjax.post_json(config, cb);}')
    sig2 = probe.parse_js_signals([{'url': '/x/search.js', 'tag': 'list', 'text': js2}])
    tpl2 = next((t for t in sig2['templates'] if 'searchKeyWord' in t['url']), None)
    ok &= _check('config 变量写法也能解析请求体',
                 bool(tpl2) and tpl2['data']['dto'].get('categoryId') == ''
                 and tpl2['data']['dto'].get('keyWords') == '', tpl2)

    ep = {'path': '/cms/api/search/searchKeyWord', 'template': tpl2}
    variants = probe.SiteProbe._payload_variants(
        ep, {'siteId': '725', 'categoryId': '222', 'cms': '/cms'}, 'keyWords')
    styles = {s for _, _, s in variants}
    minimal = next((p for p, _, s in variants if s == 'minimal'), {})
    ok &= _check('请求体变体含 template 与 minimal',
                 'template' in styles and 'minimal' in styles, styles)
    ok &= _check('minimal 去掉分类但保留站点',
                 minimal.get('dto', {}).get('siteId') == '725'
                 and 'categoryId' not in minimal.get('dto', {}), minimal)

    body = {'pageNo': '{page}', 'pageSize': '{size}',
            'dto': {'keyWords': '{keyword}', 'siteId': '725'}}
    rend = _render_deep(body, {'page': 2, 'size': 50, 'keyword': '门头'})
    ok &= _check('生产链路递归渲染嵌套请求体',
                 rend['pageNo'] == 2 and rend['pageSize'] == 50
                 and rend['dto']['keyWords'] == '门头', rend)

    ok &= _check('Java Date 日期解析',
                 str(parse_datetime('Tue Apr 01 09:17:32 UTC 2025'))[:10] == '2025-04-01',
                 parse_datetime('Tue Apr 01 09:17:32 UTC 2025'))

    page = ('<html><div class="content">短</div><div class="content">'
            + ('门头标识采购公告。' * 30) + '</div></html>')
    res = FetchResult(ok=True, status=200, url='https://x.cn/d.html',
                      text=page, content_type='text/html')
    recipe = {'meta': {'id': 'x'},
              'detail': {'enabled': True, 'content_css': '.content',
                         'ai': {'enabled': True, 'max_chars': 20000}}}
    text = detail_ai.content_from_result(recipe, res)
    ok &= _check('HTML content_css 取最长匹配', len(text) >= 200, len(text))

    # 6f. 入口 URL 本身是接口（jc.abchina.com.cn 形态）离线回归
    abc_url = ('https://jc.abchina.com.cn/gateway/puc/portalMessage/'
               'queryInviteMessageForEie')
    ok &= _check('接口型入口 URL 识别', probe.SiteProbe._looks_like_api_url(abc_url))
    ok &= _check('非接口页面不误判',
                 not probe.SiteProbe._looks_like_api_url('https://cg.psbc.com/'))
    # 网关接口 WAF：Accept 含 text/javascript,q=0.01 会被判 XSS，通用头修复
    ok &= _check('JSON 请求头避开 WAF 误判',
                 'text/javascript' not in probe.JSON_HEADERS.get('Accept', '')
                 and 'q=0.01' not in probe.JSON_HEADERS.get('Accept', ''),
                 probe.JSON_HEADERS.get('Accept'))
    # 嵌套分页 payload 模板（pageInfo.current/size + 顶层关键词）
    p = probe.SiteProbe(abc_url, '门楣')
    local = {'path': '/gateway/puc/portalMessage/queryInviteMessageForEie',
             'template': {'url': '/gateway/x', 'method': 'POST', 'content_type': 'json',
                          'data': {'pageInfo': {'current': '{page}', 'size': '{size}'}}},
             'param_keys': {'messageTitle': ''}, 'score': 24, 'source': abc_url}
    payloads = p._payload_variants(local, {}, 'messageTitle')
    first = _render_deep(payloads[0][0], {'page': 1, 'size': 10, 'keyword': '门楣'})
    p.close()
    ok &= _check('入口接口嵌套 pageInfo 请求体',
                 first.get('pageInfo', {}).get('current') == 1
                 and first.get('messageTitle') == '门楣', first)
    # 生成器字段候选覆盖农行字段名
    ok &= _check('生成器识别 messageId/messageTitle/startTime',
                 all(k in str(ai_analyze.LIST_FIELD_MAP)
                     for k in ('messageId', 'messageTitle', 'startTime')),
                 ai_analyze.LIST_FIELD_MAP.get('external_id'))

    # 6g. SPA 无头嗅探：请求体参数化（纯离线，不启动浏览器）
    from . import browser_probe
    req = {'url': 'https://jc.abchina.com.cn/gateway/puc/portalMessage/queryInviteMessageForEie',
           'post_data': json.dumps({'pageInfo': {'current': 1, 'size': 10},
                                    'beginTime': None, 'messageType': '',
                                    'publishColumn': 2}, ensure_ascii=False)}
    bp = probe.SiteProbe('https://jc.abchina.com.cn/puc/#/biddingPage', '门楣')
    bpayload, broles = bp._browser_payload(req, [{'messageTitle': 'x'}])
    bp.close()
    ok &= _check('嗅探请求体参数化分页',
                 bpayload['pageInfo']['current'] == '{page}'
                 and bpayload['pageInfo']['size'] == '{size}', bpayload)
    ok &= _check('嗅探请求体注入关键词字段',
                 broles.get('keyword') == 'messageTitle'
                 and bpayload.get('messageTitle') == '{keyword}', broles)
    ok &= _check('嗅探请求体保留业务常量',
                 bpayload.get('publishColumn') == 2, bpayload)

    # Referer 规范化：hash 路由不能带 #（网关会判来源非法）
    ok &= _check('Referer 去掉 hash 路由',
                 ai_analyze._referer_of(
                     'https://jc.abchina.com.cn/puc/#/biddingPage')
                 == 'https://jc.abchina.com.cn/puc/',
                 ai_analyze._referer_of('https://jc.abchina.com.cn/puc/#/biddingPage'))

    # 嗅探模块自检：无浏览器时给出可操作原因，而不是抛异常
    bres = browser_probe.sniff('http://127.0.0.1:1/', '', timeout_s=1) \
        if not browser_probe.find_browser() else {'ok': True, 'reason': ''}
    ok &= _check('浏览器嗅探无浏览器时优雅降级',
                 isinstance(bres, dict) and 'ok' in bres, bres)
    return ok


def _manual_selftest() -> bool:
    """证据模式（curl + 样本 → 配方）的离线用例。"""
    from . import evidence as evd, manual
    ok = True

    # 1. curl 解析：bash 续行 + JSON 嵌套体
    curl_json = (
        "curl 'https://jc.abchina.com.cn/puc/gateway/chunk/queryInviteMessageForEie' \\\n"
        "  -H 'Accept: application/json, text/plain, */*' \\\n"
        "  -H 'Content-Type: application/json;charset=UTF-8' \\\n"
        "  -H 'Referer: https://jc.abchina.com.cn/puc/' \\\n"
        "  --data-raw '{\"pageInfo\":{\"current\":1,\"size\":10},"
        "\"messageTitle\":\"门楣\",\"publishColumn\":2}'")
    req = evd.parse_curl(curl_json)
    ok &= _check('证据模式 curl 解析 method/url/json 体',
                 req['ok'] and req['method'] == 'POST' and req['body_kind'] == 'json'
                 and req['body']['pageInfo']['current'] == 1, req.get('error'))
    kw = evd.find_keyword_slot(req['body'], req['params'], '门楣')
    ok &= _check('证据模式 关键词参数按样本值命中',
                 kw and kw['path'] == 'messageTitle' and kw['by'] == 'value', kw)
    pg = evd.find_page_slot(req['body'], req['params'])
    ok &= _check('证据模式 嵌套分页参数（pageInfo.current）',
                 pg['page_param'] == 'pageInfo.current'
                 and pg['size_param'] == 'pageInfo.size'
                 and pg['size'] == 10, pg)

    # 2. cmd 风格续行 + 表单体 + query 参数
    curl_cmd = ('curl "http://www.cfcpn.com/api/list?column=cggg&page=2" ^\n'
                ' -H "Content-Type: application/x-www-form-urlencoded" ^\n'
                ' --data-raw "noticeType=1&pageNo=2&pageSize=20&briefContent=%%E9%%97%%A8%%E5%%A4%%B4"')
    req2 = evd.parse_curl(curl_cmd)
    ok &= _check('证据模式 cmd 续行与表单体',
                 req2['ok'] and req2['body_kind'] == 'form'
                 and req2['body']['pageNo'] == '2'
                 and req2['params'].get('column') == 'cggg', req2.get('error'))
    ok &= _check('证据模式 表单体识别页码/页宽',
                 evd.find_page_slot(req2['body'], req2['params'])['page_param'] == 'pageNo',
                 evd.find_page_slot(req2['body'], req2['params']))
    ok &= _check('证据模式 参数名推断关键词位',
                 (evd.find_keyword_slot(req2['body'], req2['params'], '') or {}
                  ).get('path') == 'briefContent',
                 evd.find_keyword_slot(req2['body'], req2['params'], ''))

    # 3. 详情模板：JSON 字典体里的 id 字面值要换成 {id}
    row = {'id': '77e48c730b9f47bea89d8c7410dcf7e6', 'title': 'x'}
    dreq = {'url': 'http://a/api/detail', 'method': 'POST',
            'headers': {}, 'params': {},
            'body': {'id': '77e48c730b9f47bea89d8c7410dcf7e6', 'isDetail': '1'}}
    tpl = evd.infer_detail_templates(dreq, row)
    ok &= _check('证据模式 详情字典体参数化',
                 tpl['body'] == {'id': '{id}', 'isDetail': '1'}, tpl['body'])

    # 4. 列表相对链接需要前缀（/cms/default/webfile）
    ok &= _check('证据模式 相对链接前缀反推',
                 evd.match_url_prefix('/1ywgg3/2025/x.html',
                                      'https://cg.psbc.com/cms/default/webfile/1ywgg3/2025/x.html')
                 == '/cms/default/webfile',
                 evd.match_url_prefix('/1ywgg3/2025/x.html',
                                      'https://cg.psbc.com/cms/default/webfile/1ywgg3/2025/x.html'))

    # 5. HTML 列表反推（重复结构 + 日期跨标签）
    html = ('<html><body><ul class="newslist">'
            + ''.join('<li class="item"><h1><a href="/bidding/%d.html">门头标识灯箱制作项目 %d</a></h1>'
                      '<span>ZBB-%05d</span><span>公开招标</span>'
                      '<div>2026</div><div>11/%02d</div></li>' % (i, i, i, i)
                      for i in range(1, 7))
            + '</ul></body></html>')
    hl = evd.infer_html_list(html, 'https://x.com/list.html')
    ok &= _check('证据模式 HTML 列表行识别',
                 bool(hl) and hl['rows'] == 6
                 and hl['row_selector'] in ('li.item', 'ul.newslist > li'),
                 (hl or {}).get('row_selector'))
    ok &= _check('证据模式 HTML 列表字段（title/url/publish_at/bid_no）',
                 bool(hl) and hl['fields'].get('title', {}).get('css') in
                 ('h1::text', 'a::text', 'h1 a::text')
                 and hl['fields'].get('url', {}).get('attr') == 'href'
                 and 'publish_at' in hl['fields']
                 and hl['fields'].get('bid_no', {}).get('nth') == 0,
                 (hl or {}).get('fields'))

    # 6. HTML 详情正文容器
    dhtml = ('<html><body><div class="nav">首页 新闻</div>'
             '<div class="content"><h1>公告标题</h1><p>' + '正文内容。' * 120 +
             '</p></div><div class="footer">版权</div></body></html>')
    hd = evd.infer_html_detail(dhtml)
    ok &= _check('证据模式 HTML 详情正文容器',
                 hd and hd['content_css'] == 'div.content' and hd['content_len'] >= 200, hd)

    # 7. api_list 全链路（离线：不联网校验，只看结构与样本回测）
    list_json = json.dumps({'code': 0, 'res': {
        'total': 2,
        'rows': [{'id': 'A1', 'messageTitle': '门楣标识采购项目', 'startTime': '2026-11-01 09:00',
                  'purchaser': '某某银行', 'url': '/n/1.html'},
                 {'id': 'A2', 'messageTitle': '门楣灯箱制作项目', 'startTime': '2026-11-02 09:00',
                  'purchaser': '某某烟草', 'url': '/n/2.html'}]}}, ensure_ascii=False)
    detail_json = json.dumps({'data': {'id': 'A1', 'noticeContent': '<p>' + '公告正文。' * 80 +
                                       '</p>', 'bidsNo': 'ZB-2026-001'}}, ensure_ascii=False)
    lc = ("curl 'https://x.com/api/list' -H 'Content-Type: application/json' "
          "--data-raw '{\"pageInfo\":{\"current\":1,\"size\":20},\"messageTitle\":\"门楣\"}'")
    dc = ("curl 'https://x.com/api/detail' -H 'Content-Type: application/json' "
          "--data-raw '{\"id\":\"A1\"}'")
    out = manual.analyze({'mode': 'api_list', 'list_curl': lc, 'list_sample': list_json,
                          'detail_curl': dc, 'detail_sample': detail_json,
                          'keyword': '门楣', 'name': '测试站',
                          'with_llm': False, 'verify': False})
    rec = out.get('recipe') or {}
    d = rec.get('discovery') or {}
    ok &= _check('证据模式 api_list 生成通过', bool(out.get('ok')), out.get('error'))
    ok &= _check('证据模式 api_list items_path/total',
                 d.get('items_path') == '$.res.rows[*]'
                 and d.get('total_path') == '$.res.total', d.get('items_path'))
    ok &= _check('证据模式 api_list 关键词与嵌套分页',
                 d.get('request', {}).get('body', {}).get('messageTitle') == '{keyword}'
                 and d['pagination']['page_param'] == 'pageInfo.current',
                 d.get('request', {}).get('body'))
    ok &= _check('证据模式 api_list 详情正文与请求模板',
                 (rec.get('detail') or {}).get('content_path') == 'noticeContent'
                 and (rec.get('detail') or {}).get('request', {}).get('body') == {'id': '{id}'},
                 (rec.get('detail') or {}).get('request'))
    ok &= _check('证据模式 api_list 字段非空率',
                 out.get('verify', {}).get('field_coverage', {}).get('title') == 1.0
                 and out.get('verify', {}).get('field_coverage', {}).get('buyer') == 1.0,
                 out.get('verify', {}).get('field_coverage'))

    # 7b. 详情页「示例地址」必须参数化：否则所有条目 url 相同、详情永远抓同一条
    no_url_json = json.dumps({'value': {'total': 2, 'records': [
        {'messageId': 'M10001', 'messageTitle': '门楣标识灯箱采购项目'},
        {'messageId': 'M10002', 'messageTitle': '门楣灯箱更换项目'}]}}, ensure_ascii=False)
    example = 'https://x.com/puc/#/noticeDetail?messageId=M10001'
    ok &= _check('证据模式 示例地址转模板',
                 evd.page_url_template(example, {'messageId': 'M10001'})
                 == 'https://x.com/puc/#/noticeDetail?messageId={messageId}',
                 evd.page_url_template(example, {'messageId': 'M10001'}))
    ok &= _check('证据模式 已是模板时原样保留',
                 evd.page_url_template('https://x/#/d?id={messageId}', {})
                 == 'https://x/#/d?id={messageId}')
    out_ex = manual.analyze({'mode': 'api_list', 'list_curl': lc,
                             'list_sample': no_url_json, 'keyword': '门楣',
                             'detail_page_tpl': example,
                             'with_llm': False, 'verify': False})
    u_spec = ((out_ex.get('recipe') or {}).get('discovery') or {}).get('fields', {}).get('url')
    rows_ex = json.loads(no_url_json)['value']['records']
    urls = [recipe_mod.resolve_field(u_spec, r) for r in rows_ex]
    ok &= _check('证据模式 示例地址按行渲染出不同 url',
                 len(set(urls)) == 2 and urls[0].endswith('M10001')
                 and urls[1].endswith('M10002'), urls)
    ok &= _check('证据模式 外部 ID 取自示例占位符',
                 ((out_ex.get('recipe') or {}).get('discovery') or {}).get(
                     'fields', {}).get('external_id', {}).get('path') == 'messageId',
                 u_spec)
    # 未参数化的固定模板会被样本回测拦住
    bad_rec = json.loads(json.dumps(out_ex['recipe']))
    bad_rec['discovery']['fields']['url'] = {'template': example}
    off_bad = manual.offline_verify(bad_rec, no_url_json)
    ok &= _check('证据模式 回测拦截「url 全部相同」',
                 not off_bad['valid']
                 and any('url 完全相同' in e for e in off_bad['errors']), off_bad['errors'])

    # 7d. hash 路由 URL：id 在 # 后面，去重指纹必须保留 fragment，
    #     否则同站所有详情收敛成一个 hash → 入库只剩 1 条（农银e采实际踩坑）
    ua = 'https://jc.abchina.com.cn/puc/#/noticeDetail?messageId=d52dc682-9656-4084-8f8a-913527c91496'
    ub = 'https://jc.abchina.com.cn/puc/#/noticeDetail?messageId=0f0f7c1a-1111-4a33-a77c-60970703a961'
    ok &= _check('hash 路由 URL 指纹各不相同',
                 normalize.url_hash(ua) != normalize.url_hash(ub),
                 (normalize.canonical_url(ua), normalize.canonical_url(ub)))
    ok &= _check('纯锚点 fragment 仍然丢弃',
                 normalize.canonical_url('https://x.com/a.html#top')
                 == 'https://x.com/a.html',
                 normalize.canonical_url('https://x.com/a.html#top'))

    # 7e. 详情字典体按「键名对齐」参数化：用户复制的详情 curl 常来自另一条公告，
    #     只按值匹配会漏，导致详情永远抓同一条
    tpl_body = evd.infer_detail_templates(
        {'url': 'https://x.com/api/detail', 'method': 'POST', 'headers': {}, 'params': {},
         'body': {'messageId': 'b654beb2-7ab2-4a33-a77c-60970703a961', 'isDetail': 1}},
        {'messageId': 'd52dc682-9656-4084-8f8a-913527c91496', 'title': 'x'})
    ok &= _check('详情字典体按键名参数化（值不在样本里也能替换）',
                 tpl_body['body']['messageId'] == '{messageId}'
                 and tpl_body['body']['isDetail'] == 1, tpl_body['body'])

    # 7f. 示例地址里的页码参数（&currentPage=3）要清掉，它不是公告属性
    ok &= _check('示例地址清理页码参数',
                 evd.page_url_template(
                     'https://x.com/puc/#/noticeDetail?messageId=M1&currentPage=3',
                     {'messageId': 'M1'})
                 == 'https://x.com/puc/#/noticeDetail?messageId={messageId}',
                 evd.page_url_template(
                     'https://x.com/puc/#/noticeDetail?messageId=M1&currentPage=3',
                     {'messageId': 'M1'}))

    # 7g. 详情样本是网页时，哪怕详情 curl 指向接口，也必须 GET 页面 {url}
    dhtml_page = ('<html><body><div class="nav">首页</div><div class="article"><h1>公告</h1><p>'
                  + '正文内容。' * 150 + '</p></div></body></html>')
    out_page = manual.analyze({
        'mode': 'api_list', 'list_curl': lc, 'list_sample': no_url_json,
        'detail_curl': "curl 'https://x.com/api/chunk/detail' -H 'Content-Type: application/json' "
                       "--data-raw '{\"messageId\":\"M10001\"}'",
        'detail_sample': dhtml_page, 'keyword': '门楣',
        'detail_page_tpl': example, 'with_llm': False, 'verify': False})
    dreq2 = ((out_page.get('recipe') or {}).get('detail') or {}).get('request') or {}
    ok &= _check('证据模式 网页详情用 {url} 取页面而非接口',
                 dreq2.get('url') == '{url}' and dreq2.get('method') == 'GET'
                 and ((out_page.get('recipe') or {}).get('detail') or {}).get('content_css'),
                 dreq2)

    # 8. html_list 全链路
    lh = ('curl "https://x.com/list.html?page=1" -H "User-Agent: Mozilla/5.0"')
    out2 = manual.analyze({'mode': 'html_list', 'list_curl': lh, 'list_sample': html,
                           'detail_sample': dhtml, 'keyword': '门头',
                           'with_llm': False, 'verify': False})
    rec2 = out2.get('recipe') or {}
    ok &= _check('证据模式 html_list 生成通过', bool(out2.get('ok')), out2.get('error'))
    ok &= _check('证据模式 html_list 选择器与翻页',
                 (rec2.get('discovery') or {}).get('row_selector') in
                 ('li.item', 'ul.newslist > li')
                 and (rec2.get('discovery') or {}).get('pagination', {}).get('page_param') == 'page',
                 (rec2.get('discovery') or {}).get('pagination'))
    ok &= _check('证据模式 html_list 详情正文容器',
                 (rec2.get('detail') or {}).get('content_css') == 'div.content',
                 (rec2.get('detail') or {}).get('content_css'))

    # 9. 样本回测能发现坏选择器
    bad = json.loads(json.dumps(rec2))
    bad['discovery']['row_selector'] = 'ul.not-exist > li'
    off = manual.offline_verify(bad, html, dhtml)
    ok &= _check('证据模式 样本回测拦截无效选择器',
                 not off['valid'] and off['items'] == 0, off)

    # 10. HTML 模式的详情页链接模板：用户常写 https://host{href}
    ok &= _check('HTML 模式 {href} 模板转 {_href}',
                 evd.html_url_template('https://x.com{href}', hl) == 'https://x.com{_href}',
                 evd.html_url_template('https://x.com{href}', hl))
    ok &= _check('HTML 模式 真实详情地址转模板（同目录）',
                 evd.html_url_template('https://x.com/bidding/5.html', hl)
                 == 'https://x.com{_href}',
                 evd.html_url_template('https://x.com/bidding/5.html', hl))
    out3 = manual.analyze({'mode': 'html_list', 'list_curl': lh, 'list_sample': html,
                           'detail_sample': dhtml, 'keyword': '门头',
                           'detail_page_tpl': 'https://x.com{href}',
                           'with_llm': False, 'verify': False})
    u3 = ((out3.get('recipe') or {}).get('discovery') or {}).get('fields', {}).get('url')
    ok &= _check('HTML 模式 用户模板写进配方并可按行渲染',
                 (u3 or {}).get('template') == 'https://x.com{_href}'
                 and str(recipe_mod.resolve_field(
                     u3, {'_text': '', '_href': '/bidding/1.html'})).endswith('/bidding/1.html'),
                 u3)

    # 11. 详情链接选「每条不同、像详情页」的锚点，而不是行内第一个 <a>
    tricky = ('<html><body><ul class="searchList">'
              + ''.join('<li><a href="/zbcg/index.jhtml">分类</a>'
                        '<a href="/zgczb/%d.jhtml">门头项目 %d</a></li>' % (900 + i, i)
                        for i in range(5))
              + '</ul></body></html>')
    hk = evd.infer_html_list(tricky, 'https://x.com/search.jspx')
    ok &= _check('HTML 模式 跳过栏目链接选中详情链接',
                 bool(hk) and str(hk['fields'].get('url')).find('nth') >= 0
                 and hk['fields']['url'].get('nth') == 1,
                 hk and hk['fields'].get('url'))
    return ok


def _path_page_selftest(base: str) -> bool:
    """页码在路径里（search.jspx → search_2.jspx）时的识别与翻页。"""
    from . import evidence as evd
    from .transport import Fetcher

    ok = True
    listing = ('<html><body><ul class="searchList">'
               '<li><a href="/zgczb/897997.jhtml">门头项目</a></li>'
               '</ul><a href="search_2.jspx?q=x">下一页</a>'
               '<div>共1118条记录 1/125页</div></body></html>')
    hint = evd.infer_path_pagination(listing, base + '/search.jspx')
    ok &= _check('路径分页识别 search.jspx → search_{page}.jspx',
                 bool(hint) and hint['url_template'] == base + '/search_{page}.jspx'
                 and hint['first_url'] == base + '/search.jspx', hint)

    raw = {
        'meta': {'id': 'pathpage', 'name': '路径分页', 'host': '127.0.0.1',
                 'base_url': base},
        'transport': {'kind': 'http', 'timeout_s': 10, 'trust_env': False,
                      'rate_limit': {'qps': 0}},
        'discovery': {
            'mode': 'html_list',
            'request': {'url': base + '/html/list_{page}.jspx', 'method': 'GET',
                        'params': {'q': '{keyword}'}},
            'pagination': {'type': 'page', 'page_in_url': True, 'page_param': '',
                           'start': 1, 'max_pages': 3,
                           'first_url': base + '/html/list',
                           'url_template': base + '/html/list_{page}.jspx',
                           'stop_when': ['empty_items', 'page_unchanged']},
            'row_selector': '#list > li.row',
            'fields': {'title': {'css': 'a::text', 'strip': True},
                       'url': {'css': 'a', 'attr': 'href'}},
        },
        'normalize': {'filters': {'inherit': 'global'}},
    }
    rec = recipe_mod.compile_recipe(raw)
    ok &= _check('路径分页配方结构合法', not recipe_mod.validate_recipe(raw),
                 recipe_mod.validate_recipe(raw))
    with Fetcher(rec) as f:
        pages = list(paginate.iter_pages(rec, f, '门头', max_pages=3))
    first = [it['title'] for it in pages[0].items] if pages else []
    second = [it['title'] for it in pages[1].items] if len(pages) > 1 else []
    ok &= _check('路径分页第 1 页取到 5 条', len(first) == 5, len(first))
    ok &= _check('路径分页第 2 页与第 1 页不同',
                 bool(second) and second[0] != first[0], (first[:1], second[:1]))
    ok &= _check('路径分页至少翻到 2 页', len(pages) >= 2, len(pages))
    return ok


def _waf_selftest(base: str) -> bool:
    """详情页被反爬中间页（acw_sc__v2）拦下时的识别与降级。"""
    from .transport import Fetcher, _is_waf_text

    ok = True
    challenge = ("<html><script>var arg1='30B869B3DAED0C5F9EAEB03B6621FED4D2A43728';"
                 "function reload(x){document.cookie='acw_sc__v2='+x;}</script></html>")
    ok &= _check('反爬中间页可识别',
                 _is_waf_text(challenge)
                 and not _is_waf_text('<html><div id="content">公告正文</div></html>')
                 and not _is_waf_text('{"data":{"content":"正文"}}'),
                 'arg1 挑战页判定')
    raw = {
        'meta': {'id': 'waf', 'name': 'WAF 夹具', 'host': '127.0.0.1', 'base_url': base},
        'transport': {'kind': 'http', 'timeout_s': 8, 'trust_env': False,
                      'rate_limit': {'qps': 0}, 'retry': {'times': 0}},
        'discovery': {
            'mode': 'html_list',
            'request': {'url': base + '/html/list', 'method': 'GET'},
            'pagination': {'type': 'none', 'page_param': '', 'start': 1},
            'row_selector': '#list > li.row',
            'fields': {'title': {'css': 'a::text', 'strip': True},
                       'url': {'css': 'a', 'attr': 'href'}},
        },
        'detail': {'enabled': True, 'id_field': 'url',
                   'request': {'url': '{url}', 'method': 'GET'},
                   'content_css': '#content', 'content_encoding': 'html',
                   'extract': {'scope': 'html',
                               'fields': {'title': {'css': 'h1::text', 'strip': True}}}},
        'normalize': {'filters': {'inherit': 'global'}},
    }
    rec = recipe_mod.compile_recipe(raw)
    old = os.environ.get('BIDCRAWL_NO_BROWSER')
    os.environ['BIDCRAWL_NO_BROWSER'] = '1'
    try:
        with Fetcher(rec) as f:
            res = f.fetch_detail(base + '/waf/detail')
            ok &= _check('无浏览器时过盾失败不抛异常', res is not None,
                         getattr(res, 'status', None))
            ok &= _check('过盾失败给出可操作原因', bool(f.waf_reason), f.waf_reason)
            # 瑞数 412：状态码不是 4xx 里的普通错误，也要走挑战识别
            ok &= _check('瑞数 $_ts 挑战页可识别',
                         _is_waf_text("$_ts=window['$_ts'];$_ts.cd='abc';"))
            res2 = f.fetch_detail(base + '/waf/rishu')
            ok &= _check('412 + 瑞数页面也走反爬分支（不抛异常）',
                         res2 is not None and res2.status == 412,
                         getattr(res2, 'status', None))
            # 同实例第二次不再重复启动浏览器（本次已标记不可用）
            before = f._waf_boots
            f.fetch_detail(base + '/waf/detail')
            ok &= _check('过盾失败后不再重复尝试', f._waf_boots == before,
                         (before, f._waf_boots))
    finally:
        if old is None:
            os.environ.pop('BIDCRAWL_NO_BROWSER', None)
        else:
            os.environ['BIDCRAWL_NO_BROWSER'] = old
    return ok


def _list_shape_selftest() -> bool:
    """同页多份同构容器、绝对链接、双域名：列表反推与 URL 模板都要稳。"""
    from . import evidence as evd
    from .transport import _is_waf_text, _repair_url

    ok = True
    nav = ''.join('<li><a href="https://%szb.example.com">%s</a></li>' % (x, x)
                  for x in ('zb', 'sh', 'zj', 'fj', 'jx'))
    notices = ''.join(
        '<li><a href="/zbgg/%d.jhtml">某单位物资采购项目公告 %d</a>'
        '<p>发布时间：2026-11-0%d 10:00</p></li>' % (i, i, i)
        for i in range(1, 6))
    html = ('<html><body><ul>%s</ul>'
            '<div class="List2 Top5"><ul>%s</ul></div>'
            '</body></html>' % (nav, notices))
    info = evd.infer_html_list(html, 'https://hnzb.example.com/search.jspx')
    ok &= _check('同页多个同构 ul 时不被导航抢走',
                 bool(info) and info['rows'] == 5
                 and 'List2' in (info['row_selector'] or ''),
                 (info or {}).get('row_selector'))
    ok &= _check('选中公告行后日期能抽到',
                 bool(info) and all(r.get('publish_at') for r in (info['sample'] or [])),
                 (info or {}).get('sample'))
    ok &= _check('跨域名导航行被扣分（不选它）',
                 bool(info) and 'example.com' not in str(info.get('sample')))

    # 绝对链接 + origin 前缀模板：必须退化成 {_href}，否则拼出双域名
    li = {'sample': [{'url': 'https://cdn.example.com/a/1.html'},
                     {'url': 'https://cdn.example.com/a/2.html'}],
          'first_url': 'https://cdn.example.com/a/1.html'}
    ok &= _check('绝对链接时模板不再叠加域名',
                 evd.html_url_template('https://hnzb.example.com{_href}', li) == '{_href}',
                 evd.html_url_template('https://hnzb.example.com{_href}', li))
    li2 = {'sample': [{'url': '//cdn.example.com/a/1.html'}]}
    ok &= _check('协议相对链接沿用站点 scheme',
                 evd.html_url_template('https://hnzb.example.com{_href}', li2) == 'https:{_href}',
                 evd.html_url_template('https://hnzb.example.com{_href}', li2))
    li3 = {'sample': [{'url': '/zbgg/1.jhtml'}]}
    ok &= _check('站内相对链接保留 origin 前缀',
                 evd.html_url_template('https://h.example.com{_href}', li3)
                 == 'https://h.example.com{_href}',
                 evd.html_url_template('https://h.example.com{_href}', li3))

    ok &= _check('双域名 URL 会被修回单域名',
                 _repair_url('https://hnzb.example.comhttps://zb.example.com')
                 == 'https://zb.example.com',
                 _repair_url('https://hnzb.example.comhttps://zb.example.com'))
    ok &= _check('查询串里的 https:// 不被误改',
                 _repair_url('https://a.com/p?u=https://b.com')
                 == 'https://a.com/p?u=https://b.com')
    ok &= _check('瑞数页面识别',
                 _is_waf_text("$_ts=window['$_ts'];$_ts.nsd=1;$_ts.cd='x';")
                 and not _is_waf_text('<html><body><div class="Content">公告</div></body></html>'))

    # 只有 2 行：必须给出"为什么"（不然用户只会看到一句「识别不出」）
    d2: dict = {}
    two = ('<html><body><ul class="list">' + ''.join(
        '<li><a href="/n/%d.html">某单位采购公告 %d</a></li>' % (i, i) for i in (1, 2))
        + '</ul></body></html>')
    got_two = evd.infer_html_list(two, 'https://x.com', diag=d2)
    ok &= _check('样本不足 3 行时给出诊断原因',
                 got_two is None and bool(d2.get('reason')), d2)

    # 多一条無效行：宽松选择器救回，但空行不能被算成公告
    extra = ('<html><body><ul class="list">' + ''.join(
        '<li><a href="/n/%d.html">某单位物资采购项目公告 %d</a></li>' % (i, i)
        for i in range(1, 6)) + '<li class="hidden"><a href="#">x</a></li>'
        '</ul></body></html>')
    d3: dict = {}
    got3 = evd.infer_html_list(extra, 'https://x.com', diag=d3)
    ok &= _check('多一条隐藏行时用宽松匹配救回',
                 bool(got3) and got3['rows'] == 5, d3)
    return ok


def _html_list_robust_selftest() -> bool:
    """列表反推的健壮性：行本身是 <a>、工具类名、畸形 HTML 都不能炸。"""
    from parsel import Selector

    from . import evidence as evd

    ok = True

    # 1) 行元素本身是 <a>（<div><a class="item">…</a>×5）
    a_rows = ('<html><body><div class="list">' + ''.join(
        '<a class="item" href="/notice/%d.html">'
        '<h3 class="tt">某单位标识标牌采购项目公告 %d</h3>'
        '<span class="dt">2026-11-%02d</span></a>' % (i, i, i)
        for i in range(1, 6)) + '</div></body></html>')
    got = evd.infer_html_list(a_rows, 'https://x.com/list')
    ok &= _check('行本身是 <a> 的列表能识别',
                 bool(got) and got['rows'] == 5
                 and got['fields']['url'].get('attr') == 'href', (got or {}).get('row_selector'))
    ok &= _check('行本身是 <a> 时标题取到文本',
                 bool(got) and got['fields']['title'].get('css') == 'h3.tt::text',
                 (got or {}).get('fields', {}).get('title'))

    # 2) 工具类名（Tailwind：sm:flex / w-1/2 / md:block）不能拼成非法选择器
    tw = ('<html><body><div class="space-y-4 sm:p-6">' + ''.join(
        '<a class="group sm:flex w-1/2 md:block" href="/n/%d.html">'
        '<h3 class="text-lg">某单位宣传服务采购项目 %d</h3>'
        '<span class="text-sm">2026-11-%02d</span></a>' % (i, i, i)
        for i in range(1, 6)) + '</div></body></html>')
    try:
        got2 = evd.infer_html_list(tw, 'https://x.com/list')
        raised = None
    except Exception as e:  # noqa: BLE001
        got2, raised = None, e
    ok &= _check('工具类名不产生非法 CSS 选择器（不抛异常）',
                 raised is None, raised)
    ok &= _check('工具类名列表仍能选中行',
                 bool(got2) and got2['rows'] == 5, (got2 or {}).get('row_selector'))
    ok &= _check('工具类名列表的选择器可回测命中',
                 bool(got2) and len(Selector(text=tw).css(got2['row_selector'])) == 5,
                 (got2 or {}).get('row_selector'))

    # 3) 选不中的选择器必须返回空串，而不是「看起来能用」
    sel = Selector(text=a_rows)
    nodes = [n.root for n in sel.css('a.item')]
    bogus = evd.selector_for_nodes(sel, nodes[:2] + [sel.css('div')[0].root])
    ok &= _check('无法回测的选择器返回空串', bogus == '', bogus)

    # 4) 畸形/截断 HTML：只能返回 None 或可用结果，不能抛
    broken = ('<html><body><ul class=list>' + ''.join(
        "<li class='row'><a href='/n/%d.html'>公告 %d 号标题</a>" % (i, i)
        for i in range(1, 6)))
    try:
        got3 = evd.infer_html_list(broken, 'https://x.com/list')
        bad = None
    except Exception as e:  # noqa: BLE001
        got3, bad = None, e
    ok &= _check('畸形 HTML 不抛异常', bad is None, bad)
    ok &= _check('畸形 HTML 仍能抽出 5 行',
                 bool(got3) and got3['rows'] == 5, (got3 or {}).get('row_selector'))
    return ok


def _pdf_source_selftest() -> bool:
    """PDF 正文源：不能把页脚《平台注册规则.pdf》当成公告附件。"""
    from .ai_analyze import _pdf_looks_like_notice
    from .probe import find_pdf_source

    ok = True
    footer = ('<a href="/r/cms/images/rules.pdf">平台注册规则</a>'
              '<a href="/r/cms/images/guide.pdf">供应商入驻指南</a>')
    list_html = '<html><body><div class="footer">%s</div></body></html>' % footer
    detail = ('<html><body><div id="content"><h2>某单位宣传服务采购公告</h2>'
              '<p>采购范围：宣传片拍摄制作，预算 120 万元。</p></div>'
              '<div class="footer">%s</div></body></html>' % footer)
    got = find_pdf_source(detail, 'https://x.com/a/1.jhtml',
                          list_html=list_html, content_css='#content')
    ok &= _check('页脚 PDF（列表页同款）不被当成正文',
                 got is None, (got or {}).get('pdf_url'))

    attach = ('<html><body><div id="content"><h2>招标公告</h2>'
              '<p>详见附件。</p><a href="/upload/zb/2026/123.pdf">招标文件.pdf</a>'
              '</div><div class="footer">%s</div></body></html>' % footer)
    got2 = find_pdf_source(attach, 'https://x.com/a/1.jhtml',
                           list_html=list_html, content_css='#content')
    ok &= _check('正文容器内的附件 PDF 能被选中',
                 bool(got2) and str(got2['pdf_url']).endswith('/upload/zb/2026/123.pdf'),
                 (got2 or {}).get('pdf_url'))
    ok &= _check('附件 PDF 不标记为 iframe 查看器',
                 bool(got2) and not got2.get('from_iframe'), got2)

    viewer = ('<html><body><iframe id="pdfContainer" '
              'src="https://x.com/pdfjs/web/viewer.html?file=https%3A%2F%2Fx.com%2Fa.pdf">'
              '</iframe></body></html>')
    got3 = find_pdf_source(viewer, 'https://x.com/a/2.jhtml')
    ok &= _check('pdfjs 查看器仍能还原 PDF 直链',
                 bool(got3) and got3['pdf_url'] == 'https://x.com/a.pdf'
                 and got3.get('from_iframe'), (got3 or {}).get('pdf_url'))

    ok &= _check('PDF 正文语义校验：页脚文档被识破',
                 not _pdf_looks_like_notice('中烟电子采购平台采购代理机构入驻规则。'
                                            '本规则适用于代理机构注册、账号管理。',
                                            '某单位宣传服务采购公告')[0])
    ok &= _check('PDF 正文语义校验：真公告通过',
                 _pdf_looks_like_notice('某单位宣传服务采购公告。招标范围包括宣传片拍摄与'
                                        '制作、活动策划执行、媒体投放等内容，服务期一年。'
                                        '投标截止时间 2026-12-01 09:30，欢迎符合资格条件的'
                                        '供应商参加投标。采购人：某单位；代理机构：某招标公司。',
                                        '某单位宣传服务采购公告')[0])
    return ok


def _relative_detail_selftest(base: str) -> bool:
    """列表给的是站内相对路径时，详情请求必须自动补成绝对地址（否则永远抓不到）。"""
    from .transport import Fetcher

    raw = {
        'meta': {'id': 'rel', 'name': '相对链接夹具', 'host': '127.0.0.1',
                 'base_url': base},
        'transport': {'kind': 'http', 'timeout_s': 10, 'trust_env': False,
                      'rate_limit': {'qps': 0}},
        'discovery': {
            'mode': 'html_list',
            'request': {'url': base + '/html/list', 'method': 'GET',
                        'params': {'page': '{page}'}},
            'pagination': {'type': 'none', 'page_param': '', 'start': 1},
            'row_selector': '#list > li.row',
            'fields': {'title': {'css': 'a::text', 'strip': True},
                       'url': {'css': 'a', 'attr': 'href'}},
        },
        'detail': {'enabled': True,
                   'request': {'url': '{url}', 'method': 'GET'},
                   'content_css': '.content',
                   'extract': {'scope': 'html', 'fields': {
                       'scope': {'css': '.content', 'strip': True}}}},
        'normalize': {'filters': {'inherit': 'global'}},
    }
    rec = recipe_mod.compile_recipe(raw)
    ok = True
    with Fetcher(rec) as f:
        page = paginate.fetch_first(rec, f, '门头')
        ok &= _check('相对链接 HTML 列表可抽取', len(page.items) >= 3, len(page.items))
        item = page.items[0]
        ok &= _check('相对链接原样保留（交由 normalize 补全）',
                     str(item.get('url') or '').startswith('/notice/'), item.get('url'))
        ok &= _check('Fetcher.abs_url 用 meta.base_url 补全',
                     f.abs_url(item['url']) == base + item['url'], f.abs_url(item['url']))
        seen = []

        def _spy(spec, ctx):
            seen.append(spec.get('url'))
            return orig(spec, ctx)
        orig = f.request
        f.request = _spy
        res = f.fetch_detail(item['url'])
        f.request = orig
        ok &= _check('详情请求地址被补成绝对地址',
                     str(res.url or '').startswith('http://'), res.url)
        ok &= _check('相对链接详情能取到正文',
                     res.ok and '门头灯箱' in (res.text or ''),
                     (res.status, (res.error or '')[:80]))
    return ok


def main() -> int:
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % srv.server_address[1]
    ok = True
    try:
        # 1. 真实配方文件可校验
        cfcpn = json.loads((Path(__file__).with_name('recipes') / 'cfcpn.json')
                           .read_text(encoding='utf-8'))
        ok &= _check('cfcpn 配方校验', not recipe_mod.validate_recipe(cfcpn),
                     recipe_mod.validate_recipe(cfcpn))
        rec = recipe_mod.compile_recipe(_fixture_recipe(base))

        # 2. 页宽探测：请求 100，服务端截断到 50，且 total=120
        with Fetcher(rec) as f:
            probe = paginate.probe_size(rec, f, '门头')
        ok &= _check('页宽探测 size_max=50', probe['size_max'] == HARD_CAP, probe)
        ok &= _check('页宽探测 behavior=truncate', probe['behavior'] == 'truncate', probe)

        # 3. 用最大页宽翻页：120 条 / 50 = 3 页
        paginate.apply_probe(rec, probe)
        pages, titles = 0, []
        with Fetcher(rec) as f:
            for pg in paginate.iter_pages(rec, f, '门头'):
                pages += 1
                titles.append(pg.items[0]['title'])
                if pages == 1:
                    ok &= _check('首页条数=50', len(pg.items) == 50, len(pg.items))
                    ok &= _check('total=120', pg.total == TOTAL, pg.total)
                ok &= _check('第 %d 页非空' % pg.page, bool(pg.items))
            reqs = f.requests
        ok &= _check('翻页数=3', pages == 3, pages)
        ok &= _check('请求数=3（大页宽生效）', reqs == 3, reqs)
        ok &= _check('第 1/2 页内容不同', len(titles) >= 2 and titles[0] != titles[1])

        # 3.5 字段映射按样本值复核（接口型站点最常见的一类错映射）
        from . import ai_analyze, evidence as evd, probe
        rows = [
            {'pkNotice': 'NOTI1', 'createTime': None, 'areaName': None,
             'ancmHdlnCntnt': '中国银行股份有限公司2027-2028年信息科技运营中心硬件维保服务招标公告',
             'ancmAncDt': '2026-11-09 09:57:18', 'orgType': '1'},
            {'pkNotice': 'NOTI2', 'createTime': None, 'areaName': None,
             'ancmHdlnCntnt': '2026-2028年信息科技运营中心硬件维保服务（博科网络设备）第三方维保项目结果公告',
             'ancmAncDt': '2026-10-22 15:28:38', 'orgType': '1'},
        ]
        keys = list(rows[0].keys())
        fixed = evd.refine_field_map(
            {'title': 'areaName', 'region': 'areaName', 'publish_at': 'createTime',
             'external_id': 'pkNotice'}, keys, rows)
        ok &= _check('字段复核：title 不再指向地区列',
                     fixed.get('title') == 'ancmHdlnCntnt', fixed)
        ok &= _check('字段复核：发布时间换成真正有值的日期列',
                     fixed.get('publish_at') == 'ancmAncDt', fixed)
        ok &= _check('字段复核：整列为空的地区被丢弃', 'region' not in fixed, fixed)
        mm = ai_analyze._merge_map({'region': 'areaName'}, {'region'},
                                   {'title': 'areaName'}, keys)
        ok &= _check('字段映射：title 不抢 region 已占用的列',
                     mm.get('title') != 'areaName', mm)
        ok &= _check('字段映射：areaName 不会被当成标题',
                     not ai_analyze._guard_ok('title', 'areaName'))
        ok &= _check('字段映射：noticeTitle 仍可作标题',
                     ai_analyze._guard_ok('title', 'noticeTitle'))

        # 3.6 关键词槽位候选（分片键 / 同名过滤字段）
        kw_body = {'reqHeader': {'shardingKey': '门头', 'shardingType': '09'},
                   'reqBody': {'ancmHdlnCntnt': '硬件', 'orgType': '1',
                               'createTime': '', 'pageNo': 1}}
        cands = evd.find_keyword_candidates(kw_body, {}, '门头', row_keys=keys)
        leaves = [c['leaf'] for c in cands]
        ok &= _check('关键词候选：收「请求体与结果行同名且有值」的字段',
                     'ancmHdlnCntnt' in leaves, leaves)
        ok &= _check('关键词候选：不收空值 / 配置类字段',
                     'orgType' not in leaves and 'createTime' not in leaves, leaves)
        ok &= _check('关键词候选：只有值命中时才收 shardingKey',
                     all(c['by'] == 'value' for c in cands if c['leaf'] == 'shardingKey'),
                     leaves)
        tp = probe._find_total({'respBody': {'totalRecord': 1834, 'data': []}})
        ok &= _check('totalRecord 被识别为总数', tp == '$.respBody.totalRecord', tp)

        # 3.7 任务参数清洗：全部平台任务不能残留单平台参数
        from . import jobs as bjobs
        ok &= _check('任务参数：全平台任务清掉残留 platform',
                     'platform' not in bjobs.normalize_params(
                         None, {'platform': '365trade', 'dry_run': True}),
                     bjobs.normalize_params(None, {'platform': '365trade'}))
        ok &= _check('任务参数：全平台任务保留关键词',
                     bjobs.normalize_params(None, {'platform': 'x', 'keywords': ['门头']})
                     .get('keywords') == ['门头'])
        ok &= _check('任务参数：指定平台时以 platform_id 为准',
                     bjobs.normalize_params('cfcpn', {'platform': 'ctpch',
                                                      'platforms': ['ctpch']})
                     == {'platform': 'cfcpn'},
                     bjobs.normalize_params('cfcpn', {'platform': 'ctpch',
                                                      'platforms': ['ctpch']}))

        # 3.8 字段正则：别名拼成完整正则
        from . import regexcfg, detail_ai
        _f = [{'field_key': 'budget',
               'pattern_tpl': r'(?:{alts})[：:\s]*([^\n，。；]{1,40})'}]
        _a = [{'field_key': 'budget', 'alias': '预算', 'scope': 'global'},
              {'field_key': 'budget', 'alias': '项目规模', 'scope': 'global'},
              {'field_key': 'budget', 'alias': '控制价', 'scope': 'site_a'}]
        _r, _e = regexcfg.build_rules(_f, _a, '')
        ok &= _check('正则拼装：别名合成完整正则',
                     not _e and _r.get('budget') ==
                     r'(?:项目规模|预算)[：:\s]*([^\n，。；]{1,40})', _r.get('budget'))
        _r2, _ = regexcfg.build_rules(_f, _a, 'site_a')
        ok &= _check('正则拼装：平台级别名只对该平台生效',
                     '控制价' in _r2.get('budget', '')
                     and '控制价' not in _r.get('budget', ''), _r2.get('budget'))
        _r3, _ = regexcfg.build_rules(
            [{'field_key': 'x', 'pattern_tpl': '(?:{alts})'}],
            [{'field_key': 'x', 'alias': '截止时间', 'scope': 'global'},
             {'field_key': 'x', 'alias': '投标截止时间', 'scope': 'global'}], '')
        ok &= _check('正则拼装：长词优先（避免短词先吃掉）',
                     _r3.get('x') == '(?:投标截止时间|截止时间)', _r3.get('x'))
        _r4, _ = regexcfg.build_rules(
            [{'field_key': 'x', 'pattern_tpl': '(?:{alts})'}],
            [{'field_key': 'x', 'alias': 'A(B', 'scope': 'global'}], '')
        ok &= _check('正则拼装：别名里的正则符号被转义',
                     _r4.get('x') == r'(?:A\(B)', _r4.get('x'))
        _r5, _ = regexcfg.build_rules(
            [{'field_key': 'fee', 'pattern_tpl': '(?:{alts})x'}], [], '')
        ok &= _check('正则拼装：没有匹配词的字段不生成', 'fee' not in _r5, _r5)
        _r6, _e6 = regexcfg.build_rules(
            [{'field_key': 'a', 'pattern_tpl': '(?:{alts}['},
             {'field_key': 'b', 'pattern_tpl': '(?:{alts})x'}],
            [{'field_key': 'a', 'alias': 'x', 'scope': 'global'},
             {'field_key': 'b', 'alias': 'y', 'scope': 'global'}], '')
        ok &= _check('正则拼装：坏模板只淘汰自己',
                     'a' not in _r6 and 'b' in _r6 and 'a' in _e6, _e6)
        # 等价性：库里的种子（由原内置正则拆出）对同一段文本的结果必须一致
        _sample = ('预算金额：人民币 128.5 万元\n招标编号：0730-2613JS0099-01\n'
                   '获取时间：从 2026年11月18日 09:00 到 2026年11月25日 17:00\n'
                   '投标文件递交截止时间：2026年12月05日 09:30\n标书费：500元\n'
                   '联系人：张工\n工期：60日历天\n中标人数量：1名\n'
                   '中招国际招标有限公司（招标代理机构）')
        _db_rules = regexcfg.load_rules('', force=True)

        def _pick(pat, _t=_sample):
            m = re.search(pat, _t, re.M)
            if not m:
                return ''
            return (m.group(1) if m.lastindex else m.group(0)).strip()

        _diffs = []
        for _k, _pat in detail_ai.DEFAULT_FALLBACK_REGEX.items():
            _new = _db_rules.get(_k)
            if _new and _pick(_pat) != _pick(_new):
                _diffs.append((_k, _pick(_pat), _pick(_new)))
        ok &= _check('正则拼装：与内置默认抽取结果一致', not _diffs, _diffs)

        # 4. 归一化
        dt = normalize.parse_datetime('2026年11月13日 09:30')
        ok &= _check('中文日期解析', bool(dt) and dt.year == 2026, dt)
        ok &= _check('金额万元转元', str(normalize.parse_amount('123.45万元')) == '1234500.00',
                     normalize.parse_amount('123.45万元'))
        ok &= _check('状态 expired',
                     normalize.compute_status(datetime.now() - timedelta(days=1)) == 'expired')
        a = normalize.canonical_url('https://A.com/x/?b=2&a=1&utm_source=z#frag')
        b = normalize.canonical_url('https://a.com/x?a=1&b=2')
        ok &= _check('URL 规范化去跟踪参数', a == b, (a, b))

        # 5. 列表 + 详情 + 过滤
        snap = config.Snapshot(
            platform_id='fixture',
            keywords=[{'id': 1, 'word': '门头', 'scope': 'global', 'sort_no': 0}],
            buyers=[{'id': 1, 'pattern': '烟草', 'match_type': 'contains',
                     'scope': 'global', 'sort_no': 0}],
            rules=[{'id': 1, 'name': '结果类', 'rule_type': 'title_regex',
                    'value': {'regex': '中标结果|成交公告'}, 'scope': 'global', 'sort_no': 0}],
            version=1)
        engine = bfilter.RuleEngine(snap, 'fixture')
        kept = dropped = 0
        with Fetcher(rec) as f:
            first = paginate.fetch_first(rec, f, '门头')
            from .extract import extract_detail
            item = first.items[0]
            detail_res = f.fetch_detail(item['url'])
            extra = extract_detail(rec, detail_res, item)
            item.update(extra)
            norm = normalize.normalize_item(item, source_default='夹具')
            norm['platform_id'] = 'fixture'
            ok &= _check('详情预算抽取', str(norm['budget_yuan']) == '1234500.00',
                         norm['budget_yuan'])
            ok &= _check('详情开标时间抽取', norm['open_at'] is not None, norm['open_at'])
            for it in first.items:
                d = engine.evaluate(normalize.normalize_item(it))
                kept += 1 if d.keep else 0
                dropped += 0 if d.keep else 1
        ok &= _check('白名单过滤生效', kept > 0 and dropped > 0, (kept, dropped))

        # 6. HTML 列表抽取
        html_recipe = _fixture_recipe(base)
        html_recipe['discovery'] = {
            'mode': 'html_list', 'row_selector': 'ul#list li.row',
            'request': {'url': base + '/html/list', 'method': 'GET',
                        'params': {'keyword': '{keyword}', 'page': '{page}'}},
            'pagination': {'type': 'page', 'page_param': 'page', 'start': 1,
                           'max_pages': 1, 'stop_when': ['empty_items']},
            'fields': {
                'title': {'css': 'a'},
                'url': {'css': 'a', 'attr': 'href'},
                'buyer': {'css': '.buyer'},
                'publish_at': {'css': '.date'},
            }}
        hrec = recipe_mod.compile_recipe(html_recipe)
        with Fetcher(hrec) as f:
            hpage = paginate.fetch_first(hrec, f, '门头')
        ok &= _check('HTML 列表抽取 5 条', len(hpage.items) == 5, len(hpage.items))
        ok &= _check('HTML 相对链接', hpage.items[0]['url'].endswith('/notice/11'),
                     hpage.items[0]['url'])

        # 6b. 详情 AI 解析（纯离线：正文清洗 / 中文日期 / 降级抽取 / URL 保真）
        from . import detail_ai
        sample = ('&lt;p&gt;4、合同估算价：约10万元整（含税）&lt;/p&gt;'
                  '&lt;p&gt;1、获取时间：从2026年11月14日09时00分到2026年11月20日17时00分&lt;/p&gt;'
                  '&lt;p&gt;3、费用：每份300元人民币，售后不退。&lt;/p&gt;'
                  '&lt;p&gt;开标时间：2026年12月04日下午14:00&lt;/p&gt;')
        plain = detail_ai.html_to_text(sample)
        ok &= _check('双层转义 HTML 清洗',
                     '合同估算价：约10万元整' in plain and '<p>' not in plain, plain[:120])
        ok &= _check('中文时分日期转 ISO',
                     detail_ai.cn_dt_to_iso('2026年11月14日09时00分') == '2026-11-14 09:00',
                     detail_ai.cn_dt_to_iso('2026年11月14日09时00分'))
        ok &= _check('下午时刻按 24 小时制',
                     detail_ai.cn_dt_to_iso('2026年12月04日下午2:00').endswith('14:00'),
                     detail_ai.cn_dt_to_iso('2026年12月04日下午2:00'))
        fb = detail_ai.regex_fallback(plain, {})
        ok &= _check('降级正则抽到预算', '10万' in str(fb.get('budget') or ''), fb)
        ok &= _check('降级正则抽到报名费', '300' in str(fb.get('fee_note') or ''), fb)
        ok &= _check('缓存 key 稳定且随正文变化',
                     detail_ai._cache_key('p', 'v1', plain) ==
                     detail_ai._cache_key('p', 'v1', plain) and
                     detail_ai._cache_key('p', 'v1', plain) !=
                     detail_ai._cache_key('p', 'v1', plain + 'x'))
        cf_keep = json.loads((Path(__file__).with_name('recipes') / 'cfcpn.json')
                             .read_text(encoding='utf-8'))
        ok &= _check('cfcpn 声明详情 POST / AI / URL 模板',
                     cf_keep['detail']['request']['body'] == {'id': '{id}', 'isDetail': '1'}
                     and cf_keep['detail']['ai']['enabled']
                     and cf_keep['normalize']['keep_url'] is True
                     and 'searchVal={id}' in cf_keep['discovery']['fields']['url']['template'])
        keep_url_item = normalize.normalize_item(
            {'title': 'T', 'url': 'http://www.cfcpn.com/jcw/sys/index/goUrl?'
                                  'url=modules/sys/login/detail&column=undefined&searchVal=abc'},
            keep_url=True)
        ok &= _check('keep_url 保留原始拼接 URL',
                     keep_url_item['url'].endswith('searchVal=abc') and
                     '%2F' not in keep_url_item['url'], keep_url_item['url'])
        ok &= _check('keep_url 指纹仍走规范化',
                     keep_url_item['url_hash'] == normalize.url_hash(keep_url_item['url']))

        # 6c. AI 配方生成器（纯离线：不做外网探测、不调 LLM）
        ok &= _generator_selftest(base)
        # 6d. HTML 列表 + goSearch/page + PDF 正文（cg.11185.cn 形态）
        ok &= _html_pdf_selftest()

        # 6e. 嵌套 dto / 列表页隐藏域 / HTML 正文（cg.psbc.com 形态）
        ok &= _nested_api_selftest()

        # 6g. 证据模式：curl + 返回样本 → 配方（用户手工取证）
        ok &= _manual_selftest()

        # 6h. 列表给相对链接时，详情请求要自动补成绝对地址
        ok &= _relative_detail_selftest(base)

        # 6i. 页码在路径里的分页（search_2.jspx）
        ok &= _path_page_selftest(base)

        # 6j. 详情页被反爬中间页拦下（acw_sc__v2）
        ok &= _waf_selftest(base)

        # 6k. PDF 正文源选择（页脚规则文档 vs 真附件）
        ok &= _pdf_source_selftest()

        # 6l. HTML 列表反推健壮性（行本身是 <a>、工具类名、畸形 HTML）
        ok &= _html_list_robust_selftest()

        # 6m. 多份同构容器 / 绝对链接 / 双域名 / 瑞数识别
        ok &= _list_shape_selftest()

        # 6n. 生成器版本随响应返回（部署排查用）
        from . import manual as _manual
        ok &= _check('生成器带版本号', bool(_manual.GENERATOR_VERSION),
                     _manual.GENERATOR_VERSION)

        # 6f. 平台状态白名单：前端/生成器用 paused，runner 用 error
        ok &= _check('平台状态白名单含 paused/error',
                     all(s in recipe_mod.PLATFORM_STATUS
                         for s in ('active', 'paused', 'degraded', 'error')),
                     recipe_mod.PLATFORM_STATUS)

        # 7. 可选：任务幂等 / 领取 / 完成
        if os.environ.get('BIDCRAWL_SELFTEST_DB') == '1':
            from . import db as bdb, jobs
            bdb.init_schema()
            token = 'selftest-%d' % os.getpid()
            job1, d1 = jobs.submit('run', '__selftest__', {'platform': '__selftest__'},
                                   'selftest', token)
            job2, d2 = jobs.submit('run', '__selftest__', {'platform': '__selftest__'},
                                   'selftest', token)
            ok &= _check('重复提交合并为同一任务', job1['id'] == job2['id'] and d2, (job1, job2))
            # 只在队列里仅有本任务时验证领取，避免抢走真实业务任务
            cnt = bdb.one("SELECT count(*) AS c FROM bid_crawl_job WHERE status='queued'")
            if int(cnt['c']) == 1:
                claimed = jobs.claim_one('selftest-worker')
                ok &= _check('worker 可领取任务',
                             bool(claimed) and claimed['status'] == 'running', claimed)
            else:
                print('SKIP worker 领取（队列中还有其他任务，避免干扰）')
            jobs.set_progress(job1['id'], 'selftest', 50, '自检', force=True)
            jobs.finish(job1['id'], 'ok', {'ok': True})
            done = jobs.get_job(job1['id'])
            ok &= _check('任务终态 ok', done['status'] == 'ok' and done['progress'] == 100, done)
            bdb.x('DELETE FROM bid_crawl_job WHERE platform_id=%s', ('__selftest__',))
            bdb.commit()

            # 平台停用（paused）与配方删除
            spid = '__selftest_status__'
            bdb.x("INSERT INTO bid_platform(id,name,host,status) VALUES(%s,%s,%s,'draft') "
                  "ON CONFLICT (id) DO UPDATE SET status='draft', active_ver=NULL",
                  (spid, 'selftest', 'x.cn'))
            bdb.x('DELETE FROM bid_recipe_version WHERE platform_id=%s', (spid,))
            bdb.x('DELETE FROM bid_recipe_field WHERE platform_id=%s', (spid,))
            bdb.commit()
            ok &= _check('平台状态接受 paused', recipe_mod.set_status(spid, 'paused'))
            bdb.x('INSERT INTO bid_recipe_version(platform_id,version,recipe,created_by) '
                  "VALUES(%s,1,%s,'selftest') ON CONFLICT (platform_id,version) DO NOTHING",
                  (spid, json.dumps({'meta': {'id': spid}})))
            bdb.commit()
            dout = recipe_mod.delete_recipe(spid)
            left = bdb.one('SELECT count(*) AS c FROM bid_recipe_version WHERE platform_id=%s',
                           (spid,))
            prow = bdb.one('SELECT status, active_ver FROM bid_platform WHERE id=%s', (spid,))
            ok &= _check('删除配方清空版本并停用',
                         dout['versions_deleted'] >= 1 and int(left['c']) == 0
                         and prow['status'] == 'paused' and prow['active_ver'] is None,
                         (dout, left, prow))
            # 该夹具平台本身也要删掉，否则每跑一次自检就在库里留一个空平台
            bdb.x('DELETE FROM bid_recipe_field WHERE platform_id=%s', (spid,))
            bdb.x('DELETE FROM bid_recipe_version WHERE platform_id=%s', (spid,))
            bdb.x('DELETE FROM bid_platform WHERE id=%s', (spid,))
            bdb.commit()
            # 平台彻底删除：级联清理 + 运行任务保护
            dpid = '__selftest_delete__'
            for t in ('bid_crawl_job', 'bid_crawl_run', 'bid_item', 'bid_detail_ai',
                      'bid_recipe_field', 'bid_recipe_version',
                      'bid_platform_credential', 'bid_platform'):
                key = 'platform_id' if t != 'bid_platform' else 'id'
                bdb.x('DELETE FROM %s WHERE %s=%%s' % (t, key), (dpid,))
            for t in ('bid_keyword', 'bid_buyer_pattern', 'bid_exclude_rule'):
                bdb.x('DELETE FROM %s WHERE scope=%%s' % t, (dpid,))
            bdb.x("INSERT INTO bid_platform(id,name,host,status) VALUES(%s,%s,%s,'paused')",
                  (dpid, 'selftest-delete', 'x.cn'))
            bdb.x('INSERT INTO bid_recipe_version(platform_id,version,recipe,created_by) '
                  "VALUES(%s,1,%s,'selftest')", (dpid, json.dumps({'meta': {'id': dpid}})))
            bdb.x('INSERT INTO bid_item(platform_id,url,url_hash,title) '
                  "VALUES(%s,%s,%s,%s)", (dpid, 'http://x.cn/a', 'a' * 40, '自检条目'))
            bdb.commit()
            # 先验证有活动任务时拒绝删除
            bdb.x("INSERT INTO bid_crawl_job(job_type,platform_id,dedupe_key,params,status) "
                  "VALUES('run',%s,%s,%s,'queued')",
                  (dpid, 'selftest-del-' + str(os.getpid()), json.dumps({'platform': dpid})))
            bdb.commit()
            try:
                recipe_mod.delete_platform(dpid)
                blocked = False
            except recipe_mod.RecipeError:
                blocked = True
            ok &= _check('有活动任务时拒绝删除平台', blocked)
            # 强删：清理全部关联数据与平台本身
            out = recipe_mod.delete_platform(dpid, force=True)
            gone = bdb.one('SELECT count(*) AS c FROM bid_platform WHERE id=%s', (dpid,))
            items = bdb.one('SELECT count(*) AS c FROM bid_item WHERE platform_id=%s', (dpid,))
            jobs_left = bdb.one('SELECT count(*) AS c FROM bid_crawl_job WHERE platform_id=%s',
                                (dpid,))
            ok &= _check('强删平台级联清理全部数据',
                         int(gone['c']) == 0 and out['deleted'].get('items') >= 1
                         and out['deleted'].get('recipes') >= 1
                         and int(items['c']) == 0 and int(jobs_left['c']) == 0,
                         (out, gone, items, jobs_left))

        # 8. 可选：同一配方连跑两次，验证幂等与去重
        if os.environ.get('BIDCRAWL_SELFTEST_DB') == '1':
            ok &= _runner_selftest(base)

        # 9. 可选：接口层 CRUD / 任务提交（需要数据库）
        if os.environ.get('BIDCRAWL_SELFTEST_API') == '1':
            ok &= _api_selftest()
    finally:
        srv.shutdown()
    print('\n结果：%s' % ('全部通过' if ok else '存在失败项'))
    return 0 if ok else 1


def _runner_selftest(base: str) -> bool:
    from . import db as bdb, runner

    ok = True
    pid = 'fixture'
    raw = _fixture_recipe(base)
    raw['meta'].update({'id': pid, 'name': '夹具', 'host': '127.0.0.1'})
    raw['detail']['enabled'] = False
    try:
        recipe_mod.save_version(raw, created_by='selftest', activate=True)
        job = {'id': 0, 'job_type': 'run',
               'params': {'platform': pid, 'keywords': ['门头']}}
        prog = lambda *a, **k: None
        r1 = runner.run_job(job, prog, lambda: False)
        c1 = bdb.one('SELECT count(*) AS c FROM bid_item WHERE platform_id=%s', (pid,))
        r2 = runner.run_job(job, prog, lambda: False)
        c2 = bdb.one('SELECT count(*) AS c FROM bid_item WHERE platform_id=%s', (pid,))
        ok &= _check('首跑入库 120 条', int(c1['c']) == TOTAL, c1)
        ok &= _check('首跑新增计数正确', r1['inserted'] == TOTAL, r1.get('inserted'))
        ok &= _check('二跑不重复入库',
                     int(c2['c']) == TOTAL and r2['inserted'] == 0, (c2, r2.get('inserted')))
        ok &= _check('二跑仍 3 次请求', r2['requests'] == 3, r2.get('requests'))
        runs = bdb.one('SELECT count(*) AS c FROM bid_crawl_run WHERE platform_id=%s', (pid,))
        ok &= _check('两次运行均留痕', int(runs['c']) == 2, runs)

        # 平台并行：两个平台同一任务内并发跑，各自完整入库、顺序与进度都要对
        pids = ['fixture_a', 'fixture_b']
        for x in pids:
            r = _fixture_recipe(base)
            r['meta'].update({'id': x, 'name': '夹具-' + x[-1], 'host': '127.0.0.1'})
            r['detail']['enabled'] = False
            recipe_mod.save_version(r, created_by='selftest', activate=True)
        kwname = '门头'
        kw = bdb.one('SELECT id, hits_last FROM bid_keyword WHERE word=%s '
                     "AND scope='global'", (kwname,))
        seen_pct = []

        def _prog(stage, pct, message='', force=False):
            seen_pct.append(float(pct or 0))

        pr = runner.run_job({'id': 0, 'job_type': 'run',
                             'params': {'platforms': pids, 'keywords': [kwname]}},
                            _prog, lambda: False)
        cnt = {x: int(bdb.one('SELECT count(*) AS c FROM bid_item WHERE platform_id=%s',
                              (x,))['c']) for x in pids}
        ok &= _check('并行：两个平台都跑完', len(pr['platforms']) == 2, pr.get('parallel'))
        ok &= _check('并行：确实开了多线程', pr.get('parallel') == 2, pr.get('parallel'))
        ok &= _check('并行：平台顺序与目标一致',
                     [x['platform_id'] for x in pr['platforms']] == pids,
                     [x['platform_id'] for x in pr['platforms']])
        ok &= _check('并行：两平台各自完整入库',
                     all(cnt[x] == TOTAL for x in pids) and pr['inserted'] == 2 * TOTAL,
                     (cnt, pr.get('inserted')))
        ok &= _check('并行：进度单调不倒退且到 100',
                     seen_pct == sorted(seen_pct) and seen_pct[-1] >= 99.9,
                     (len(seen_pct), seen_pct[:3], seen_pct[-2:]))
        if kw:
            hits = bdb.one('SELECT hits_last FROM bid_keyword WHERE id=%s', (kw['id'],))
            ok &= _check('并行：同名关键词命中数合并写库',
                         int(hits['hits_last'] or 0) >= 2 * TOTAL, hits)

        # 目标平台解析：选「全部启用平台」必须落到所有启用平台，
        # 不能因为参数里残留一个 platform 就只跑单站
        all_t = runner._targets({})
        ok &= _check('目标解析：全部平台覆盖所有启用平台', len(all_t) >= 3,
                     [x['id'] for x in all_t])
        ok &= _check('目标解析：platform 只跑一个',
                     [x['id'] for x in runner._targets({'platform': 'fixture'})] == ['fixture'])
        ok &= _check('目标解析：platforms 列表按列表执行',
                     sorted(x['id'] for x in runner._targets({'platforms': pids})) == sorted(pids),
                     [x['id'] for x in runner._targets({'platforms': pids})])
    finally:
        try:
            bdb.x("DELETE FROM bid_item WHERE platform_id IN "
                  "('fixture','fixture_a','fixture_b')")
            bdb.x("DELETE FROM bid_crawl_run WHERE platform_id IN "
                  "('fixture','fixture_a','fixture_b')")
            bdb.x('DELETE FROM bid_fetch_cache WHERE url LIKE %s', (base + '%',))
            bdb.x("DELETE FROM bid_platform WHERE id IN "
                  "('fixture','fixture_a','fixture_b')")
            if locals().get('kw'):
                bdb.x('UPDATE bid_keyword SET hits_last=%s WHERE id=%s',
                      (kw.get('hits_last') or 0, kw['id']))
            bdb.commit()
        except Exception:
            bdb.rollback()
    return ok


def _api_selftest() -> bool:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from .. import bidcrawl_api
    from . import db as bdb

    ok = True
    pid = '__selftest__'
    word = '自检关键词_%d' % os.getpid()
    app = FastAPI()
    app.include_router(bidcrawl_api.build_router(
        lambda: 'selftest', {'need': lambda *a: None}))
    c = TestClient(app)
    try:
        r = c.get('/api/bidcrawl/keywords')
        ok &= _check('接口 关键词列表', r.status_code == 200, r.text[:200])
        r = c.post('/api/bidcrawl/keywords',
                   json={'word': word, 'scope': 'global', 'note': 'selftest'})
        ok &= _check('接口 新建关键词', r.status_code == 200, r.text[:200])
        kid = (r.json() or {}).get('id')
        r = c.put('/api/bidcrawl/keywords/%s' % kid, json={'enabled': False})
        ok &= _check('接口 修改关键词', r.status_code == 200 and
                     r.json().get('enabled') is False, r.text[:200])
        r = c.delete('/api/bidcrawl/keywords/%s' % kid)
        ok &= _check('接口 删除关键词（软删）', r.status_code == 200, r.text[:200])

        # 字段正则：普通员工维护匹配词，系统拼正则
        r = c.get('/api/bidcrawl/regex')
        rf = r.json() if r.status_code == 200 else {}
        ok &= _check('接口 正则字段列表', r.status_code == 200 and
                     len(rf.get('fields') or []) >= 5 and not rf.get('errors'),
                     (r.status_code, (r.text or '')[:150]))
        r = c.post('/api/bidcrawl/regex/aliases',
                   json={'field_key': 'budget', 'alias': word, 'scope': 'global'})
        aid = (r.json() or {}).get('id')
        ok &= _check('接口 新增匹配词', r.status_code == 200 and bool(aid), r.text[:200])
        r = c.post('/api/bidcrawl/regex/aliases',
                   json={'field_key': 'budget', 'alias': word, 'scope': 'global'})
        ok &= _check('接口 重复匹配词幂等',
                     r.status_code == 200 and r.json().get('id') == aid, r.text[:200])
        r = c.post('/api/bidcrawl/regex/test', json={'text': word + '：123 万元'})
        bv = [x for x in (r.json().get('fields') or []) if x['field_key'] == 'budget']
        ok &= _check('接口 匹配测试能命中新加的写法',
                     r.status_code == 200 and bool(bv) and bv[0]['value'].startswith('123'),
                     (r.status_code, bv[:1]))
        r = c.put('/api/bidcrawl/regex/aliases/%s' % aid, json={'enabled': False})
        ok &= _check('接口 停用匹配词',
                     r.status_code == 200 and r.json().get('enabled') is False, r.text[:150])
        r = c.delete('/api/bidcrawl/regex/aliases/%s' % aid)
        ok &= _check('接口 删除匹配词', r.status_code == 200, r.text[:150])
        r = c.put('/api/bidcrawl/regex/fields/budget', json={'pattern_tpl': '(?:{alts}['})
        ok &= _check('接口 非法正则模板被拒', r.status_code == 400, r.text[:150])
        r = c.post('/api/bidcrawl/regex/aliases',
                   json={'field_key': '__nope__', 'alias': 'x'})
        ok &= _check('接口 不存在的字段被拒', r.status_code == 400, r.text[:150])

        r = c.post('/api/bidcrawl/platforms',
                   json={'id': pid, 'name': '接口自检', 'host': '127.0.0.1'})
        ok &= _check('接口 新建平台', r.status_code == 200, r.text[:200])

        raw = json.loads((Path(__file__).with_name('recipes') / 'cfcpn.json')
                         .read_text(encoding='utf-8'))
        raw['meta'].update({'id': pid, 'name': '接口自检', 'host': '127.0.0.1'})
        r = c.put('/api/bidcrawl/platforms/%s/recipe' % pid, json={'recipe': raw})
        ok &= _check('接口 保存配方', r.status_code == 200 and
                     r.json().get('version') == 1, r.text[:300])
        r = c.get('/api/bidcrawl/platforms/%s/recipe' % pid)
        ok &= _check('接口 读取配方', r.status_code == 200 and
                     (r.json().get('recipe') or {}).get('meta', {}).get('id') == pid,
                     r.text[:200])
        r = c.get('/api/bidcrawl/platforms/%s/fields' % pid)
        ok &= _check('接口 字段投影', r.status_code == 200 and
                     len(r.json().get('fields') or []) >= 5, r.text[:200])

        token = 'api-selftest-%d' % os.getpid()
        body = {'job_type': 'run', 'platform_id': pid,
                'params': {'platform': pid}, 'client_token': token}
        r1 = c.post('/api/bidcrawl/jobs', json=body)
        r2 = c.post('/api/bidcrawl/jobs', json=body)
        ok &= _check('接口 提交任务', r1.status_code == 202, r1.text[:200])
        ok &= _check('接口 重复提交合并',
                     r2.status_code == 200 and
                     r2.json().get('job_id') == r1.json().get('job_id'),
                     (r1.text[:120], r2.text[:120]))
        jid = r1.json().get('job_id')
        r = c.get('/api/bidcrawl/jobs/%s' % jid)
        ok &= _check('接口 任务详情', r.status_code == 200 and
                     r.json().get('status') == 'queued', r.text[:200])
        r = c.post('/api/bidcrawl/jobs/%s/cancel' % jid)
        ok &= _check('接口 取消任务', r.status_code == 200 and
                     r.json().get('cancelled') is True, r.text[:200])
        r = c.post('/api/bidcrawl/rules/preview', json={'platform_id': pid})
        ok &= _check('接口 规则预览', r.status_code == 200, r.text[:200])
        r = c.get('/api/bidcrawl/items', params={'platform_id': pid})
        ok &= _check('接口 结果列表', r.status_code == 200, r.text[:200])
        # 删除平台：rules/preview 留下活动任务，普通删除应先被拒绝，force 再强删
        r = c.delete('/api/bidcrawl/platforms/%s' % pid)
        ok &= _check('接口 活动任务时拒绝删平台', r.status_code == 400 and
                     '任务' in r.text, r.text[:200])
        r = c.delete('/api/bidcrawl/platforms/%s?force=1' % pid)
        ok &= _check('接口 强删平台成功', r.status_code == 200 and
                     (r.json().get('deleted') or {}).get('recipes', 0) >= 1, r.text[:300])
        r = c.get('/api/bidcrawl/platforms/%s/recipe' % pid)
        ok &= _check('接口 删除后平台不存在', r.status_code == 404, r.text[:200])

        # 定时任务：CRUD + 权限点
        from . import scheduler as bsched
        ok &= _check('定时：HH:MM 解析（全角冒号/越界）',
                     bsched.hhmm_to_min('9：30') == 570
                     and bsched.hhmm_to_min('24:00') is None
                     and bsched.hhmm_to_min('abc') is None)
        _n = datetime(2026, 11, 17, 15, 0)
        ok &= _check('定时：过点的任务当天不再触发',
                     bsched.initial_last_run_date('09:00', _n) == _n.date()
                     and bsched.initial_last_run_date('18:00', _n) is None)
        ok &= _check('定时：空平台/关键词表示「全部」',
                     bsched.build_params({'id': 1, 'platforms': [], 'keywords': []})
                     == {'dry_run': False, 'schedule_id': 1})
        ok &= _check('定时：选中的平台与关键词进 params',
                     bsched.build_params({'id': 2, 'platforms': ['a'], 'keywords': ['门头']})
                     == {'dry_run': False, 'schedule_id': 2,
                         'platforms': ['a'], 'keywords': ['门头']})
        ok &= _check('定时：下次执行时间顺延到明天',
                     bsched.next_run_at({'run_at': '09:00', 'enabled': True,
                                         'last_run_date': _n.date()}, _n)
                     == datetime(2026, 11, 18, 9, 0))

        sched_app = FastAPI()
        sched_app.include_router(bidcrawl_api.build_schedule_router(
            lambda: 'selftest', {'need': lambda *a, **k: None}))
        sc = TestClient(sched_app)
        r = sc.post('/api/bidcrawl/schedules',
                    json={'name': '自检定时', 'run_at': '00:00', 'enabled': False})
        srow = r.json() if r.status_code == 200 else {}
        ok &= _check('接口 新建定时任务', r.status_code == 200 and srow.get('id'), r.text[:200])
        ok &= _check('定时：已过时间的任务标记当天已跑',
                     str(srow.get('last_run_date')) == str(datetime.now().date()),
                     srow.get('last_run_date'))
        sid = srow.get('id')
        r = sc.get('/api/bidcrawl/schedules')
        ok &= _check('接口 定时任务列表', r.status_code == 200 and
                     any(x['id'] == sid for x in r.json().get('schedules') or []), r.text[:200])
        r = sc.put('/api/bidcrawl/schedules/%s' % sid,
                   json={'name': '自检定时2', 'run_at': '09:15', 'enabled': False})
        ok &= _check('接口 修改定时任务', r.status_code == 200 and
                     r.json().get('run_at') == '09:15', r.text[:200])
        r = sc.post('/api/bidcrawl/schedules', json={'run_at': '25:00'})
        ok &= _check('接口 定时任务时间非法被拒', r.status_code == 400, r.text[:200])
        r = sc.post('/api/bidcrawl/schedules', json={'run_at': '09:00', 'platforms': ['__nope__']})
        ok &= _check('接口 定时任务平台不存在被拒', r.status_code == 400, r.text[:200])
        r = sc.delete('/api/bidcrawl/schedules/%s' % sid)
        ok &= _check('接口 删除定时任务', r.status_code == 200, r.text[:200])

        def _deny(*a, **k):
            from fastapi import HTTPException
            raise HTTPException(403, 'denied')

        deny_app = FastAPI()
        deny_app.include_router(bidcrawl_api.build_schedule_router(
            lambda: 'nobody', {'need': _deny}))
        dc = TestClient(deny_app)
        ok &= _check('定时任务：无 view.schedule 时列表 403',
                     dc.get('/api/bidcrawl/schedules').status_code == 403)
        ok &= _check('定时任务：无权限时增删改全拦',
                     dc.post('/api/bidcrawl/schedules', json={'run_at': '09:00'}).status_code == 403
                     and dc.put('/api/bidcrawl/schedules/1', json={'run_at': '09:00'}).status_code == 403
                     and dc.delete('/api/bidcrawl/schedules/1').status_code == 403
                     and dc.post('/api/bidcrawl/schedules/1/run', json={}).status_code == 403)
    finally:
        try:
            bdb.x('DELETE FROM bid_crawl_job WHERE platform_id=%s', (pid,))
            bdb.x('DELETE FROM bid_platform WHERE id=%s', (pid,))
            bdb.x("DELETE FROM bid_crawl_schedule WHERE name LIKE %s", ('自检定时%',))
            bdb.x("DELETE FROM bid_regex_alias WHERE alias LIKE %s", ('自检关键词%',))
            bdb.commit()
        except Exception:
            bdb.rollback()
    return ok


if __name__ == '__main__':
    sys.exit(main())
