# -*- coding: utf-8 -*-
"""Reconstruct membership: python Stragety/RedisQMT/CSI1000Independent/reconstruct_membership.py

Uses archived official attachments and dated anchor; checks PDF columns independently.
This is a membership check, not a price or performance certification.
"""
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import pymupdf


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/analysis/csi1000_independent_20261027'
SOURCE = OUT / 'membership'
# First trading dates from the published after-close effective dates.
EVENTS = ((3006000, '20251212', '20251215'),
          (3006027, '20260109', '20260112'),
          (3006041, '20260204', '20260205'),
          (3006120, '20260508', '20260511'),
          (3006137, '20260612', '20260615'),
          (3006261, '20261009', '20261012'))


def normalize(code):
    code = str(code).zfill(6)
    return code + ('.SH' if code.startswith('6') else '.SZ')


def mainboard(code):
    return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003'))


def changes(path):
    if path.suffix != '.pdf':
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        result = {}
        for sheet, label in (('调出', 'outgoing'), ('调入', 'incoming')):
            frame = sheets[sheet]
            selected = frame.iloc[:, 0].str.zfill(6).eq('000852')
            result[label] = sorted(normalize(code) for code in frame.loc[selected].iloc[:, 2])
        return result
    with pymupdf.open(path) as doc:
        text = '\n'.join(page.get_text() for page in doc)
        start = re.search(r'中证\s*1000\s*指数样本调整名单', text)
        end = re.search(r'中证\s*A50\s*指数样本调整名单', text)
        if not start or not end or start.end() >= end.start():
            raise ValueError(f'Missing PDF section boundaries: {path}')
        codes = re.findall(r'\b\d{6}\b', text[start.end():end.start()])
        if len(codes) != 200:
            raise ValueError(f'Expected 100 paired changes, got {len(codes)}: {path}')
        result = {'outgoing': sorted(map(normalize, codes[::2])),
                  'incoming': sorted(map(normalize, codes[1::2]))}
        # Independently assign numeric words to left/right physical columns.
        active = False
        geometric = {'outgoing': [], 'incoming': []}
        for page in doc:
            lines = []
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    content = ''.join(span['text'] for span in line['spans'])
                    lines.append((line['bbox'][1], content))
            headings = [(y, content) for y, content in lines
                        if re.search(r'中证\s*(1000|A50)\s*指数样本调整名单', content)]
            lower = 0.0
            upper = page.rect.height
            for y, content in headings:
                if '1000' in content:
                    active = True
                    lower = y
                elif active:
                    upper = y
            if active:
                for word in page.get_text('words'):
                    if lower < word[1] < upper and re.fullmatch(r'\d{6}', word[4]):
                        label = 'outgoing' if word[0] < page.rect.width / 2 else 'incoming'
                        geometric[label].append(normalize(word[4]))
            if upper < page.rect.height:
                break
        if {key: sorted(value) for key, value in geometric.items()} != result:
            raise ValueError(f'PDF text/physical-column mismatch: {path}')
        return result


def main():
    anchor = json.loads((OUT / 'official_comparison.json').read_text(encoding='utf-8'))
    if anchor['snapshot_date'] != '20261026' or len(set(anchor['codes'])) != 1000:
        raise ValueError('Unexpected official anchor')
    current = set(anchor['codes'])
    snapshots = {'20261012': sorted(current)}
    records = []
    for notice_id, effective, first_trading in reversed(EVENTS):
        files = list(SOURCE.glob(f'attachment_{notice_id}_*'))
        if len(files) != 1:
            raise ValueError(f'Expected one attachment for {notice_id}')
        path = files[0]
        change = changes(path)
        incoming = set(change['incoming'])
        outgoing = set(change['outgoing'])
        if not incoming or len(incoming) != len(outgoing) or incoming & outgoing:
            raise ValueError(f'Invalid changes: {notice_id}')
        if not incoming <= current or outgoing & current:
            raise ValueError(f'Anchor chain inconsistent at {notice_id}')
        after = sorted(current)
        current = (current - incoming) | outgoing
        if len(current) != 1000:
            raise ValueError(f'Universe not 1000 at {notice_id}')
        notice = json.loads((SOURCE / f'notice_{notice_id}.json').read_text(encoding='utf-8'))['data']
        records.append({'id': notice_id, 'published': notice['publishDate'],
                        'after_close': effective, 'first_trading_date': first_trading,
                        'attachment': path.name,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'after': after, 'before': sorted(current), **change})
    # Each event's before-set is valid back to the previous event's first trading date.
    chronological = sorted(records, key=lambda item: item['first_trading_date'])
    snapshots['20251009'] = chronological[0]['before']
    for event in chronological:
        snapshots[event['first_trading_date']] = event['after']
    annual = {date: codes for date, codes in sorted(snapshots.items()) if date <= '20261009'}
    union = sorted({code for codes in annual.values() for code in codes if mainboard(code)})
    result = {'source': 'CSI official anchor and announcement attachments',
              'anchor_date': anchor['snapshot_date'], 'reconstructed_on': '2026-10-27',
              'status': 'Chain and PDF geometry consistent; independent historical anchor required',
              'snapshots': dict(sorted(snapshots.items())), 'events': chronological,
              'annual_mainboard_union': union,
              'mainboard_counts': {date: sum(map(mainboard, codes)) for date, codes in annual.items()},
              'limitations': ['Keyword query coverage is not proof that every temporary event is captured',
                              'ST history, prices and company actions are not certified here']}
    (OUT / 'reconstructed_membership.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'counts': result['mainboard_counts'], 'union': len(union),
                      'events': len(records), 'status': result['status']}))


if __name__ == '__main__':
    main()
