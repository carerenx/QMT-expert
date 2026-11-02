# -*- coding: utf-8 -*-
"""Run: python Stragety/RedisQMT/Trend100/sources/probe_pipe_qmt.py

Read two securities through the repository's Named Pipe QMT bridge, preserving
raw unadjusted bars. No Redis, native xtquant connection, subscription or orders.
No automatic download/healing; actual returned dates are recorded, not assumed.
Interface: integrations/bigqmt_pipe/README.md and xtquant_compat.py.
See ../AlternativeDataSources.md for validation limits.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'
SRC = ROOT / 'integrations/bigqmt_pipe/src'
sys.path.insert(0, str(SRC))


def main():
    from bigqmt_signal_trader.xtquant_compat import configure, load_client_config
    config = load_client_config()
    settings = dict(config.get('redis_config') or {})
    if settings.get('transport') != 'pipe':
        raise ValueError('Expected explicitly configured pipe transport')
    settings.update(local_cache_enabled=False, formula_server_enabled=False)
    trader, data = configure(redis_config=settings, timeout_seconds=20)
    if trader.client.transport_name != 'pipe':
        raise ValueError('Refusing non-pipe transport')
    result = {'source': 'QMT Named Pipe bridge', 'redis_calls': 0, 'orders': 0,
              'formal_backtest_ready': False, 'operations': []}
    OUT.mkdir(parents=True, exist_ok=True)

    def save():
        (OUT / 'pipe_qmt_probe.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    ping = trader.client.call('ping')
    result['ping'] = {'pong': bool(ping.get('pong')), 'version': ping.get('version')}
    save()
    print(json.dumps(result['ping']), flush=True)
    for day in ['20260806', '20251010', '20261009']:
        for code in ['600000.SH', '510300.SH']:
            record = {'code': code, 'requested_day': day}
            try:
                frames = data.get_market_data_ex(
                    field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                    stock_list=[code], period='1m', start_time=day+'093000',
                    end_time=day+'150000', count=-1, dividend_type='none', fill_data=False,
                    timeout_seconds=20, use_formula=False, backfill_pre_close=False,
                    resynth_ongoing_multiday=False, heal=False, subscribe=False)
                frame = frames.get(code)
                record['rows'] = 0 if frame is None else len(frame)
                if frame is not None and len(frame):
                    path = OUT / f'pipe_qmt_{code}_{day}_1m.csv'
                    frame.to_csv(path)
                    record.update(first=str(frame.index.min()), last=str(frame.index.max()),
                        columns=list(frame.columns), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            except Exception as exc:
                record['error'] = type(exc).__name__ + ': ' + str(exc)
            result['operations'].append(record)
            save()
            print(json.dumps(record, ensure_ascii=True), flush=True)
    for code in ['600000.SH', '510300.SH']:
        record = {'code': code, 'requested_count': 60000, 'requested_dates': 'unbounded'}
        try:
            frames = data.get_market_data_ex(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                stock_list=[code], period='1m', count=60000, dividend_type='none',
                fill_data=False, timeout_seconds=30, use_formula=False,
                backfill_pre_close=False, resynth_ongoing_multiday=False,
                heal=False, subscribe=False)
            frame = frames.get(code)
            record['rows'] = 0 if frame is None else len(frame)
            if frame is not None and len(frame):
                path = OUT / f'pipe_qmt_{code}_last60000_1m.csv'
                frame.to_csv(path)
                record.update(first=str(frame.index.min()), last=str(frame.index.max()),
                    columns=list(frame.columns), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
        result['operations'].append(record)
        save()
        print(json.dumps(record, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
