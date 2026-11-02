# -*- coding: utf-8 -*-
"""Check: python Stragety/RedisQMT/CSI1000Independent/probe_redis_membership.py

Explicit Redis RPC membership reads only; bypass FormulaServer, no downloads/orders.
Existing response is reused without retries. See ResearchProtocol.md.
"""
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure


OUT = ROOT / 'output/analysis/csi1000_independent_20261027'


def main():
    path = OUT / 'redis_rpc_membership.json'
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps({item['date']: item.get('count') for item in record['requests']}))
        return
    record = {'source': 'Redis QMT', 'route': 'client.call_rpc; no FormulaServer',
              'orders': 0, 'downloads': 0, 'requests': [],
              'historical_membership_verified': False}
    connection = configure(timeout_seconds=15)
    client = connection[0].client
    if client.transport_name != 'redis':
        raise RuntimeError('Redis transport required')
    try:
        pong = client.call_rpc('ping', timeout_seconds=15)
        if not pong.get('pong'):
            raise RuntimeError('Redis QMT ping failed; stop')
        record['connection'] = {key: pong.get(key) for key in ('pong', 'server_time', 'rpc_revision')}
        for date in ('20251009', '20251215', '20260615', 'latest'):
            timestamp = -1
            if date != 'latest':
                timestamp = int(pd.Timestamp(date + ' 15:00', tz='Asia/Shanghai').value // 1_000_000)
            params = {'sector_name': '中证1000', 'real_timetag': timestamp}
            response = client.call_rpc('get_stock_list_in_sector', params, timeout_seconds=15)
            if not isinstance(response, list):
                raise ValueError('Expected a membership list')
            codes = sorted(set(response))
            record['requests'].append({'date': date, 'params': params,
                                        'response': response, 'count': len(codes),
                                        'sha256': hashlib.sha256('\n'.join(codes).encode()).hexdigest()})
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            print(date, len(codes), flush=True)
        record['limitation'] = 'Raw Redis responses only. Current list is not historical membership certification.'
    except Exception as error:
        record['error'] = '{}: {}'.format(type(error).__name__, error)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
