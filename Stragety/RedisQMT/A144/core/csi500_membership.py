# -*- coding: utf-8 -*-
"""Build a short, dated CSI500 membership window from official CSI sources.

The current XLS is an anchor, not a historical universe. Reverse/forward
official sample changes in the required window; never backdate today's list.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from .dual_regime_execution import atomic_save


CURRENT_URL = 'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/000905cons.xls'
NOTICE_URL = 'https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementByVo'
DETAIL_URL = 'https://www.csindex.com.cn/csindex-home/announcement/queryAnnouncementById'


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def fetch(url, directory, payload=None):
    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in ('www.csindex.com.cn', 'oss-ch.csindex.com.cn'):
        raise ValueError('Unexpected membership source: ' + url)
    encoded = quote(url, safe=':/?=&%')
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request(encoded, data=body, headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        raw = response.read()
    digest = hashlib.sha256(raw).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlsplit(url).path).suffix or '.json'
    name = digest + suffix
    (directory / name).write_bytes(raw)
    proof = dict(url=url, payload=payload, sha256=digest, file=name,
                 retrieved_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    return raw, proof


def six_digits(value):
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    if not text.isdigit() or len(text) > 6:
        raise ValueError('Invalid constituent/index code: ' + text)
    return text.zfill(6)


def security(value):
    code = six_digits(value)
    if not code.startswith(('0', '3', '6')):
        raise ValueError('Unexpected CSI500 security: ' + code)
    return code + ('.SH' if code.startswith('6') else '.SZ')


def parse_anchor(raw):
    frame = pd.read_excel(io.BytesIO(raw), dtype=str)
    if frame.shape != (500, 9) or not frame.iloc[:, 1].map(six_digits).eq('000905').all():
        raise ValueError('Official CSI500 anchor must have 500 rows and index 000905')
    dates = frame.iloc[:, 0].str.replace('-', '', regex=False).str[:8].unique()
    if len(dates) != 1:
        raise ValueError('Mixed official anchor dates')
    datetime.strptime(dates[0], '%Y%m%d')
    codes = [security(value) for value in frame.iloc[:, 4]]
    if len(set(codes)) != 500:
        raise ValueError('Duplicate official constituents')
    exchanges = frame.iloc[:, 8].tolist()
    if any(exchange != ('Shanghai Stock Exchange' if code.endswith('.SH') else 'Shenzhen Stock Exchange')
           for code, exchange in zip(codes, exchanges)):
        raise ValueError('Official security/exchange mismatch')
    return str(dates[0]), set(codes)


def parse_change(raw):
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, dtype=str)
    result = {}
    for name, label in (('调入', 'incoming'), ('调出', 'outgoing')):
        if name not in sheets or sheets[name].shape[1] < 3:
            raise ValueError('Unsupported official adjustment workbook')
        frame = sheets[name]
        selected = frame.loc[frame.iloc[:, 0].map(six_digits).eq('000905')]
        result[label] = {security(value) for value in selected.iloc[:, 2]}
    if not result['incoming'] or len(result['incoming']) != len(result['outgoing']) or result['incoming'] & result['outgoing']:
        raise ValueError('Invalid CSI500 incoming/outgoing adjustment')
    return result


def reconstruct(anchor_date, anchor, changes, dates, today):
    earliest = min(dates)
    if anchor_date < max(date for date in dates if date < today) or anchor_date > today:
        raise ValueError('Official snapshot is stale or future-dated')
    current = set(anchor)
    snapshots = {}
    for change in sorted(changes, key=lambda item: item['effective'], reverse=True):
        if earliest < change['effective'] <= anchor_date:
            incoming = set(change['incoming'])
            outgoing = set(change['outgoing'])
            if not incoming <= current or outgoing & current:
                raise ValueError('Official reverse adjustment conflicts with anchor')
            snapshots.setdefault(change['effective'], sorted(current))
            current = (current - incoming) | outgoing
    snapshots[earliest] = sorted(current)
    current = set(anchor)
    for change in sorted(changes, key=lambda item: item['effective']):
        if anchor_date < change['effective'] <= today:
            incoming = set(change['incoming'])
            outgoing = set(change['outgoing'])
            if not outgoing <= current or incoming & current:
                raise ValueError('Official forward adjustment conflicts with anchor')
            current = (current - outgoing) | incoming
            snapshots[change['effective']] = sorted(current)
    if any(len(codes) != 500 for codes in snapshots.values()):
        raise ValueError('Reconstruction changed index size')
    return dict(sorted(snapshots.items()))


def prepare(path, dates, today, downloader=fetch):
    dates = sorted(set(dates))
    if today not in dates or len(dates) < 7:
        raise ValueError('Need current plus six confirmed trading dates')
    folder = Path(path).parent / 'membership_sources'
    sources = []
    raw, proof = downloader(CURRENT_URL, folder)
    sources.append(proof)
    anchor_date, anchor = parse_anchor(raw)
    start = (datetime.strptime(dates[0], '%Y%m%d') - timedelta(days=45)).strftime('%Y-%m-%d')
    end = datetime.strptime(today, '%Y%m%d').strftime('%Y-%m-%d')
    notices = []
    page = 1
    while True:
        payload = dict(lang='cn', startDate=start, endDate=end, classList=[], indexList=[],
                       relatedTopics=[], typeList=[], page=dict(key='', page=page, rows=100))
        raw, proof = downloader(NOTICE_URL, folder, payload)
        sources.append(proof)
        data = json.loads(raw)
        rows = data.get('data')
        if str(data.get('code')) != '200' or not isinstance(rows, list):
            raise ValueError('Official announcement query failed')
        notices.extend(rows)
        if len(notices) >= int(data['total']):
            break
        if not rows:
            raise ValueError('Incomplete official announcement pagination')
        page += 1
    if len(notices) != int(data['total']) or len({item['id'] for item in notices}) != len(notices):
        raise ValueError('Incomplete or duplicate official announcements')
    changes = []
    for notice in notices:
        if not any(word in notice['title'] for word in ('调整', '样本', '终止', '合并')):
            continue
        raw, proof = downloader(DETAIL_URL + '?id=' + str(notice['id']), folder)
        sources.append(proof)
        detail = json.loads(raw)
        if str(detail.get('code')) != '200' or detail['data']['id'] != notice['id']:
            raise ValueError('Official notice identity mismatch')
        detail = detail['data']
        parser = PlainText()
        parser.feed(detail['content'])
        text = re.sub(r'\s+', '', ''.join(parser.parts))
        if '中证500' not in text or '样本' not in text:
            continue
        match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日收市后生效', text)
        if not match:
            raise ValueError('Cannot establish CSI500 effective date: ' + str(notice['id']))
        close_date = ''.join((match[1], match[2].zfill(2), match[3].zfill(2)))
        if close_date < dates[0] or close_date >= today:
            continue
        effective = next(date for date in dates if date > close_date)
        files = [item for item in detail['enclosureList'] if item['fileUrl'].lower().endswith('.xlsx')]
        if len(files) != 1:
            raise ValueError('Adjustment needs a verified manual import (unsupported attachment): ' + str(notice['id']))
        raw, proof = downloader(files[0]['fileUrl'], folder)
        sources.append(proof)
        change = parse_change(raw)
        changes.append(dict(id=notice['id'], effective=effective, after_close=close_date,
                            incoming=sorted(change['incoming']), outgoing=sorted(change['outgoing'])))
    snapshots = reconstruct(anchor_date, anchor, changes, dates, today)
    result = dict(source='CSI official dated anchor and announcement adjustments', valid_through=today,
                  snapshots=snapshots, anchor_date=anchor_date, checked_from=dates[0],
                  announcement_search_start=start, changes=changes, sources=sources,
                  fetched_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                  limitation='Bounded announcement search; current anchor is not used as a past universe. Unsupported changes fail closed.')
    atomic_save(path, result)
    return result


def ensure(path, xtdata, today, lookback=5, auto_refresh=False):
    path = Path(path)
    if path.is_file():
        current = json.loads(path.read_text(encoding='utf-8'))
        if not auto_refresh or current.get('valid_through', '') >= today:
            return
    calendar = xtdata.get_trading_dates('SH', end_time=today, count=max(7, lookback + 2))
    dates = []
    for value in calendar or []:
        text = str(value)
        if len(text) == 8 and text.isdigit():
            dates.append(text)
        elif isinstance(value, (int, float)):
            dates.append(pd.Timestamp(value, unit='ms', tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y%m%d'))
        else:
            raise ValueError('Unsupported trading calendar timestamp')
    prepare(path, dates, today)
