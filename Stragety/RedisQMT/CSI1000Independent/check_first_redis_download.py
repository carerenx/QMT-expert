# -*- coding: utf-8 -*-
"""Check: python Stragety/RedisQMT/CSI1000Independent/check_first_redis_download.py

One first supplementation attempt, 603305.SH/2025-10-10, through Redis only.
Resumes the same recorded job; never resubmits. No trading or formal backtest.
Native reference: Python API p69 down_history_data. See ResearchProtocol.md.
"""
import json
import sys
import time
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure
from audit_candidate_minutes import check


BASE = ROOT / 'output/analysis/csi1000_independent_20261027'
OUT = BASE / 'first_redis_download'
CODE = '603305.SH'
DATE = '20251010'
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def main():
    OUT.mkdir(exist_ok=True)
    path = OUT / 'attempt.json'
    record = {'source': 'Redis QMT explicit RPC', 'code': CODE, 'date': DATE,
              'orders': 0, 'downloads': 0, 'submission_started': False}
    if path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        if 'validation' in record or record.get('terminal_failure'):
            print(json.dumps(record, indent=2))
            return
        if record.get('submission_started') and not record.get('job_id'):
            raise RuntimeError('Submission outcome uncertain; do not submit again')

    def save():
        path.write_text(json.dumps(record, indent=2), encoding='utf-8')

    connection = configure(timeout_seconds=15)
    client = connection[0].client
    data = connection[1]
    if client.transport_name != 'redis':
        raise RuntimeError('Redis transport required')
    pong = client.call_rpc('ping', timeout_seconds=15)
    if not pong.get('pong'):
        raise RuntimeError('Redis QMT connection failed; stop')
    record['server_time'] = pong.get('server_time')
    if not record.get('job_id'):
        membership = json.loads((BASE / 'redis_rpc_membership.json').read_text(encoding='utf-8'))
        latest = next(item['response'] for item in membership['requests'] if item['date'] == 'latest')
        if CODE not in latest:
            raise ValueError('Probe code absent from explicit Redis current membership')
        history = json.loads((BASE / 'redis_download_history_check.json').read_text(encoding='utf-8'))
        if history['jobs']:
            raise RuntimeError('A prior recorded download exists; inspect instead of submitting')
        record['submission_started'] = True
        record['downloads'] = 1
        record['params'] = {'stock_list': [CODE], 'period': '1m',
                             'start_time': DATE + '000000', 'end_time': DATE + '150000',
                             'chunk_size': 1}
        save()
        job = client.call_rpc('submit_download_history_data2', record['params'], timeout_seconds=15)
        record['job_id'] = job['job_id']
        record['last_status'] = job
        save()
    deadline = time.monotonic() + 30
    while True:
        status = client.call_rpc('get_download_status', {'job_id': record['job_id']}, timeout_seconds=15)
        record['last_status'] = status
        save()
        if status['state'] in ('done', 'failed'):
            break
        if time.monotonic() >= deadline:
            print(json.dumps({'job_id': record['job_id'], 'state': status['state'],
                              'note': 'Resume same job; no resubmission'}), flush=True)
            return
        time.sleep(1)
    if status['state'] == 'failed':
        record['terminal_failure'] = True
        save()
        print(json.dumps(status), flush=True)
        return
    # A finished command changes the store's potential availability; verify it
    # once rather than treating a completed job as proof of valid history.
    raw_daily = data.get_market_data_ex(
        field_list=FIELDS, stock_list=[CODE], period='1d', start_time=DATE,
        end_time=DATE, dividend_type='none', fill_data=False,
        bypass_formula=True, timeout_seconds=20)[CODE]
    raw_minutes = data.get_market_data_ex(
        field_list=FIELDS, stock_list=[CODE], period='1m', start_time=DATE + '000000',
        end_time=DATE + '150000', dividend_type='none', fill_data=False,
        bypass_formula=True, timeout_seconds=20).get(CODE)
    raw_daily.to_parquet(OUT / 'daily.parquet')
    if raw_minutes is not None:
        raw_minutes.to_parquet(OUT / 'minutes.parquet')
    record['validation'] = check(raw_minutes, raw_daily.loc[DATE], DATE)
    record['formal_backtest_ready'] = False
    save()
    print(json.dumps({'job_id': record['job_id'], 'validation': record['validation']}), flush=True)


if __name__ == '__main__':
    main()
