# -*- coding: utf-8 -*-
"""Historical 5-minute checks, not a replacement performance backtest:
python Stragety/RedisQMT/Trend100/sources/probe_historical_5m.py

Two serial public Eastmoney requests for 2026-08-06, stock/ETF, unadjusted.
Low-rate shared session, bounded timeouts, raw responses and hashes, no retry.
No Redis/QMT, subscriptions, credentials or orders. See ../AlternativeDataSources.md.
"""
import hashlib
import json
from pathlib import Path
import random
import time
from datetime import datetime, timezone

import requests


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'
EM_SESSION = requests.Session()
EM_SESSION.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
LAST_CALL = 0.0


def em_get(url, params):
    global LAST_CALL
    wait = 1.5 - (time.monotonic() - LAST_CALL)
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, timeout=(5, 15))
    finally:
        LAST_CALL = time.monotonic()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    summary_path = OUT / 'historical_5m_probe.json'
    if summary_path.exists():
        print(summary_path.read_text(encoding='utf-8'))
        return
    records = []
    for code in ('600000', '510300'):
        url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        params = {'secid': '1.' + code, 'klt': '5', 'fqt': '0', 'beg': '20260806',
                  'end': '20260806', 'lmt': '1000', 'fields1': 'f1,f2,f3,f4,f5,f6',
                  'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'}
        record = {'source': 'Eastmoney public 5-minute', 'symbol': code, 'url': url,
                  'params': params, 'retrieved_utc': datetime.now(timezone.utc).isoformat(),
                  'purpose': 'clock/cross-source diagnostics; not formal minute backtest'}
        try:
            response = em_get(url, params)
            raw = response.content
            (OUT / ('eastmoney_5m_' + code + '.raw')).write_bytes(raw)
            record.update(status=response.status_code, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            response.raise_for_status()
            data = response.json().get('data') or {}
            rows = data.get('klines') or []
            record.update(rows=len(rows), first=rows[0] if rows else None, last=rows[-1] if rows else None)
        except Exception as error:
            record['error_type'] = type(error).__name__
        records.append(record)
        # Stop on explicit provider rate/permission refusal, not an alternate host.
        if record.get('status') in (403, 429):
            break
    result = {'redis_calls': 0, 'orders': 0, 'formal_backtest': False, 'records': records}
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
