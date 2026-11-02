# -*- coding: utf-8 -*-
"""Run: python Stragety/RedisQMT/Trend100/sources/probe_xtquant.py

Probe pip-installed xtquant against the observed localhost QMT port 58600.
Read-only market APIs; download only two securities for 2026-08-06 into QMT's
cache. No Redis or trading modules. A 60-second subprocess limit bounds hangs.
Reference: https://knowledge.thinktrader.net/docs/xtquant/code_examples.html
See ../AlternativeDataSources.md; returned rows do not certify valid history.
"""
import json
from pathlib import Path
import subprocess
import sys

OUT = Path(__file__).resolve().parents[4] / 'output/analysis/trend100_alternative_sources_20261102'


def worker():
    from xtquant import xtdata
    xtdata.enable_hello = False
    result = {'source': 'pip xtquant, localhost:58600', 'redis_calls': 0,
              'orders': 0, 'formal_backtest_ready': False, 'operations': []}

    def save():
        (OUT / 'xtquant_probe.json').write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')

    try:
        client = xtdata.connect(ip='127.0.0.1', port=58600)
        result['connected'] = bool(client.is_connected())
        save()
        for code in ['600000.SH', '510300.SH']:
            for operation in ['read_before', 'download', 'read_after']:
                record = {'code': code, 'operation': operation, 'start': '20260806', 'end': '20260806'}
                result['operations'].append(record)
                record['status'] = 'running'
                save()
                try:
                    if operation == 'download':
                        value = xtdata.download_history_data(code, '1m', '20260806', '20260806', incrementally=False)
                        record['return_repr'] = repr(value)
                    else:
                        value = xtdata.get_market_data_ex([], [code], period='1m', start_time='20260806093000',
                            end_time='20260806150000', count=-1, dividend_type='none', fill_data=False)
                        frame = value.get(code)
                        record['rows'] = 0 if frame is None else len(frame)
                        if frame is not None and len(frame):
                            record['first'] = str(frame.index.min())
                            record['last'] = str(frame.index.max())
                            record['columns'] = list(frame.columns)
                            frame.to_csv(OUT / f'xtquant_{code}_{operation}.csv')
                    record['status'] = 'returned'
                except Exception as exc:
                    record['status'] = 'error'
                    record['error'] = type(exc).__name__ + ': ' + str(exc)
                save()
                print(json.dumps(record, ensure_ascii=True), flush=True)
    except Exception as exc:
        result['connection_error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        xtdata.disconnect()
        save()


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    if '--worker' in sys.argv:
        worker()
    else:
        try:
            completed = subprocess.run([sys.executable, __file__, '--worker'], timeout=60)
            print(json.dumps({'worker_exit_code': completed.returncode}))
        except subprocess.TimeoutExpired:
            path = OUT / 'xtquant_probe.json'
            result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            result['worker_timeout_seconds'] = 60
            path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
            print('Market probe timed out after 60 seconds; partial evidence retained.')
