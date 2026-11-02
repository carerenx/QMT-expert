# -*- coding: utf-8 -*-
"""Archive actions: python Stragety/RedisQMT/CSI1000Independent/fetch_actions.py

Reuses identified Redis archives and fetches remaining corporate-action maps.
No orders or downloads. API reference: Python API p57. See ResearchProtocol.md.
"""
import hashlib
import json
import math
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure
from core.source_policy import require_redis_membership


OUT = ROOT / 'output/analysis/csi1000_independent_20261027'
ARCHIVES = ('output/analysis/a144_secondary_reversion_20261012/corporate_actions.json',
            'output/analysis/a144_historical_constituents_20261014/dynamic_daily/corporate_actions.json')


def main():
    universe = json.loads((OUT / 'reconstructed_membership.json').read_text(encoding='utf-8'))
    require_redis_membership(universe)
    codes = universe['annual_mainboard_union']
    path = OUT / 'corporate_actions.json'
    evidence_path = OUT / 'corporate_actions_sources.json'
    actions = {}
    evidence = {}
    if path.exists():
        actions = json.loads(path.read_text(encoding='utf-8'))
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    for relative in ARCHIVES:
        source = ROOT / relative
        old = json.loads(source.read_text(encoding='utf-8'))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for code in codes:
            if code in old and code not in actions:
                actions[code] = old[code]
                evidence[code] = {'source': 'Prior Redis QMT archive', 'file': relative,
                                  'archive_sha256': digest}

    def save():
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        path.write_text(json.dumps(actions, indent=2), encoding='utf-8')

    save()
    connection = configure(timeout_seconds=15)
    trader = connection[0]
    data = connection[1]
    if trader.client.transport_name != 'redis' or not trader.client.call('ping', timeout_seconds=15).get('pong'):
        raise RuntimeError('Redis QMT unavailable; stop')
    raw_dir = OUT / 'actions_raw'
    raw_dir.mkdir(exist_ok=True)
    for code in codes:
        if code in actions:
            continue
        raw = data.client.call_rpc('get_divid_factors', {'stock_code': code}, timeout_seconds=15)
        if not isinstance(raw, dict):
            raise ValueError(f'Unexpected corporate action schema: {code}')
        parsed = []
        for stamp, values in raw.items():
            if len(values) != 7 or not all(math.isfinite(float(value)) for value in values) or values[6] <= 0:
                raise ValueError(f'Invalid factor: {code} {stamp}')
            date = pd.Timestamp(int(stamp), unit='ms', tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y%m%d')
            parsed.append({'date': date, 'interest': values[0], 'bonus': values[1],
                           'gift': values[2], 'allot': values[3], 'allot_price': values[4],
                           'reform': values[5], 'dr': values[6]})
        raw_path = raw_dir / f'{code}.json'
        raw_path.write_text(json.dumps(raw, indent=2), encoding='utf-8')
        actions[code] = sorted(parsed, key=lambda event: event['date'])
        evidence[code] = {'source': 'Redis QMT explicit RPC', 'retrieved': '2026-10-27',
                          'file': str(raw_path.relative_to(ROOT)),
                          'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                          'empty': not raw,
                          'limitation': 'No payment/listing dates; empty map is not proof of no actions'}
        save()
        if len(actions) % 25 == 0:
            print(f'Action maps archived {len(actions)}/{len(codes)}', flush=True)
    print(f'Action maps complete {len(actions)}/{len(codes)}; payment/listing dates unverified', flush=True)


if __name__ == '__main__':
    main()
