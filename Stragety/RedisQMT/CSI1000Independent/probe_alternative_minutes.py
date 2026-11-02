# -*- coding: utf-8 -*-
"""Probe TDX depth: python Stragety/RedisQMT/CSI1000Independent/probe_alternative_minutes.py

Alternative source, never Redis QMT. Samples frequency=8 at three offsets.
No return calculation; full-date coverage and cross-source agreement still required.
"""
import json
import socket
from pathlib import Path

from mootdx.quotes import Quotes


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/analysis/csi1000_independent_20261027/alternative_tdx'
SERVERS = ('119.97.185.59', '124.70.133.119', '116.205.183.150',
           '123.60.73.44', '116.205.163.254', '121.36.225.169',
           '123.60.70.228', '124.71.9.153', '110.41.147.114', '124.71.187.122')


def main():
    raise SystemExit('Disabled: user requires Redis QMT as the sole data source (2026-10-27).')
    OUT.mkdir(exist_ok=True)
    path = OUT / 'probe.json'
    record = {'source': 'TDX public quote servers via mootdx; NOT Redis QMT',
              'retrieved': '2026-10-27', 'symbol': '603305', 'frequency': 8,
              'connections': [], 'samples': {}, 'certified_for_backtest': False}
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        if any(item.get('rows', 0) for item in record['samples'].values()):
            print(json.dumps(record, indent=2))
            return
    attempted = {item['host'] for item in record['connections']}
    client = None
    for host in SERVERS:
        if host in attempted:
            continue
        try:
            with socket.create_connection((host, 7709), timeout=2):
                pass
            client = Quotes.factory(market='std', server=(host, 7709), timeout=4,
                                    auto_retry=False, raise_exception=True)
            latest = client.bars(symbol=record['symbol'], frequency=8, start=0, offset=800)
            if latest is None or latest.empty:
                raise ValueError('TCP connected but quote request returned empty')
            latest.to_parquet(OUT / '603305_offset0.parquet')
            record['samples']['0'] = {'rows': len(latest), 'first': str(latest.index[0]),
                                       'last': str(latest.index[-1]), 'columns': list(latest.columns)}
            record['connections'].append({'host': host, 'connected': True, 'quote_verified': True})
            break
        except Exception as error:
            if client is not None:
                client.client.disconnect()
            client = None
            record['connections'].append({'host': host, 'connected': False,
                                           'error': f'{type(error).__name__}: {error}'})
    if client is not None:
        for start in (24000, 57600):
            try:
                frame = client.bars(symbol=record['symbol'], frequency=8, start=start, offset=800)
                if frame is None or frame.empty:
                    record['samples'][str(start)] = {'rows': 0}
                else:
                    frame.to_parquet(OUT / f'603305_offset{start}.parquet')
                    record['samples'][str(start)] = {'rows': len(frame),
                                                     'first': str(frame.index[0]),
                                                     'last': str(frame.index[-1]),
                                                     'columns': list(frame.columns)}
            except Exception as error:
                record['samples'][str(start)] = {'error': f'{type(error).__name__}: {error}'}
            path.write_text(json.dumps(record, indent=2), encoding='utf-8')
        client.client.disconnect()
    path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2), flush=True)


if __name__ == '__main__':
    main()
