# -*- coding: utf-8 -*-
"""标讯采集命令行。

    python -m server.bidcrawl.cli init
    python -m server.bidcrawl.cli import-recipes
    python -m server.bidcrawl.cli run --platform cfcpn --dry-run
    python -m server.bidcrawl.cli probe-size --platform cfcpn --keyword 门头
    python -m server.bidcrawl.cli verify --platform cfcpn
    python -m server.bidcrawl.cli worker
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, db, jobs, paginate, recipe as recipe_mod, runner
from .transport import Fetcher

RECIPE_DIR = Path(__file__).resolve().parent / 'recipes'


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args):
    db.init_schema()
    print('建表完成（bid_* 全部表 + 种子配置）')


def cmd_import(args):
    path = Path(args.path) if args.path else RECIPE_DIR
    if not path.exists():
        raise SystemExit('配方目录不存在：%s' % path)
    db.init_schema()
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    if not files:
        raise SystemExit('目录下没有 JSON 配方：%s' % path)
    imported = []
    for f in files:
        raw = json.loads(f.read_text(encoding='utf-8'))
        rec = recipe_mod.compile_recipe(raw)
        out = recipe_mod.save_version(raw, created_by='cli', activate=True)
        imported.append({'file': f.name, 'platform': rec.platform_id,
                         'version': out['version'],
                         'host': rec.meta.get('host')})
        print('已导入 %s → %s v%s' % (f.name, rec.platform_id, out['version']))
    _print(imported)


def cmd_platforms(args):
    rows = recipe_mod.list_platforms()
    _print(rows)


def _fetcher_for(pid):
    rec = recipe_mod.get_active(pid)
    if not rec:
        raise SystemExit('平台 %s 没有启用的配方，请先 import-recipes' % pid)
    return rec, Fetcher(rec)


def cmd_probe(args):
    rec, fetcher = _fetcher_for(args.platform)
    try:
        word = args.keyword or config.load_snapshot(rec.platform_id).sample_word()
        out = paginate.probe_size(rec, fetcher, word)
        out['platform'] = rec.platform_id
        out['keyword'] = word
        out['requests'] = fetcher.requests
        _print(out)
        if args.save and out.get('size_max'):
            paginate.apply_probe(rec, out)
            _print(recipe_mod.save_version(rec.raw, created_by='cli-probe', activate=True))
    finally:
        fetcher.close()


def cmd_verify(args):
    rec, fetcher = _fetcher_for(args.platform)
    try:
        word = args.keyword or config.load_snapshot(rec.platform_id).sample_word()
        p1 = paginate.fetch_first(rec, fetcher, word)
        p2 = None
        if rec.pagination.get('type') != 'none':
            for pg in paginate.iter_pages(rec, fetcher, word, max_pages=2):
                if pg.page > int(rec.pagination.get('start') or 1):
                    p2 = pg
                    break
        cfg = rec.validation_cfg()

        def cover(items, key):
            return round(sum(1 for x in items if str(x.get(key) or '').strip())
                         / (len(items) or 1), 3)

        checks = {
            'items_ge_1': len(p1.items) >= int(cfg.get('min_items_per_keyword') or 1),
            'page2_differs': (p2 is not None and bool(p2.items)) or
                             rec.pagination.get('type') == 'none',
            'title_cover': cover(p1.items, 'title') >= 1.0,
            'url_cover': cover(p1.items, 'url') >= 1.0,
        }
        for key, need in (cfg.get('expected_nonempty') or {}).items():
            checks['%s_cover' % key] = cover(p1.items, key) >= float(need)
        out = {'platform': rec.platform_id, 'keyword': word,
               'items_page1': len(p1.items), 'items_page2': len(p2.items) if p2 else 0,
               'page_size': p1.size, 'total': p1.total,
               'checks': checks, 'passed': all(checks.values()),
               'requests': fetcher.requests,
               'samples': p1.items[:5]}
        _print(out)
        if not out['passed']:
            sys.exit(2)
    finally:
        fetcher.close()


def cmd_detail(args):
    """单条详情链路调试：列表 → 详情 POST → 正文清洗 → AI 抽取 → 归一化。"""
    from . import detail_ai, recipe as rmod
    from .extract import extract_detail
    from .normalize import normalize_item
    from .paginate import fetch_first

    rec, fetcher = _fetcher_for(args.platform)
    if not rec.detail_enabled():
        raise SystemExit('平台 %s 未启用详情（detail.enabled=false）' % args.platform)
    word = args.keyword or config.load_snapshot(rec.platform_id).sample_word()
    try:
        page = fetch_first(rec, fetcher, word)
        items = page.items[:max(1, args.limit)]
        print('列表：关键词「%s」共 %s 条，取前 %d 条做详情'
              % (word, page.total, len(items)))
        for i, item in enumerate(items, 1):
            ctx = runner._detail_ctx(rec, item)
            res = fetcher.fetch_detail(item['url'], ctx=ctx)
            print('\n--- [%d/%d] %s' % (i, len(items), (item.get('title') or '')[:60]))
            print('  详情请求 HTTP %s，正文响应 %d 字节，id=%s'
                  % (res.status, len(res.text or ''), ctx.get('id')))
            if not res.ok:
                print('  详情请求失败：%s' % (res.error or '')[:200])
                continue
            text = detail_ai.content_from_result(rec, res)
            print('  清洗后正文 %d 字，前 100 字：%s' % (len(text), text[:100].replace('\n', ' ')))
            extra = extract_detail(rec, res, item, ai=not args.no_ai)
            norm = normalize_item(dict(item, **{k: v for k, v in extra.items()
                                                if not k.startswith('_')}),
                                  keep_url=bool((rec.normalize or {}).get('keep_url')),
                                  source_default=rec.meta.get('name') or rec.platform_id)
            print('  URL      :', norm['url'])
            for k in ('budget_yuan', 'fee_note', 'scope', 'doc_get_start',
                      'doc_get_end', 'open_at', 'status'):
                print('  %-11s: %s' % (k, norm.get(k)))
            d = norm.get('detail') or {}
            print('  detail   :', json.dumps({k: v for k, v in d.items() if k != 'ai'},
                                            ensure_ascii=False, default=str)[:600])
            print('  ai       :', json.dumps((extra.get('_ai_meta') or {}),
                                            ensure_ascii=False, default=str))
    finally:
        fetcher.close()


def cmd_run(args):
    words = args.keyword or None
    params = {'platform': args.platform, 'keywords': words,
              'dry_run': bool(args.dry_run), 'full': bool(args.full),
              'with_ai': not bool(args.no_ai)}
    if args.sync:
        job = {'id': 0, 'job_type': 'run', 'params': params}

        def progress(stage, pct, msg='', force=False):
            print('[%3d%%] %s' % (int(pct), msg))

        def cancel():
            return False

        out = runner.run_job(job, progress, cancel)
        _print(out)
        return
    jobs.db.init_schema()
    job, dedupe = jobs.submit('run', args.platform, params, args.user or 'cli')
    print('已提交任务 #%s（dedupe=%s），由 worker 执行；查看：'
          'python -m server.bidcrawl.cli job --id %s' % (job['id'], dedupe, job['id']))


def cmd_job(args):
    _print(jobs.get_job(args.id) or {'error': '任务不存在'})


def cmd_jobs(args):
    _print(jobs.list_jobs(args.status or ''))


def cmd_config(args):
    entity = args.entity
    table = {'keyword': 'bid_keyword', 'buyer': 'bid_buyer_pattern',
             'rule': 'bid_exclude_rule'}[entity]
    rows = db.q('SELECT * FROM %s WHERE deleted_at IS NULL ORDER BY id' % table)
    _print(rows)


def cmd_worker(args):
    from . import worker
    worker.main()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='bidcrawl', description='标讯采集（配方复用）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('init', help='建表 + 种子配置').set_defaults(func=cmd_init)

    p = sub.add_parser('import-recipes', help='导入 recipes/*.json 并启用')
    p.add_argument('--path', help='配方 JSON 文件或目录')
    p.set_defaults(func=cmd_import)

    sub.add_parser('platforms', help='平台与配方列表').set_defaults(func=cmd_platforms)

    p = sub.add_parser('probe-size', help='实测接口页宽上限')
    p.add_argument('--platform', required=True)
    p.add_argument('--keyword')
    p.add_argument('--save', action='store_true', help='把探测结果保存为新配方版本')
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser('verify', help='配方体检（第 1/2 页、字段覆盖率）')
    p.add_argument('--platform', required=True)
    p.add_argument('--keyword')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('run', help='执行一轮采集（同步或提交后台任务）')
    p.add_argument('--platform', help='平台 id，省略则跑所有 active/degraded')
    p.add_argument('--keyword', action='append', help='只跑指定关键词，可重复')
    p.add_argument('--dry-run', action='store_true', help='只打请求不写库')
    p.add_argument('--full', action='store_true')
    p.add_argument('--sync', action='store_true', help='本进程同步执行（联调用）')
    p.add_argument('--no-ai', action='store_true', help='跳过 AI 详情解析（只走确定性正则）')
    p.add_argument('--user', default='cli')
    p.set_defaults(func=cmd_run)

    p = sub.add_parser('detail', help='单条详情链路调试（列表→详情→AI 解析）')
    p.add_argument('--platform', required=True)
    p.add_argument('--keyword')
    p.add_argument('--limit', type=int, default=1, help='取前 N 条列表数据')
    p.add_argument('--no-ai', action='store_true', help='只跑确定性正则，不调模型')
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser('job', help='查看任务')
    p.add_argument('--id', type=int, required=True)
    p.set_defaults(func=cmd_job)

    p = sub.add_parser('jobs', help='任务列表')
    p.add_argument('--status', default='')
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser('config', help='查看配置')
    p.add_argument('--entity', choices=['keyword', 'buyer', 'rule'], default='keyword')
    p.set_defaults(func=cmd_config)

    sub.add_parser('worker', help='启动独立 worker').set_defaults(func=cmd_worker)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
