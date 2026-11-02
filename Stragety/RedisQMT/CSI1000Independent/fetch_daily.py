# -*- coding: utf-8 -*-
"""Archive daily source: python Stragety/RedisQMT/CSI1000Independent/fetch_daily.py

Reads Redis QMT raw daily history for reconstructed mainboard union and index.
No download jobs, orders or formal backtest. See ResearchProtocol.md.
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure
from core.source_policy import require_redis_membership


BASE = ROOT / 'output/analysis/csi1000_independent_20261027'
OUT = BASE / 'daily'
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def main():
    universe_path = BASE / 'reconstructed_membership.json'
    universe = json.loads(universe_path.read_text(encoding='utf-8'))
    require_redis_membership(universe)
    codes = universe['annual_mainboard_union'] + ['000852.SH']
    digest = hashlib.sha256(universe_path.read_bytes()).hexdigest()
    OUT.mkdir(exist_ok=True)
    manifest_path = OUT / 'manifest.json'
    manifest = {'source': 'Redis QMT explicit RPC raw daily', 'start': '20240101',
                'end': '20261009', 'dividend_type': 'none', 'downloads': 0, 'orders': 0,
                'universe_sha256': digest, 'codes': codes, 'files': {},
                'scope': 'Daily source diagnostics; no formal minute performance'}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['universe_sha256'] != digest or manifest['codes'] != codes:
            raise ValueError('Universe changed; preserve previous archive')
    pending = []
    for code in codes:
        if code not in manifest['files']:
            pending.append(code)
        elif hashlib.sha256((OUT / f'{code}.parquet').read_bytes()).hexdigest() != manifest['files'][code]['sha256']:
            raise ValueError(f'Archived daily changed: {code}')
    connection = configure(timeout_seconds=15)
    trader = connection[0]
    data = connection[1]
    if trader.client.transport_name != 'redis' or not trader.client.call('ping', timeout_seconds=15).get('pong'):
        raise RuntimeError('Redis QMT not connected; stop')
    for offset in range(0, len(pending), 20):
        batch = pending[offset:offset + 20]
        raw = data.get_market_data_ex(
            field_list=FIELDS, stock_list=batch, period='1d',
            start_time=manifest['start'], end_time=manifest['end'],
            dividend_type='none', fill_data=False, bypass_formula=True, timeout_seconds=30)
        for code in batch:
            frame = raw.get(code)
            if frame is None or frame.empty:
                manifest.setdefault('empty_codes', []).append(code)
                continue
            frame = frame[FIELDS].copy()
            frame.index = frame.index.astype(str)
            frame = frame.sort_index()
            annual = frame.loc['20251009':'20261009']
            finite = np.isfinite(annual.to_numpy()).all(axis=1)
            positive = annual[['open', 'high', 'low', 'close']].gt(0).all(axis=1)
            active = annual.volume.gt(0) & annual.amount.gt(0)
            path = OUT / f'{code}.parquet'
            frame.to_parquet(path)
            manifest['files'][code] = {'rows': len(frame), 'first': str(frame.index[0]),
                                       'last': str(frame.index[-1]), 'annual_rows': len(annual),
                                       'annual_positive_finite': int((finite & positive).sum()),
                                       'annual_active': int(active.sum()),
                                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print(f'Daily source archived {len(manifest["files"])}/{len(codes)}', flush=True)


if __name__ == '__main__':
    main()
