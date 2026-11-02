# -*- coding: utf-8 -*-
"""Check two pinned free historical samples from repository root:
python Stragety/RedisQMT/Trend100/sources/sample_hf.py

Requires probe_public.py's Tencent daily raw responses for independent comparison.
Downloads only 600000/510300 canonical files (40MB bound each), never the full market.
Not Redis/QMT, not a strategy backtest. See ../AlternativeDataSources.md.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'
SAMPLES = (
    ('hf_stock', 'sh600000', 'neigezhu/china-a-share-1min-ohlcv',
     'ba589a11534825044fe5a6b84838f50ba8d8d188', 'data/stock_1m/SH/600000.parquet'),
    ('hf_etf', 'sh510300', 'neigezhu/china-etf-1min-ohlcv',
     '4b7e48c1e8265cc7d25baee55879dd5bbb24f74d', 'data/etf_1m/SH/510300.parquet'),
)


def obtain_sample(kind, dataset, revision, filename):
    target = OUT / (kind + '_sample.parquet')
    record_path = OUT / (kind + '_sample_record.json')
    if target.exists():
        if not record_path.exists():
            raise ValueError('Existing file lacks a source/hash record: ' + kind)
        record = json.loads(record_path.read_text(encoding='utf-8'))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if (record.get('sha256') != digest or record.get('revision') != revision
                or record.get('dataset') != dataset or record.get('file') != filename):
            raise ValueError('Existing sample does not match pinned provenance: ' + kind)
        return target
    url = 'https://huggingface.co/datasets/' + dataset + '/resolve/' + revision + '/' + filename
    response = requests.get(url, timeout=(5, 35), stream=True)
    response.raise_for_status()
    size = 0
    with target.open('wb') as stream:
        for chunk in response.iter_content(1024 * 1024):
            size += len(chunk)
            if size > 40_000_000:
                raise ValueError('Sample exceeds 40MB bound')
            stream.write(chunk)
    record = {'dataset': dataset, 'revision': revision, 'file': filename,
              'bytes': size, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
              'not_redis_qmt': True, 'certified_for_backtest': False}
    record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    return target


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    checks = {}
    for kind, symbol, dataset, revision, filename in SAMPLES:
        path = obtain_sample(kind, dataset, revision, filename)
        frame = pd.read_parquet(path, filters=[('timestamp', '>=', datetime(2025, 8, 7)),
                                              ('timestamp', '<', datetime(2026, 8, 7))])
        frame = frame.set_index('timestamp').sort_index()
        frame.to_parquet(OUT / (kind + '_annual_subset_20250807_20260806.parquet'))
        counts = frame.groupby(frame.index.date).size()
        prices = frame[['open', 'high', 'low', 'close']]
        valid = frame.high.ge(frame[['open', 'low', 'close']].max(axis=1))
        valid &= frame.low.le(frame[['open', 'high', 'close']].min(axis=1))
        daily = frame.groupby(frame.index.date).agg(open=('open', 'first'), high=('high', 'max'),
            low=('low', 'min'), close=('close', 'last'), volume=('volume', 'sum'), amount=('turnover', 'sum'))
        raw = json.loads((OUT / ('tencent_daily_' + symbol + '.raw')).read_text(encoding='utf-8'))
        rows = raw['data'][symbol]['day']
        reference = pd.DataFrame([{'date': pd.Timestamp(row[0]).date(), 'open': float(row[1]),
            'close': float(row[2]), 'high': float(row[3]), 'low': float(row[4]),
            'volume': float(row[5]) * 100, 'amount': float(row[8]) * 10000}
            for row in rows]).set_index('date')
        joined = daily.join(reference, rsuffix='_tencent', how='left')
        deltas = pd.DataFrame(index=joined.index)
        for column in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            deltas[column + '_delta'] = joined[column] - joined[column + '_tencent']
        deltas.to_csv(OUT / (kind + '_daily_crosscheck.csv'))
        price_error = deltas[[column + '_delta' for column in ('open', 'high', 'low', 'close')]].abs().max(axis=1)
        checks[kind] = {'rows': len(frame), 'days': len(counts), 'first': str(frame.index.min()),
            'last': str(frame.index.max()), 'rows_per_day': {str(key): int(value) for key, value in counts.value_counts().items()},
            'duplicate_timestamps': int(frame.index.duplicated().sum()),
            'nonpositive_price_rows': int((~prices.gt(0).all(axis=1)).sum()),
            'invalid_ohlc_rows': int((~valid).sum()),
            'tencent_missing_days': int(joined.close_tencent.isna().sum()),
            'ohlc_max_abs_delta': float(price_error.max()),
            'volume_max_abs_delta_shares': float(deltas.volume_delta.abs().max()),
            'amount_max_abs_delta_cny': float(deltas.amount_delta.abs().max()),
            'amount_days_over_100_cny': int((deltas.amount_delta.abs() > 100).sum()),
            'certified_for_backtest': False,
            'note': 'Diagnostic comparison; no tolerance change to formal strategy audits.'}
    (OUT / 'hf_reproducible_sample_checks.json').write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
