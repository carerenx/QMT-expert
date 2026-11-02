# -*- coding: utf-8 -*-
"""Read-only check: C:/QMT/bin.x64/python.exe Stragety/RedisQMT/CSI1000Independent/probe_native_qmt.py

Local native QMT SDK, separate from Redis. No downloads or trading.
Checks the first missing candidate session; see ResearchProtocol.md.
"""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/analysis/csi1000_independent_20261027/native_qmt'


def main():
    raise SystemExit('Disabled: user requires Redis QMT as the sole data source (2026-10-27).')
    OUT.mkdir(exist_ok=True)
    path = OUT / 'probe.json'
    if path.exists():
        print(path.read_text(encoding='utf-8'))
        return
    record = {'source': 'Local native QMT SDK, NOT Redis RPC',
              'retrieved': '2026-10-27', 'stock': '603305.SH',
              'date': '20251010', 'orders': 0, 'downloads': 0,
              'certified_for_backtest': False}
    try:
        from xtquant import xtdata
        record['sdk'] = xtdata.__file__
        frames = xtdata.get_local_data(
            field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
            stock_list=[record['stock']], period='1m',
            start_time='20251010000000', end_time='20251010150000',
            count=-1, dividend_type='none', fill_data=False,
            data_dir='C:/QMT/datadir')
        frame = frames.get(record['stock'])
        record['rows'] = 0 if frame is None else len(frame)
        if frame is not None and not frame.empty:
            frame.to_csv(str(OUT / '603305.SH_20251010.csv'))
            record['positive_price_rows'] = int(frame[['open', 'high', 'low', 'close']].gt(0).all(axis=1).sum())
            record['volume_sum'] = float(frame.volume.sum())
            record['amount_sum'] = float(frame.amount.sum())
    except Exception as error:
        record['error'] = '{}: {}'.format(type(error).__name__, error)
    path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2), flush=True)


if __name__ == '__main__':
    main()
