# -*- coding: utf-8 -*-
"""Audit entry sessions: python Stragety/RedisQMT/CSI1000Independent/audit_candidate_minutes.py

Read-only Redis 1m requests, one per candidate date; caches failed sessions too.
No download jobs, orders or performance. Exit-session coverage is a separate gate.
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure
from core.source_policy import require_redis_membership


BASE = ROOT / 'output/analysis/csi1000_independent_20261027'
OUT = BASE / 'entry_minutes'
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def check(frame, daily, date):
    if frame is None or frame.empty:
        return {'valid': False, 'reason': ['empty'], 'rows': 0}
    frame = frame.sort_index()
    expected = list(pd.date_range(date + ' 09:31', date + ' 11:30', freq='min').strftime('%Y%m%d%H%M%S'))
    expected += list(pd.date_range(date + ' 13:01', date + ' 15:00', freq='min').strftime('%Y%m%d%H%M%S'))
    continuous = frame.loc[frame.index.isin(expected)]
    reasons = []
    if list(continuous.index) != expected or frame.index.duplicated().any():
        reasons.append('session_timestamps')
    if not np.isfinite(frame[FIELDS].to_numpy()).all():
        reasons.append('nonfinite')
    if not frame[['open', 'high', 'low', 'close']].gt(0).all().all():
        reasons.append('nonpositive_price')
    if not (frame.high.add(1e-8).ge(frame[['open', 'low', 'close']].max(axis=1))
            & frame.low.sub(1e-8).le(frame[['open', 'high', 'close']].min(axis=1))).all():
        reasons.append('ohlc_order')
    if frame[['volume', 'amount']].lt(0).any().any():
        reasons.append('negative_activity')
    if frame.volume.sum() <= 0 or frame.amount.sum() <= 0:
        reasons.append('zero_activity')
    aggregate = {'open': frame.open.iloc[0], 'high': frame.high.max(),
                 'low': frame.low.min(), 'close': frame.close.iloc[-1],
                 'volume': frame.volume.sum(), 'amount': frame.amount.sum()}
    differences = {}
    for field in FIELDS:
        difference = abs(float(aggregate[field]) - float(daily[field]))
        differences[field] = difference
        tolerance = max(1e-6, abs(float(daily[field])) * 1e-8)
        if difference > tolerance:
            reasons.append('daily_mismatch_' + field)
    return {'valid': not reasons, 'reason': reasons, 'rows': len(frame),
            'positive_volume_minutes': int(frame.volume.gt(0).sum()),
            'daily_absolute_differences': differences}


def main():
    universe = json.loads((BASE / 'reconstructed_membership.json').read_text(encoding='utf-8'))
    require_redis_membership(universe)
    OUT.mkdir(exist_ok=True)
    candidate_path = BASE / 'v1_candidates.csv'
    digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    candidates = pd.read_csv(candidate_path, dtype={'trade_date': str, 'code': str})
    target = OUT / 'audit.json'
    record = {'source': 'Redis QMT explicit RPC 1m, raw, fill_data=False',
              'candidate_sha256': digest, 'downloads': 0, 'orders': 0,
              'scope': 'Provisional entry candidates only; not exit-session or formal-backtest approval',
              'sessions': {}}
    if target.exists():
        record = json.loads(target.read_text(encoding='utf-8'))
        if record['candidate_sha256'] != digest:
            raise ValueError('Candidates changed; preserve prior minute evidence')
    connection = configure(timeout_seconds=15)
    trader = connection[0]
    data = connection[1]
    if trader.client.transport_name != 'redis' or not trader.client.call('ping', timeout_seconds=15).get('pong'):
        raise RuntimeError('Redis QMT connection failed; stop')
    for date, group in candidates.groupby('trade_date', sort=True):
        codes = [code for code in group.code if f'{code}_{date}' not in record['sessions']]
        if not codes:
            continue
        raw = data.get_market_data_ex(
            field_list=FIELDS, stock_list=codes, period='1m',
            start_time=date + '000000', end_time=date + '150000',
            dividend_type='none', fill_data=False, bypass_formula=True, timeout_seconds=30)
        for code in codes:
            frame = raw.get(code)
            daily = pd.read_parquet(BASE / f'daily/{code}.parquet').loc[date]
            result = check(frame, daily, date)
            if frame is not None and not frame.empty:
                path = OUT / f'{code}_{date}.parquet'
                frame.to_parquet(path)
                result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            record['sessions'][f'{code}_{date}'] = result
        target.write_text(json.dumps(record, indent=2), encoding='utf-8')
        good = sum(item['valid'] for item in record['sessions'].values())
        print(f'Entry sessions checked {len(record["sessions"])}/{len(candidates)}, valid {good}', flush=True)
    record['valid_sessions'] = sum(item['valid'] for item in record['sessions'].values())
    record['invalid_sessions'] = len(record['sessions']) - record['valid_sessions']
    record['formal_backtest_ready'] = False
    target.write_text(json.dumps(record, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
