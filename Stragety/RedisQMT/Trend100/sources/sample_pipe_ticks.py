# -*- coding: utf-8 -*-
"""Run: python Stragety/RedisQMT/Trend100/sources/sample_pipe_ticks.py

Inspect two securities on each side of the SSE ETF closing-phase change.
Read existing snapshots first; request a missing sample once, over Named Pipe.
Retain attempts so a rerun never resubmits a known empty download. No Redis,
subscriptions, trading APIs or executable-minute conversion. See PipeClockAudit.md.
API: QMT Python API p.71, Appendix 5.5; bundled pipe xtquant_compat.py.
"""
import json
from pathlib import Path
import sys
import time

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
    report = OUT / 'pipe_tick_samples.json'
    records = json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}

    def save():
        report.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

    for day in ['20261009', '20260703', '20260806']:
        for code in ['600000.SH', '510300.SH']:
            key = code + '_' + day
            path = OUT / ('pipe_ticks_' + key + '.csv')
            if key in records:
                print(json.dumps({'existing_attempt': key, 'record': records[key]}), flush=True)
                continue

            def read():
                return data.get_market_data_ex(
                    field_list=['time', 'lastPrice', 'volume', 'amount'], stock_list=[code],
                    period='tick', start_time=day+'091500', end_time=day+'150100',
                    count=-1, dividend_type='none', fill_data=False, use_formula=False,
                    heal=False, backfill_pre_close=False, resynth_ongoing_multiday=False,
                    subscribe=False, timeout_seconds=20).get(code)

            record = {'source': 'QMT Named Pipe', 'code': code, 'day': day,
                      'redis_calls': 0, 'orders': 0, 'status': 'reading'}
            records[key] = record
            save()
            try:
                frame = read()
                record['initial_rows'] = 0 if frame is None else len(frame)
                if frame is None or frame.empty:
                    record['status'] = 'download_requested_once'
                    save()
                    record['download_reply'] = trader.client.call('download_history_data', {
                        'stock_code': code, 'period': 'tick', 'start_time': day,
                        'end_time': day}, use_formula=False, timeout_seconds=20)
                    save()
                    for delay in [3, 12]:
                        time.sleep(delay)
                        frame = read()
                        if frame is not None and len(frame):
                            break
                record['rows'] = 0 if frame is None else len(frame)
                if frame is not None and len(frame):
                    frame.to_csv(path)
                    record['first'] = str(frame.index.min())
                    record['last'] = str(frame.index.max())
                record['status'] = 'observed'
            except Exception as exc:
                record['status'] = 'error'
                record['error'] = type(exc).__name__ + ': ' + str(exc)
            save()
            print(json.dumps(record, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
