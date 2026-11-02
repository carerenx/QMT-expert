# -*- coding: utf-8 -*-
"""Archive CSI announcements: python Stragety/RedisQMT/CSI1000Independent/fetch_membership.py

Fetches dated membership evidence only; does not trade or fetch price history.
See ResearchProtocol.md for the historical-universe validation requirements.
"""
import hashlib
import json
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/analysis/csi1000_independent_20261027/membership'
BASE = 'https://www.csindex.com.cn/csindex-home'


def main():
    raise SystemExit('Disabled: user requires Redis QMT as the sole data source (2026-10-27).')
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({'Referer': 'https://www.csindex.com.cn/',
                            'User-Agent': 'Mozilla/5.0'})
    notices = {}
    for keyword in ('中证1000', '1000', '临时调整'):
        page = 1
        while True:
            path = OUT / f'query_{keyword}_{page}.json'
            if path.exists():
                record = json.loads(path.read_text(encoding='utf-8'))
            else:
                payload = {'lang': 'cn', 'searchInput': keyword,
                           'startDate': '2025-05-01', 'endDate': '2026-10-27',
                           'page': {'key': '', 'page': page, 'rows': 40},
                           'classList': [], 'indexList': [], 'relatedTopics': [], 'typeList': []}
                response = session.post(BASE + '/announcement/queryAnnouncementByVo',
                                        json=payload, timeout=25)
                response.raise_for_status()
                record = {'request': payload, 'response': response.json(),
                          'retrieved': '2026-10-27', 'source': response.url}
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            body = record['response']
            rows = body.get('data', [])
            if not isinstance(rows, list):
                raise ValueError(f'Unexpected announcement response: {body}')
            for item in rows:
                notices[item['id']] = item
            print(keyword, page, 'rows', len(rows), 'total', body.get('total'), flush=True)
            if len(rows) < 40:
                break
            page += 1
    manifest = []
    error_path = OUT / 'notice_errors.json'
    errors = json.loads(error_path.read_text(encoding='utf-8')) if error_path.exists() else {}
    for notice_id, item in sorted(notices.items()):
        if '调整' not in item['title']:
            continue
        path = OUT / f'notice_{notice_id}.json'
        if not path.exists():
            if str(notice_id) in errors:
                print('Previously unavailable notice', notice_id, flush=True)
                continue
            response = session.get(BASE + '/announcement/queryAnnouncementById',
                                   params={'id': notice_id, 'lang': 'cn'}, timeout=25)
            if not response.ok:
                errors[str(notice_id)] = {'source': response.url, 'status': response.status_code,
                                          'retrieved': '2026-10-27', 'title': item['title']}
                error_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
                print('Unavailable notice', notice_id, response.status_code, flush=True)
                continue
            path.write_text(response.text, encoding='utf-8')
        body = json.loads(path.read_text(encoding='utf-8'))['data']
        # Broader temporary-adjustment search can reveal notices whose title
        # names another index; archive their bodies before checking relevance.
        plain = body['content'].replace(' ', '')
        if '1000' not in plain and '1000' not in body['title']:
            continue
        print(notice_id, body['publishDate'], body['title'], flush=True)
        for attachment in body.get('enclosureList', []):
            url = attachment['fileUrl']
            suffix = Path(url).suffix.lower()
            destination = OUT / f'attachment_{notice_id}_{attachment["id"]}{suffix}'
            if not destination.exists():
                response = session.get(url, timeout=25)
                response.raise_for_status()
                destination.write_bytes(response.content)
            manifest.append({'notice_id': notice_id, 'title': body['title'],
                             'published': body['publishDate'], 'source': url,
                             'retrieved': '2026-10-27', 'file': destination.name,
                             'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
