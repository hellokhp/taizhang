# -*- coding: utf-8 -*-
"""统一请求层：限速、重试、编码、超时、请求计数。

配方里只声明 transport 参数，本模块不认识任何具体站点。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin

import httpx

from .recipe import CompiledRecipe


class FetchError(RuntimeError):
    def __init__(self, msg: str, status: int = 0, url: str = ''):
        super().__init__(msg)
        self.status = status
        self.url = url


@dataclass
class FetchResult:
    ok: bool
    status: int
    url: str
    text: str = ''
    data: object = None
    content_type: str = ''
    elapsed_ms: int = 0
    error: str = ''
    request: dict = field(default_factory=dict)

    @property
    def is_json(self) -> bool:
        return 'json' in (self.content_type or '').lower() or \
            (self.text.lstrip()[:1] in ('{', '[') and self.status < 400)


def _fmt(value, ctx: dict):
    # 纯数字占位符保持数字类型，避免 JSON 接口因 "page":"1" 拒绝请求
    if isinstance(value, str):
        if value in ('{page}', '{size}'):
            try:
                return int(ctx.get(value[1:-1]) or 0)
            except Exception:
                return value
        try:
            return value.format(**ctx)
        except Exception:
            return value
    return value


def _render_deep(value, ctx: dict):
    """递归渲染请求体：{keyword}/{page}/{size} 可能嵌套在 dto/query 里。"""
    if isinstance(value, dict):
        return {k: _render_deep(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_deep(v, ctx) for v in value]
    return _fmt(value, ctx)


def _repair_url(u: str) -> str:
    """修 `https://hosthttps://other/...` 这种双域名（模板叠了绝对链接）。

    只在「第二个 scheme 出现在 host 段内」时修，避免误伤
    `?next=https://b.com` 这类合法查询串。
    """
    m = re.match(r'^(https?)://', u or '')
    if not m:
        return u
    rest = u[m.end():]
    stop = len(rest)
    for ch in '/?#':
        i = rest.find(ch)
        if i != -1:
            stop = min(stop, i)
    m2 = re.search(r'https?://', rest)
    # 起点落在 host 段内（含 `hosthttps:` 这种粘在一起的情况）才修
    return rest[m2.start():] if (m2 and m2.start() <= stop) else u


def _with_params(url: str, params: dict | None) -> str:
    """把 params 拼进 URL——浏览器只能导航，没法单独传查询参数。"""
    if not params:
        return url
    try:
        qs = urlencode({k: v for k, v in params.items() if v is not None})
    except Exception:
        return url
    if not qs:
        return url
    return url + ('&' if '?' in url else '?') + qs


def _is_waf_text(text: str) -> bool:
    """响应是否是反爬中间页（而不是真正的内容/JSON）。

    目前已见三类：
    - 阿里云 WAF：`arg1='…'` + 写 `acw_sc__v2` 后 reload；
    - 加速乐：`__jsl_clearance`；
    - 瑞数（RiverSecurity）：`$_ts=window['$_ts']` + `$_ts.cd` / `$_ts.nsd`，
      常见状态码 412。
    """
    if not text or len(text) > 200000:
        return False
    head = text[:6000]
    if 'arg1=' in head or 'acw_sc__v2' in head:
        return True
    if '$_ts' in head and ('$_ts.cd' in head or '$_ts.nsd' in head):
        return True
    return ('__jsl_clearance' in head or 'aliyunwaf_' in head) and len(text) < 60000


def _scale() -> float:
    try:
        return float(os.environ.get('BIDCRAWL_RATE_SCALE', '1'))
    except Exception:
        return 1.0


class Fetcher:
    """一个平台一个实例：限速按平台生效，翻页与详情共享。"""

    def __init__(self, recipe: CompiledRecipe, *, timeout_s: float | None = None):
        self.recipe = recipe
        tr = recipe.transport
        self.timeout_s = float(timeout_s or tr.get('timeout_s') or 20)
        rate = tr.get('rate_limit') or {}
        qps = float(rate.get('qps') or 0)
        self.interval = (1.0 / qps) if qps > 0 else 0.0
        self.retry = tr.get('retry') or {}
        # 单个请求（含重试）的总时长上限，避免站点不可达时重试循环把任务拖死
        self.total_timeout_s = float(tr.get('total_timeout_s') or
                                     min(120.0, self.timeout_s * 4 + 30))
        self.max_response_mb = float(tr.get('max_response_mb') or 5)
        self.requests = 0
        self.total_ms = 0
        # WAF 过盾：每次成功过盾能换到约 1 小时的 cookie，因此不必每条都拉
        # 浏览器；但一批里 cookie 中途过期还需要再换，故留出多次机会。
        self._waf_boots = 0
        self._waf_dead = False
        self._waf_reason = ''
        self._lock = threading.Lock()
        self._last = 0.0
        self._client = httpx.Client(
            timeout=self.timeout_s,
            follow_redirects=True,
            trust_env=bool(tr.get('trust_env', True)),
            headers=dict(tr.get('headers') or {}),
        )

    # ---- 生命周期 ----
    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 限速 ----
    def _wait(self):
        if self.interval <= 0:
            return
        with self._lock:
            wait = self.interval * _scale() - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _touch(self):
        """标记一次真实请求已发出，更新限速基准时间（重试也调用，避免下一个请求误算间隔）。"""
        if self.interval <= 0:
            return
        with self._lock:
            self._last = time.monotonic()

    # ---- 请求 ----
    def _base_url(self) -> str:
        """相对地址的基准：优先 meta.base_url，其次配方里的请求地址。"""
        base = str((self.recipe.meta or {}).get('base_url') or '').strip()
        if base:
            return base
        return str(self.recipe.transport.get('url') or '')

    def abs_url(self, url: str) -> str:
        u = str(url or '').strip()
        if u and not u.startswith(('http://', 'https://')):
            return urljoin(self._base_url(), u)
        return u

    # ---- WAF（阿里云 acw_sc__v2）过盾 ----
    @property
    def waf_reason(self) -> str:
        """上一次过盾失败的原因，供上层报错时引用。"""
        return self._waf_reason

    def _merge_cookies(self, cookies: dict) -> None:
        """把浏览器换来的 cookie 并进 client 默认头（同名覆盖）。"""
        if not cookies:
            return
        jar: dict[str, str] = {}
        for part in str(self._client.headers.get('cookie') or '').split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                jar[k.strip()] = v.strip()
        jar.update({str(k): str(v) for k, v in cookies.items() if v})
        self._client.headers['Cookie'] = '; '.join('%s=%s' % (k, v)
                                                   for k, v in jar.items())

    def _browser_bootstrap(self, url: str) -> FetchResult | None:
        """详情页被 JS 挑战拦下时，用无头浏览器跑一遍挑战拿正文与 cookie。

        成功后 cookie 会合并到 client，后续请求不必再启动浏览器；
        失败返回 None，由上层报出可操作的原因。
        """
        from .browser_probe import fetch_page
        if self._waf_dead or self._waf_boots >= 6:
            return None
        self._waf_boots += 1
        started = time.perf_counter()
        referer = ''
        for k, v in (self.recipe.transport.get('headers') or {}).items():
            if str(k).lower() == 'referer':
                referer = str(v)
                break
        got = fetch_page(url, referer=referer)
        elapsed = int((time.perf_counter() - started) * 1000)
        self.total_ms += elapsed
        self._merge_cookies(got.get('cookies') or {})
        html = got.get('html') or ''
        if not got.get('ok') or not html:
            # 浏览器也过不了（无 Edge/Chrome、验证码、IP 黑名单）：停止重试，
            # 避免每个条目都白拉一次浏览器。
            self._waf_reason = got.get('reason') or '浏览器未能取到正文'
            self._waf_dead = True
            return None
        self._waf_reason = ''
        return FetchResult(ok=True, status=200, url=url, text=html,
                           content_type='text/html', elapsed_ms=elapsed,
                           request={'method': 'GET', 'url': url,
                                    'params': {}, 'body': None,
                                    'via': 'browser'})

    def request(self, spec: dict, ctx: dict) -> FetchResult:
        """按 discovery.request 模板发请求；ctx 提供 {keyword}/{page}/{size}/{url}。"""
        url = _fmt(spec.get('url') or self.recipe.transport.get('url') or '', ctx)
        if not url:
            raise FetchError('配方缺少请求地址')
        # 列表里的链接常是站内相对路径（/zgczb/897997.jhtml），详情请求直接拼
        # 会抛「URL missing protocol」；这里统一用 meta.base_url 补成绝对地址。
        if not url.startswith(('http://', 'https://')):
            url = urljoin(self._base_url(), url)
        url = _repair_url(url)
        method = str(spec.get('method') or 'POST').upper()
        params = {k: _fmt(v, ctx) for k, v in (spec.get('params') or {}).items()}
        body = spec.get('body')
        if isinstance(body, (dict, list)):
            body = _render_deep(body, ctx)
        elif isinstance(body, str) and body:
            try:
                body = body.format(**ctx)
            except Exception:
                pass

        retry = self.retry
        times = int(retry.get('times') or 0)
        backoff = float(retry.get('backoff_s') or 3)
        retry_on = set(int(s) for s in (retry.get('retry_on') or [429, 500, 502, 503, 504]))
        deadline = time.monotonic() + self.total_timeout_s
        last = None
        for attempt in range(times + 1):
            if attempt == 0:
                # 限速只约束「下一次新请求」，重试不叠加限速等待（backoff 已负责退避）
                self._wait()
            elif time.monotonic() >= deadline:
                break
            started = time.perf_counter()
            self.requests += 1
            self._touch()
            try:
                kwargs = {'params': params or None}
                if spec.get('headers'):
                    # 详情页请求常带 {id} 模板（如 Referer 里的 searchVal），
                    # 与 params/body 一样按 ctx 格式化；无占位符时原样返回。
                    kwargs['headers'] = {k: _fmt(v, ctx)
                                         for k, v in spec['headers'].items()}
                # 根据 Content-Type 决定提交方式：
                #   application/json → json=body
                #   application/x-www-form-urlencoded → data=body
                #   其他 → content=body（原始字节）
                # 注意：Content-Type 通常写在 transport.headers，请求 spec 里往往没有，
                # 只查 spec 会漏判、把表单错发成 JSON（站点会忽略全部筛选条件）。
                ctype_hdr = ''
                for hs in (spec.get('headers') or {}, self.recipe.transport.get('headers') or {}):
                    for hk, hv in hs.items():
                        if str(hk).lower() == 'content-type':
                            ctype_hdr = str(hv or '')
                            break
                    if ctype_hdr:
                        break
                if isinstance(body, dict):
                    if 'form-urlencoded' in ctype_hdr:
                        kwargs['data'] = body
                    else:
                        kwargs['json'] = body
                elif body:
                    kwargs['content'] = body.encode('utf-8')
                resp = self._client.request(method, url, **kwargs)
                elapsed = int((time.perf_counter() - started) * 1000)
                self.total_ms += elapsed
                ctype = resp.headers.get('content-type', '')
                raw = resp.content or b''
                if len(raw) > self.max_response_mb * 1024 * 1024:
                    raise FetchError('响应体超过 %.1fMB' % self.max_response_mb,
                                     resp.status_code, url)
                enc = self.recipe.transport.get('encoding') or resp.encoding or 'utf-8'
                try:
                    text = raw.decode(enc, errors='replace')
                except LookupError:
                    text = raw.decode('utf-8', errors='replace')
                data = None
                if 'json' in ctype.lower() or text.lstrip()[:1] in ('{', '['):
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = None
                last = FetchResult(ok=resp.status_code < 400, status=resp.status_code,
                                   url=str(resp.url), text=text, data=data,
                                   content_type=ctype, elapsed_ms=elapsed,
                                   request={'method': method, 'url': url,
                                            'params': params, 'body': body})
                if last.ok and method == 'GET' and _is_waf_text(text):
                    # 反爬中间页（阿里云 acw_sc__v2 / 瑞数 $_ts / 加速乐）：正文被换成
                    # 一段 JS 挑战脚本，直接解析必然为空，交给浏览器过盾。
                    # 只对 GET 做：浏览器只能导航，POST 接口过盾没有意义。
                    fixed = self._browser_bootstrap(_with_params(url, params))
                    if fixed is not None:
                        return fixed
                elif (method == 'GET' and resp.status_code in (403, 412, 521, 555)
                      and _is_waf_text(text)):
                    # 瑞数常用 412/403 直接挡下，正文仍是挑战脚本
                    fixed = self._browser_bootstrap(_with_params(url, params))
                    if fixed is not None:
                        return fixed
                if resp.status_code < 400:
                    return last
                if resp.status_code not in retry_on:
                    return last
            except FetchError:
                raise
            except Exception as e:
                elapsed = int((time.perf_counter() - started) * 1000)
                self.total_ms += elapsed
                last = FetchResult(ok=False, status=0, url=url,
                                   elapsed_ms=elapsed, error=repr(e)[:300],
                                   request={'method': method, 'url': url,
                                            'params': params, 'body': body})
            if attempt < times:
                sleep_for = min(backoff * (attempt + 1), deadline - time.monotonic())
                if sleep_for > 0:
                    time.sleep(sleep_for)
            if time.monotonic() >= deadline:
                break
        return last or FetchResult(ok=False, status=0, url=url, error='请求失败')

    def fetch_detail(self, url: str, ctx: dict | None = None) -> FetchResult:
        """按配方声明取详情。

        兼容两种形态：
        - 旧配方只有 GET {url}：直接请求详情页 HTML；
        - 新配方可声明独立 url/method/body/headers（如 POST id={id}&isDetail=1），
          模板占位符由 ctx 提供，仍复用同一限速、重试、编码与响应解析。
        """
        detail_req = (self.recipe.detail.get('request') or {})
        spec = {'url': detail_req.get('url') or url,
                'method': detail_req.get('method') or 'GET',
                'headers': detail_req.get('headers') or {}}
        if detail_req.get('params'):
            spec['params'] = dict(detail_req['params'])
        if detail_req.get('body') is not None:
            spec['body'] = detail_req['body']
        base = {'url': self.abs_url(url), 'keyword': '', 'page': 1, 'size': 0, 'id': ''}
        if ctx:
            base.update(ctx)
            base['url'] = self.abs_url(base.get('url') or url)
        return self.request(spec, base)

    def fetch_url(self, url: str) -> FetchResult:
        return self.request({'url': url, 'method': 'GET'}, {'url': url})
