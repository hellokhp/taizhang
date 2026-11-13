# -*- coding: utf-8 -*-
"""把 SummaryResult 标准化写入 PG 的 ai_* 表（替代 exporter.save(Excel)）。

设计要点：
- 类型化核心列 + ai_field_evidence 逐格溯源，二者分工；金额一律「元」。
- 覆盖 AI 工具四种模式：opening_table(主力) / bidder_folder / shortlist / discount。
- raw_result 只存结构化快照，绝不把整篇 markdown 塞进 PG（体积与合规）。
- 单任务一个短事务：先删旧结果（重跑场景），再全量写，最后 commit。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import db

log = logging.getLogger('taizhang.ai.worker.writer')

MONEY_KEYS = {'budget', 'bid_price', 'deposit', 'incl_total', 'total_net',
              'price_in_words', 'win_amount'}
NUM_KEYS = {'price_score', 'rank', 'deviation', 'tax_rate', 'seq', 'confidence'}


def _s(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _text(v):
    if v is None:
        return None
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _num(v):
    """金额/数值列：只接受能安全转 float 的值，否则返回 None。"""
    if v is None or v == '':
        return None
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, str):
            v = v.replace(',', '').replace('元', '').strip()
            if not v:
                return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fv(fv):
    """FieldValue -> (value_text, value_num, confidence, level, quote, candidates, conflict, note, manual)"""
    if fv is None:
        return (None, None, 0.0, 'red', None, [], 0, None, 0)
    val = getattr(fv, 'value', None)
    text = _text(val)
    num = _num(val) if (getattr(fv, 'key', '') in MONEY_KEYS or isinstance(val, (int, float))) else None
    conf = _num(getattr(fv, 'confidence', 0)) or 0.0
    try:
        level = fv.level
    except Exception:
        level = 'green' if conf >= 0.85 else ('yellow' if conf >= 0.60 else 'red')
    quote = _text(getattr(fv, 'quote', ''))
    if quote:
        quote = quote[:200]                       # 合规：证据片段 ≤200 字
    cands = getattr(fv, 'candidates', None) or []
    return (text, num, conf, level, quote,
            json.dumps(cands, ensure_ascii=False, default=str),
            1 if getattr(fv, 'conflict', False) else 0,
            _text(getattr(fv, 'note', '')),
            1 if getattr(fv, 'manual', False) else 0)


def _jsonable(o, depth=0):
    """把 dataclass/Path 递归转成可 JSON 序列化对象；跳过超长 markdown 字段。"""
    if depth > 6:
        return None
    if o is None or isinstance(o, (str, int, float, bool)):
        if isinstance(o, str) and len(o) > 2000:
            return o[:2000] + '…[截断]'
        return o
    if isinstance(o, (datetime, date, Decimal, Path)):
        return _text(o)
    if isinstance(o, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in o.items()
                if k not in ('markdown', 'normalized_md')}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(o)[:200]]
    if is_dataclass(o) and not isinstance(o, type):
        return {k: _jsonable(v, depth + 1) for k, v in asdict(o).items()
                if k not in ('markdown', 'normalized_md', 'path')}
    return str(o)


def _project_value(result, key, default=None):
    fv = (result.project or {}).get(key)
    v = getattr(fv, 'value', None) if fv is not None else None
    return default if v in (None, '') else v


def write(task, result, prepared, skipped, prompt_version=''):
    """写入一个任务的全部结果，返回 summary_id。

    task: ai_summary_task 的一行（dict）
    result: bidsummary.models.SummaryResult
    prepared/skipped: materialize 返回的物化清单
    """
    task_id = int(task['id'])
    rid = task['rid']
    now = time.time()
    mode = getattr(result, 'mode', 'bidder_folder')

    pj = result.project or {}
    warnings = list(getattr(result, 'warnings', None) or [])
    failed = list(getattr(result, 'failed_files', None) or [])
    excluded = list(getattr(result, 'excluded_files', None) or [])

    # 0 个可解析开标表时不静默出空表：明确提示（v1 只取招标文件）
    bidder_count = _count_bidders(result)
    if bidder_count == 0:
        warnings.append('检测到 0 个可解析开标表，请确认开标一览表是否放在「招标文件」目录')

    # ---- 汇总头 ----
    cur = db.x(
        'INSERT INTO ai_summary(task_id, rid, mode, status, project_name, project_code, '
        'tenderer, agency, open_time_raw, open_place, budget, eval_method, bidder_count, '
        'warnings, raw_result, prompt_version, created_at) '
        "VALUES(%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (task_id, rid, mode,
         _text(_project_value(result, 'project_name')),
         _text(_project_value(result, 'project_code')),
         _text(_project_value(result, 'tenderer')),
         _text(_project_value(result, 'agency')),
         _text(_project_value(result, 'open_time')),
         _text(_project_value(result, 'open_place')),
         _num(_project_value(result, 'budget')),
         _text(_project_value(result, 'eval_method')),
         bidder_count,
         json.dumps(warnings, ensure_ascii=False, default=str),
         json.dumps(_jsonable(result), ensure_ascii=False),
         prompt_version or '', now))
    summary_id = int(cur.fetchone()['id'])

    # ---- 项目级字段证据 ----
    for key, fv in pj.items():
        vt, vn, conf, lv, quote, cands, conflict, note, manual = _fv(fv)
        sf = _text(getattr(fv, 'source_file', '')) or ''
        sp = _text(getattr(fv, 'source_page', '')) or ''
        db.x('INSERT INTO ai_field_evidence(summary_id, scope, field_key, field_label, '
             'value_text, value_num, confidence, level, source_file, source_page, '
             'source_quote, candidates, conflict, manual, note, created_at) '
             'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
             (summary_id, 'PROJECT', key, _text(getattr(fv, 'label', '')) or key,
              vt, vn, conf, lv, sf, sp, quote, cands, conflict, manual, note, now))

    # ---- 投标人行 ----
    for seq, item in enumerate(_iter_bidders(result), start=1):
        name, fields, flags, src = item
        bid_price = _bid_price(fields, result, seq)
        duration = _field_text(fields, 'duration')
        cur = db.x(
            'INSERT INTO ai_bidder(summary_id, seq, bidder_name, bid_price, duration, '
            'qualification, reject_reason, remark, row_status, flags, source_node_id, '
            'source_sheet, source_row, price_score, rank, deviation, tax_rate, created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (summary_id, seq, name or '（未识别投标人）', bid_price, duration,
             _field_text(fields, 'qualification'), _field_text(fields, 'reject_reason'),
             _field_text(fields, 'remark'), '正常',
             json.dumps(flags or [], ensure_ascii=False, default=str),
             _text(src.get('node_id')), _text(src.get('sheet')), src.get('row'),
             _num(src.get('price_score')), _num(src.get('rank')),
             _num(src.get('deviation')), _num(src.get('tax_rate')), now))
        bidder_id = int(cur.fetchone()['id'])
        for key, fv in (fields or {}).items():
            vt, vn, conf, lv, quote, cands, conflict, note, manual = _fv(fv)
            db.x('INSERT INTO ai_field_evidence(summary_id, bidder_id, scope, field_key, '
                 'field_label, value_text, value_num, confidence, level, source_file, '
                 'source_page, source_quote, candidates, conflict, manual, note, created_at) '
                 'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                 (summary_id, bidder_id, 'BIDDER', key,
                  _text(getattr(fv, 'label', '')) or key, vt, vn, conf, lv,
                  _text(getattr(fv, 'source_file', '')) or '',
                  _text(getattr(fv, 'source_page', '')) or '', quote,
                  cands, conflict, manual, note, now))

    # ---- 文件清单与解析状态 ----
    parsed_by_rel = {}
    for f in (getattr(result, 'files', None) or []):
        parsed_by_rel[str(getattr(f, 'rel', ''))] = f
    failed_map = {str(x.get('file')): str(x.get('reason') or '') for x in failed}
    for n in prepared:
        rel = n.get('rel_path')
        f = parsed_by_rel.get(rel) or parsed_by_rel.get(str(n.get('name')))
        status = 'PARSED' if (f is not None and getattr(f, 'status', '') == 'PARSED') else 'NEW'
        err = failed_map.get(rel) or failed_map.get(str(n.get('name'))) or ''
        if err:
            status = 'FAILED'
        md = str(getattr(f, 'markdown', '') or '') if f is not None else ''
        db.x('INSERT INTO ai_file(task_id, node_id, rel_path, name, ext, size, sha256, '
             'kind, bidder_guess, page_count, is_scanned, status, error_msg, '
             'normalized_md, created_at) '
             'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
             (task_id, _text(n.get('node_id')), rel, _text(n.get('name')),
              _text(n.get('ext')), int(n.get('size') or 0), n.get('sha256'),
              _text(getattr(f, 'kind', '') or '') if f else '',
              _text(getattr(f, 'bidder', '') or '') if f else '',
              int(getattr(f, 'page_count', 0) or 0) if f else 0,
              1 if (f is not None and getattr(f, 'is_scanned', False)) else 0,
              status, err[:500], md[:20000] if md else None, now))
    for n in skipped:
        db.x('INSERT INTO ai_file(task_id, node_id, rel_path, name, ext, size, sha256, '
             "status, error_msg, created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'SKIPPED',%s,%s)",
             (task_id, _text(n.get('node_id')), n.get('rel_path'), _text(n.get('name')),
              _text(n.get('ext')), int(n.get('size') or 0), n.get('sha256'),
              str(n.get('reason') or '')[:500], now))

    # ---- 任务收尾：summary_id / token / 状态 ----
    tok = getattr(result, 'tokens', None) or {}
    parsed_n = sum(1 for f in (getattr(result, 'files', None) or [])
                   if getattr(f, 'status', '') == 'PARSED')
    failed_n = len(failed) + len(skipped)
    status = 'PARTIAL' if (failed_n or bidder_count == 0 or parsed_n == 0) else 'DONE'
    db.x('UPDATE ai_summary_task SET summary_id=%s, status=%s, stage=%s, stage_label=%s, '
         'progress=100, parsed_files=%s, failed_files=%s, skipped_files=%s, '
         'tokens_in=%s, tokens_out=%s, error_msg=%s, finished_at=%s, updated_at=%s WHERE id=%s',
         (summary_id, status, 'DONE', '完成', parsed_n, failed_n, len(skipped),
          int(tok.get('in') or 0), int(tok.get('out') or 0),
          '; '.join(warnings)[:1000] if warnings else None, now, now, task_id))
    db.commit()
    log.info('[ai] task %s 写入完成：summary=%s，投标人 %d 家，文件 %d 解析 / %d 失败，状态 %s',
             task_id, summary_id, bidder_count, parsed_n, failed_n, status)
    return summary_id, status


# ------------------------------------------------------------------ 模式适配
def _count_bidders(result):
    if getattr(result, 'lots', None):
        return sum(len(l.rows or []) for l in result.lots)
    if getattr(result, 'rows', None):
        return len(result.rows)
    if getattr(result, 'shortlist', None):
        return len(result.shortlist.bidders or [])
    if getattr(result, 'discount', None):
        return len(result.discount.bidders or [])
    return 0


def _field_text(fields, key):
    fv = (fields or {}).get(key)
    return _text(getattr(fv, 'value', None)) if fv is not None else None


def _bid_price(fields, result, seq):
    """金额一律「元」；各模式的报价来源不同。"""
    v = _field_text(fields, 'bid_price')
    n = _num(v)
    if n is not None:
        return n
    if getattr(result, 'lots', None):
        flat = [r for l in result.lots for r in (l.rows or [])]
        if 0 < seq <= len(flat):
            return _num(flat[seq - 1].total)
    if getattr(result, 'discount', None):
        bs = result.discount.bidders or []
        if 0 < seq <= len(bs):
            return _num(bs[seq - 1].est_total)
    return None


def _iter_bidders(result):
    """统一产出 (name, fields:dict, flags:list, src:dict)。

    opening_table 的 BidRow 不是 FieldValue 结构，要在这里补成等价的字段字典，
    保证前端三色表格、证据表在四种模式下都能工作。
    """
    from bidsummary.models import FieldValue

    # 模式 B：开标记录表（主力）
    if getattr(result, 'lots', None):
        for lot in result.lots:
            for r in (lot.rows or []):
                fields = {}
                for key, val in (('bid_price', r.total), ('incl_total', r.incl_total),
                                 ('total_net', r.total_net), ('duration', r.duration),
                                 ('tax_rate', r.tax_rate), ('price_score', r.price_score),
                                 ('rank', r.rank), ('deviation', r.deviation),
                                 ('row_status', r.status)):
                    fields[key] = FieldValue(
                        key=key, label=key, value=val,
                        confidence=0.95 if val is not None else 0.0,
                        source_file=lot.source_file, source_page=str(lot.sheet or ''),
                        quote='%s · 第 %d 行' % (lot.lot_name or lot.sheet or '', (r.src_row or 0) + 1))
                yield (r.name, fields, list(r.flags or []),
                       {'sheet': lot.sheet, 'row': r.src_row,
                        'price_score': r.price_score, 'rank': r.rank,
                        'deviation': r.deviation, 'tax_rate': r.tax_rate})
        return

    # 模式 A：投标人文件夹
    if getattr(result, 'rows', None):
        for row in result.rows:
            yield (row.bidder, dict(row.fields or {}), list(row.flags or []),
                   {'sheet': '', 'row': 0})
        return

    # 模式 C / E：入围单价矩阵（aggregate / per_item）
    if getattr(result, 'shortlist', None):
        sl = result.shortlist
        variant = getattr(sl, 'variant', 'aggregate') or 'aggregate'
        for b in (sl.bidders or []):
            # 评标价 = 各类加权评标价之和（元），绝不能把价格分当金额写进 bid_price。
            # 旧版本曾把 b.total_score（分值 0~50）写成「评标价（元）」，是金额口径污染，
            # 此处按维度重新取数；缺类/缺项时留 NULL 并在 flags 里说明。
            eval_sum = sum(v for v in (b.eval_prices or []) if isinstance(v, (int, float)))
            eval_value = round(eval_sum, 2) if (b.eval_prices and all(
                isinstance(v, (int, float)) for v in b.eval_prices)) else None
            raw_sum = sum(v for v in (b.raw_sums or []) if isinstance(v, (int, float)))
            fields = {
                'bid_price': FieldValue(
                    key='bid_price', label='加权评标价合计（元）', value=eval_value,
                    confidence=0.9, source_file=b.source_file or sl.price_sheet_file,
                    quote='各类 ∑(投标单价×权重) 之和；缺报/超限价类不计'),
                'raw_sum': FieldValue(
                    key='raw_sum', label='未加权单价合计（元）', value=round(raw_sum, 2) if b.raw_sums else None,
                    confidence=0.9, source_file=b.source_file or sl.price_sheet_file),
            }
            if b.total_score is not None:
                fields['price_score'] = FieldValue(
                    key='price_score', label='价格分', value=b.total_score, confidence=0.9,
                    source_file=sl.rule_source_file or b.source_file,
                    quote='确定性代码计算（' + str(variant) + '）：' +
                          (sl.formula or sl.eval_price_rule or ''))
            flags = list(b.flags or [])
            if eval_value is None:
                flags.append('加权评标价不完整（缺报或超限价），金额列留空')
            yield (b.name, fields, flags,
                   {'sheet': '', 'row': 0, 'rank': b.rank,
                    # price_score 必须进 src：ai_bidder 类型化列从这里取，
                    # 只写 evidence 会让「标准字段」视图的价格分恒为 NULL。
                    'price_score': b.total_score,
                    'deviation': None, 'tax_rate': None})
        return

    # 模式 D：下浮率/折扣报价
    if getattr(result, 'discount', None):
        for b in (result.discount.bidders or []):
            fields = {
                'bid_price': FieldValue(key='bid_price', label='结算估算（元）',
                                        value=b.est_total, confidence=0.8,
                                        source_file=result.discount.opening_file),
                'remark': FieldValue(key='remark', label='报价口径',
                                     value='%s %.4g%%' % (
                                         '折扣率' if result.discount.rate_kind == 'discount_rate'
                                         else '下浮率', b.rate), confidence=0.9,
                                     source_file=result.discount.opening_file),
            }
            if b.price_score is not None:
                fields['price_score'] = FieldValue(
                    key='price_score', label='价格分', value=b.price_score, confidence=0.9)
            # price_score 必须进 src：ai_bidder 的类型化列从这里取，
            # 只写 evidence 会让「标准字段」视图的价格分恒为 NULL。
            yield (b.name, fields, list(b.flags or []),
                   {'sheet': '', 'row': 0, 'rank': b.rank, 'deviation': b.deviation,
                    'price_score': b.price_score, 'tax_rate': None})
