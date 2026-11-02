# -*- coding: utf-8 -*-
"""Run: python Stragety/RedisQMT/Trend100/sources/probe_pipe_membership.py

Read dated stock memberships through Named Pipe, retaining all returned codes.
Compare a historical request against observed later IPOs; identical/current lists
are not historical certification. No downloads, Redis, subscriptions or orders.
QMT Python API p.45 and get_instrumentdetail/OpenDate; see DailyPoolReadiness.md.
"""
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'
sys.path.insert(0, str(ROOT / 'integrations/bigqmt_pipe/src'))


def main():
    from bigqmt_signal_trader.xtquant_compat import configure, load_client_config
    settings = dict(load_client_config()['redis_config'])
    if settings.get('transport') != 'pipe':
        raise ValueError('Only configured Named Pipe is authorized')
    settings.update(local_cache_enabled=False, formula_server_enabled=False)
    trader, data = configure(redis_config=settings, timeout_seconds=20)
    if trader.client.transport_name != 'pipe':
        raise ValueError('Refusing non-pipe transport')
    result = {'source': 'Named Pipe QMT', 'redis_calls': 0, 'orders': 0,
              'historical_membership_certified': False, 'requests': []}
    memberships = {}
    for day in ['2025-10-10', '2026-09-24', 'current']:
        stamp = -1 if day == 'current' else int(pd.Timestamp(day+' 15:00', tz='Asia/Shanghai').timestamp()*1000)
        record = {'requested_date': day, 'real_timetag': stamp}
        try:
            codes = trader.client.call('get_stock_list_in_sector',
                {'sector_name': '沪深A股', 'real_timetag': stamp}, use_formula=False)
            if not isinstance(codes, list):
                raise ValueError('Unexpected membership response type')
            path = OUT / ('pipe_membership_'+day+'.json')
            path.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding='utf-8')
            record.update(count=len(codes), unique_count=len(set(codes)),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            memberships[day] = set(codes)
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
        result['requests'].append(record)
        print(json.dumps(record), flush=True)
    if '2025-10-10' in memberships and 'current' in memberships:
        result['historical_equals_current'] = memberships['2025-10-10'] == memberships['current']
        inventory = pd.read_csv(OUT / 'github_daily_stock_audit.csv')
        later = inventory[inventory.first_date.gt('2025-10-10')].sort_values('first_date', ascending=False)
        probes = []
        for row in later.itertuples():
            code = row.file.split('/')[-1].replace('.parquet', '').split('_')[-1]
            code += '.SH' if 'CN_SSE_' in row.file else '.SZ'
            if code not in memberships['2025-10-10']:
                continue
            detail = data.get_instrument_detail(code)
            probes.append({'code': code, 'archive_first_date': row.first_date,
                'OpenDate': detail.get('OpenDate'), 'InstrumentName': detail.get('InstrumentName')})
            if len(probes) >= 5:
                break
        result['later_first_observation_in_historical_membership'] = probes
    (OUT / 'pipe_membership_probe.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
