# -*- coding: utf-8 -*-
"""无头浏览器嗅探：用系统 Edge + CDP 抓 SPA 站点的真实 XHR。

为什么需要它
------------
部分站点是 SPA（如 jc.abchina.com.cn/puc/#/biddingPage）：
入口 HTML 只有 20KB 空壳，主包是 eval 混淆的 webpack 产物，
静态抓取拿不到任何接口线索。唯一通用解法是**真实渲染页面**，
监听它自己发出的 XHR / Fetch，把「接口 + 请求体 + 响应结构」交给配方生成器。

实现方式
--------
不依赖 Playwright（内网 pip 常装不到）：直接启动系统 Edge 的
`--headless=new --remote-debugging-port`，通过 CDP 的 Page/Network 域
收集请求。websockets 提供同步客户端，和现有探测器的同步流程一致。

失败时一律返回 None 并给出原因，调用方退回静态探测，不影响既有站点。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

UA_HINT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' \
          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# 只关心取数接口，静态资源/埋点一律忽略
SKIP_URL = re.compile(
    r'(\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|mp4|webp)(?:\?|$)|'
    r'/sensors?/|/collect|/beacon|/log|analytics|google|baidu\.com|'
    r'hm\.baidu|/report|/heartbeat)', re.I)
KEEP_TYPE = ('xhr', 'fetch')


def find_browser() -> str:
    """找本机 Edge / Chrome 可执行文件，找不到返回空串。"""
    cands = [
        os.environ.get('BIDCRAWL_BROWSER', ''),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    for name in ('msedge', 'chrome', 'chromium', 'google-chrome'):
        hit = shutil.which(name)
        if hit:
            return hit
    return ''


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class _CDP:
    """极简 CDP 客户端：发命令、收事件。"""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect
        self._ws = connect(ws_url, open_timeout=10, max_size=64 * 1024 * 1024)
        self._id = 0

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def send(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({'id': mid, 'method': method,
                                  'params': params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError(msg['error'].get('message') or 'CDP error')
                return msg.get('result') or {}
        return {}

    def drain(self, seconds: float) -> list[dict]:
        """收集一段时间内的事件。"""
        out: list[dict] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(0.05, deadline - time.monotonic()))
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if 'method' in msg:
                out.append(msg)
        return out


# 同一时刻只允许一个嗅探进程：每次都会拉起一个浏览器，并发会吃满内存。
_LOCK = threading.Lock()


def sniff(url: str, keyword: str = '', *, timeout_s: float = 35,
          wait_ms: int = 4000) -> dict:
    """用无头浏览器打开 url，收集 XHR/Fetch 请求。

    返回 {'ok': bool, 'requests': [...], 'reason': str, 'browser': str}；
    每个 request 含 url/method/post_data/status/mime/body/body_chars。
    """
    if os.environ.get('BIDCRAWL_NO_BROWSER') == '1':
        return {'ok': False, 'requests': [], 'browser': '',
                'reason': '已通过 BIDCRAWL_NO_BROWSER=1 禁用浏览器嗅探',
                'url': url}
    if not _LOCK.acquire(timeout=max(5, timeout_s)):
        return {'ok': False, 'requests': [], 'browser': '',
                'reason': '另一个浏览器嗅探正在进行，请稍后重试', 'url': url}
    try:
        return _sniff(url, keyword, timeout_s=timeout_s, wait_ms=wait_ms)
    finally:
        _LOCK.release()


def _sniff(url: str, keyword: str = '', *, timeout_s: float = 35,
           wait_ms: int = 4000) -> dict:
    exe = find_browser()
    out: dict[str, Any] = {'ok': False, 'requests': [], 'reason': '',
                           'browser': exe, 'url': url}
    if not exe:
        out['reason'] = '未找到 Edge/Chrome，可设置 BIDCRAWL_BROWSER 指向浏览器'
        return out

    port = _free_port()
    profile = tempfile.mkdtemp(prefix='bidcrawl-cdp-')
    args = [exe, '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--disable-extensions',
            '--disable-background-networking', '--mute-audio',
            '--disable-features=Translate,BackForwardCache',
            '--remote-debugging-port=%d' % port,
            '--user-data-dir=%s' % profile,
            '--window-size=1366,900', 'about:blank']
    proc = None
    cdp = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        ws_url = ''
        import httpx
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                r = httpx.get('http://127.0.0.1:%d/json/version' % port,
                              timeout=2, trust_env=False)
                if r.status_code == 200:
                    ws_url = (r.json() or {}).get('webSocketDebuggerUrl') or ''
                    if ws_url:
                        break
            except Exception:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if not ws_url:
            out['reason'] = '浏览器未能启动调试端口'
            return out

        # 连浏览器级 ws，创建一个独立 tab，避免干扰用户已开页面
        brow = _CDP(ws_url)
        try:
            tgt = brow.send('Target.createTarget', {'url': 'about:blank'})
            tid = tgt.get('targetId') or ''
            if not tid:
                out['reason'] = '创建标签页失败'
                return out
            # flatten 模式：直接对 page target 发命令
            brow.send('Target.attachToTarget', {'targetId': tid, 'flatten': True})
        finally:
            brow.close()

        # 连 page 级 ws（带 session 更稳；这里用 target 专属 ws）
        page_ws = 'ws://127.0.0.1:%d/devtools/page/%s' % (port, tid)
        cdp = _CDP(page_ws)
        cdp.send('Page.enable')
        cdp.send('Network.enable', {'maxTotalBufferSize': 20 * 1024 * 1024,
                                    'maxResourceBufferSize': 8 * 1024 * 1024})
        cdp.send('Runtime.enable')
        cdp.send('Page.navigate', {'url': url}, timeout=20)

        # 等页面加载 + 前端接口发出；先等 load，再补一段 idle 收集
        events: list[dict] = []
        deadline = time.monotonic() + timeout_s
        loaded = False
        while time.monotonic() < deadline:
            batch = cdp.drain(1.0)
            events.extend(batch)
            if any(e.get('method') == 'Page.loadEventFired' for e in events):
                loaded = True
                break
        # SPA 在 load 之后才发接口，继续收 wait_ms
        events.extend(cdp.drain(wait_ms / 1000.0))

        reqs: dict[str, dict] = {}
        order: list[str] = []
        for e in events:
            m = e.get('method')
            p = e.get('params') or {}
            if m == 'Network.requestWillBeSent':
                rid = p.get('requestId') or ''
                req = p.get('request') or {}
                rurl = str(req.get('url') or '')
                rtype = str(p.get('type') or '')
                if rtype.lower() in KEEP_TYPE or (
                        not rtype and re.search(r'(api|gateway|query|search|list|json)', rurl, re.I)):
                    if SKIP_URL.search(rurl):
                        continue
                    reqs[rid] = {'url': rurl, 'method': req.get('method') or 'GET',
                                 'post_data': req.get('postData') or '',
                                 'headers': req.get('headers') or {},
                                 'status': None, 'mime': '', 'body': '',
                                 'body_chars': 0, 'type': rtype}
                    order.append(rid)
            elif m == 'Network.responseReceived':
                rid = p.get('response', {}) and p.get('requestId') or ''
                r = p.get('response') or {}
                if rid in reqs:
                    reqs[rid]['status'] = r.get('status')
                    reqs[rid]['mime'] = str(r.get('mimeType') or '')
                    reqs[rid]['url'] = reqs[rid]['url'] or str(r.get('url') or '')
            elif m == 'Network.loadingFinished':
                rid = p.get('requestId') or ''
                if rid in reqs and not reqs[rid]['body']:
                    try:
                        got = cdp.send('Network.getResponseBody', {'requestId': rid},
                                       timeout=8)
                        reqs[rid]['body'] = got.get('body') or ''
                        reqs[rid]['body_chars'] = len(reqs[rid]['body'])
                    except Exception:
                        pass

        # 响应体可能很大（列表 JSON 几十 KB），补抓最有希望的几个
        for rid in order[:24]:
            r = reqs[rid]
            if r['body']:
                continue
            mime = r['mime'].lower()
            if 'json' in mime or re.search(r'(api|gateway|query|search|list)', r['url'], re.I):
                try:
                    got = cdp.send('Network.getResponseBody', {'requestId': rid}, timeout=6)
                    r['body'] = got.get('body') or ''
                    r['body_chars'] = len(r['body'])
                except Exception:
                    pass

        out['requests'] = [reqs[i] for i in order if i in reqs]
        out['ok'] = bool(out['requests'])
        out['loaded'] = loaded
        if not out['ok']:
            out['reason'] = '页面已渲染但未捕获到 XHR/Fetch 取数请求'
        return out
    except Exception as e:
        out['reason'] = '浏览器嗅探异常：%s' % str(e)[:200]
        return out
    finally:
        if cdp:
            cdp.close()
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass


def _launch(timeout_s: float) -> tuple:
    """启动无头浏览器并连上 page target，返回 (proc, cdp, port, tid, profile)。"""
    exe = find_browser()
    if not exe:
        return None, None, '', '', ''
    port = _free_port()
    profile = tempfile.mkdtemp(prefix='bidcrawl-cdp-')
    args = [exe, '--headless=new', '--disable-gpu', '--no-first-run',
            '--no-default-browser-check', '--disable-extensions',
            '--disable-background-networking', '--mute-audio',
            '--disable-features=Translate,BackForwardCache',
            '--remote-debugging-port=%d' % port,
            '--user-data-dir=%s' % profile,
            '--window-size=1366,900', 'about:blank']
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    import httpx
    ws_url = ''
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get('http://127.0.0.1:%d/json/version' % port,
                          timeout=2, trust_env=False)
            if r.status_code == 200:
                ws_url = (r.json() or {}).get('webSocketDebuggerUrl') or ''
                if ws_url:
                    break
        except Exception:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if not ws_url:
        try:
            proc.kill()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        return None, None, '', '', ''
    brow = _CDP(ws_url)
    try:
        tgt = brow.send('Target.createTarget', {'url': 'about:blank'})
        tid = tgt.get('targetId') or ''
        if not tid:
            return proc, None, port, '', profile
        brow.send('Target.attachToTarget', {'targetId': tid, 'flatten': True})
    finally:
        brow.close()
    cdp = _CDP('ws://127.0.0.1:%d/devtools/page/%s' % (port, tid))
    return proc, cdp, port, tid, profile


# 阿里云 WAF 的 JS 挑战页特征：arg1 + acw_sc__v2(或 __jsl_clearance)
WAF_SIG = re.compile(r"arg1\s*=|acw_sc__v2|__jsl_clearance|\baliyunwaf_", re.I)


def is_waf_page(html: str) -> bool:
    """判断响应是否是反爬中间页（而不是真正的内容页）。"""
    if not html:
        return False
    head = html[:6000]
    if 'arg1=' in head or 'acw_sc__v2' in head:
        return True
    # 瑞数（RiverSecurity）：$_ts + 动态脚本，常见 412
    if '$_ts' in head and ('$_ts.cd' in head or '$_ts.nsd' in head):
        return True
    return bool(WAF_SIG.search(head)) and len(html) < 60000


def fetch_page(url: str, *, timeout_s: float = 25, wait_ms: int = 1500,
               referer: str = '', ua: str = '') -> dict:
    """用无头浏览器打开页面，返回渲染后的 HTML 与 Cookie。

    用途：详情页被 WAF 的 JS 挑战拦下时，交给浏览器跑一遍挑战脚本，
    既可以直接拿渲染结果，也可以把换来的 cookie 交给 httpx 复用。

    注意：必须覆盖 UA。无头 Chrome 默认发 `HeadlessChrome/…`，部分站点
    （如 hnzb.chinaccsscm.cn 的瑞数）直接返回 400 + 空页面，连挑战页都不给。

    返回 {'ok','html','cookies'(dict),'url','reason','browser'}。
    """
    out: dict[str, Any] = {'ok': False, 'html': '', 'cookies': {}, 'url': url,
                           'reason': '', 'browser': find_browser()}
    if os.environ.get('BIDCRAWL_NO_BROWSER') == '1':
        out['reason'] = '已通过 BIDCRAWL_NO_BROWSER=1 禁用浏览器'
        return out
    if not out['browser']:
        out['reason'] = '未找到 Edge/Chrome，可设置 BIDCRAWL_BROWSER 指向浏览器'
        return out
    if not _LOCK.acquire(timeout=max(5, timeout_s)):
        out['reason'] = '浏览器正忙，请稍后重试'
        return out
    proc = cdp = None
    profile = ''
    try:
        proc, cdp, _port, tid, profile = _launch(timeout_s)
        if not cdp:
            out['reason'] = '浏览器未能启动调试端口'
            return out
        cdp.send('Page.enable')
        cdp.send('Network.enable')
        cdp.send('Runtime.enable')
        cdp.send('Emulation.setUserAgentOverride', {
            'userAgent': ua or UA_HINT,
            'acceptLanguage': 'zh-CN,zh;q=0.9,en;q=0.8',
            'platform': 'Win32'})
        if referer:
            cdp.send('Network.setExtraHTTPHeaders',
                     {'headers': {'Referer': referer}})
        cdp.send('Page.navigate', {'url': url}, timeout=20)

        deadline = time.monotonic() + timeout_s
        html = ''
        while time.monotonic() < deadline:
            cdp.drain(0.8)
            got = cdp.send('Runtime.evaluate', {
                'expression': 'document.documentElement.outerHTML',
                'returnByValue': True}, timeout=8)
            html = str(((got.get('result') or {}).get('value')) or '')
            # 挑战页会自己 reload，等到拿到非挑战内容再收工
            if html and len(html) > 2000 and not is_waf_page(html):
                break
        out['html'] = html
        try:
            ck = cdp.send('Network.getCookies', {'urls': [url]}, timeout=8)
            out['cookies'] = {c.get('name'): c.get('value')
                              for c in (ck.get('cookies') or [])
                              if c.get('name')}
        except Exception:
            out['cookies'] = {}
        out['ok'] = bool(html) and not is_waf_page(html)
        if not out['ok']:
            out['reason'] = '浏览器仍停留在反爬挑战页（可能是验证码/黑名单）'
        return out
    except Exception as e:
        out['reason'] = '浏览器取页异常：%s' % str(e)[:200]
        return out
    finally:
        if cdp:
            cdp.close()
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
        _LOCK.release()


def json_items(req: dict) -> tuple[str, list, Any]:
    """从嗅探到的响应体里找条目数组，返回 (jsonpath, items, data)。"""
    body = req.get('body') or ''
    if not body:
        return '', [], None
    try:
        data = json.loads(body)
    except Exception:
        return '', [], None
    from .probe import _find_items
    got = _find_items(data)
    if not got:
        return '', [], data
    path, items, _ = got
    return path, items, data


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'https://example.com/'
    kw = sys.argv[2] if len(sys.argv) > 2 else ''
    r = sniff(target, kw, timeout_s=40, wait_ms=5000)
    print('ok', r['ok'], 'browser', r['browser'], 'reason', r['reason'])
    for q in r['requests'][:15]:
        print(' ', q['method'], q['status'], q['url'][:110],
              'body', q['body_chars'], '|', (q['post_data'] or '')[:80])
