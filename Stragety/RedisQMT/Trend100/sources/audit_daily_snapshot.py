# -*- coding: utf-8 -*-
"""Inspect the checksum-verified daily archive; run from repository root:
python Stragety/RedisQMT/Trend100/sources/audit_daily_snapshot.py

Audits raw stock daily fields in the candidate 2025-08-07..2026-08-06 window;
does not certify historical universe completeness, calculate returns or extract code.
Source dates, retired securities and missing-window files are retained in inventory.
See ../AlternativeDataSources.md.
"""
from io import BytesIO
import json
from pathlib import Path
import tarfile

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from download_daily_snapshot import EXPECTED_SHA, sha256


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'


def main():
    path = OUT / 'a-share-raw-v0.tar'
    if sha256(path) != EXPECTED_SHA:
        raise ValueError('Daily archive checksum mismatch')
    rows = []
    reference_files = []
    required = {'date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount',
                'adjustflag', 'tradestatus', 'isST'}
    with tarfile.open(path, mode='r:') as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith('.parquet'):
                continue
            if '/._' in member.name:
                continue
            if not member.name.startswith('raw/bars_daily/'):
                reference_files.append({'name': member.name, 'size': member.size})
                continue
            frame = pq.read_table(BytesIO(archive.extractfile(member).read())).to_pandas()
            missing = sorted(required - set(frame.columns))
            record = {'file': member.name, 'all_rows': len(frame), 'missing_fields': ','.join(missing)}
            if not missing:
                record['first_date'] = frame.date.min()
                record['last_date'] = frame.date.max()
                record['duplicate_dates'] = int(frame.date.duplicated().sum())
                window = frame[frame.date.between('2025-08-07', '2026-08-06')]
                record['window_rows'] = len(window)
                st = pd.to_numeric(window.isST, errors='coerce')
                status = pd.to_numeric(window.tradestatus, errors='coerce')
                record['invalid_status_rows'] = int((~status.isin([0, 1]) | ~st.isin([0, 1])).sum())
                record['window_st_days'] = int(st.eq(1).sum())
                record['window_suspended_days'] = int(status.eq(0).sum())
                traded = window[status.eq(1)]
                prices = traded[['open', 'high', 'low', 'close']].apply(pd.to_numeric, errors='coerce').astype('float64')
                valid = np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
                valid &= prices.high.ge(prices[['open', 'low', 'close']].max(axis=1))
                valid &= prices.low.le(prices[['open', 'high', 'close']].min(axis=1))
                liquidity = traded[['volume', 'amount']].apply(pd.to_numeric, errors='coerce').astype('float64')
                valid &= np.isfinite(liquidity).all(axis=1) & liquidity.gt(0).all(axis=1)
                record['invalid_active_rows'] = int((~valid).sum())
                record['unexpected_adjustflag_rows'] = int(pd.to_numeric(window.adjustflag, errors='coerce').ne(3).sum())
            rows.append(record)
            if len(rows) % 1000 == 0:
                print(json.dumps({'stock_files_inspected': len(rows)}), flush=True)
    result = pd.DataFrame(rows)
    result.to_csv(OUT / 'github_daily_stock_audit.csv', index=False)
    summary = {'archive_sha256': EXPECTED_SHA, 'stock_files': len(result),
        'stock_all_rows': int(result.all_rows.sum()),
        'files_missing_required_fields': int(result.missing_fields.ne('').sum()),
        'window_rows': int(result.window_rows.sum()),
        'files_without_window_rows': int(result.window_rows.eq(0).sum()),
        'invalid_active_rows': int(result.invalid_active_rows.sum()),
        'invalid_status_rows': int(result.invalid_status_rows.sum()),
        'duplicate_dates': int(result.duplicate_dates.sum()),
        'unexpected_adjustflag_rows': int(result.unexpected_adjustflag_rows.sum()),
        'reference_files': reference_files,
        'certified_historical_universe': False, 'formal_backtest_ready': False}
    (OUT / 'github_daily_stock_audit.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key != 'reference_files'},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
