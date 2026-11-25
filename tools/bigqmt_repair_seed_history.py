"""Repair stale daily seed history through the existing BigQMT Pipe only.

Plan: python tools/bigqmt_repair_seed_history.py --date 20261124 --symbols 600000.SH 600036.SH 000001.SZ
Apply: same command plus --apply. Default max 50 symbols; --max-symbols sets
an explicit upper bound. No orders, subscriptions, Redis, paid source or fallback.
Success means returned OHLC covers 60 complete sessions, not download progress.
Official API: https://dict.thinktrader.net/nativeApi/xtdata.html (download_history_data2).
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'integrations/bigqmt_pipe/src'))
from bigqmt_signal_trader.xtquant_compat import BigQmtRpcClient, BigQmtXtData
from Stragety.MiniQMT_Stragety.core.ma5_ma20_seed_selection import _date_key, is_main_board


def verify_history(frame, expected):
    if frame is None or frame.empty:
        return False
    days = {_date_key(index): row for index, row in frame.iterrows()}
    if not expected or any(day not in days for day in expected):
        return False
    for day in expected:
        row = days[day]
        values = [float(row.get(field, float('nan'))) for field in ('high', 'low', 'close')]
        if not all(math.isfinite(value) and value > 0 for value in values):
            return False
        if not values[1] <= values[2] <= values[0]:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True)
    parser.add_argument('--symbols', nargs='*')
    parser.add_argument('--max-symbols', type=int, default=50)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', type=Path, help='same-date results.json; already attempted symbols are never retried')
    args = parser.parse_args()
    datetime.strptime(args.date, '%Y%m%d')
    if args.max_symbols <= 0:
        parser.error('--max-symbols must be positive')
    sys.stdout.reconfigure(encoding='utf-8')
    output = ROOT / 'analysis' / ('dayt_seed_repair_' + args.date) / datetime.now().strftime('%H%M%S_%f')
    output.mkdir(parents=True)
    client = BigQmtRpcClient(account_id='8890145315', transport='pipe', timeout_seconds=8,
                            redis_config={'redis_enabled': False, 'formula_server': {'enabled': False}})
    api = BigQmtXtData(client)
    results = []
    try:
        ping = client.call('ping', use_formula=False)
        if not ping.get('pong') or str(ping.get('account_id')) != '8890145315':
            raise RuntimeError('Pipe account handshake failed')
        dates = sorted({_date_key(value) for value in api.get_trading_dates('SH', end_time=args.date, count=90)})
        completed = [day for day in dates if day < args.date]
        if len(completed) < 80:
            raise RuntimeError('calendar has fewer than 80 verified complete sessions')
        expected = completed[-60:]
        if args.resume:
            previous = json.loads(args.resume.read_text(encoding='utf-8'))
            if previous['date'] != args.date or previous['expected_dates'] != expected or not previous['apply']:
                raise RuntimeError('resume requires the same verified dates and an applied run')
            results.extend(previous['records'])
        codes = sorted(set(args.symbols or api.get_stock_list_in_sector('沪深A股')))
        codes = [code for code in codes if is_main_board(code)][:args.max_symbols]
        if not codes:
            raise RuntimeError('empty main-board repair scope')
        attempted = {row['code'] for row in results}
        codes = [code for code in codes if code not in attempted]

        def read(batch):
            return api.get_market_data_ex(field_list=['high', 'low', 'close'], stock_list=batch,
                period='1d', start_time=completed[-80], end_time=completed[-1], count=81,
                dividend_type='front', fill_data=False, chunk_size=0, timeout_seconds=8,
                use_formula=False, heal=False, subscribe=False) or {}

        for offset in range(0, len(codes), 20):
            batch = codes[offset:offset + 20]
            before = read(batch)
            missing = [code for code in batch if not verify_history(before.get(code), expected)]
            reply = None
            if args.apply and missing:
                print('Downloading daily history only:', len(missing), 'first=', missing[0], flush=True)
                reply = client.call('download_history_data2', {'stock_list': missing, 'period': '1d',
                    'start_time': completed[-80], 'end_time': completed[-1]},
                    timeout_seconds=30, use_formula=False)
            after = read(batch) if args.apply and missing else before
            for code in batch:
                good = verify_history(after.get(code), expected)
                row = {'code': code, 'before_verified': code not in missing,
                       'after_verified': good, 'requested_download': bool(args.apply and code in missing)}
                results.append(row)
                frame = after.get(code)
                if frame is not None:
                    frame.to_csv(output / (code + '.csv'))
            summary = {'checked': len(results), 'verified': sum(row['after_verified'] for row in results),
                       'download_reply': reply, 'apply': args.apply}
            print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
            (output / 'results.json').write_text(json.dumps({'source': 'BigQMT Pipe', 'date': args.date,
                'server_time': ping.get('server_time'), 'expected_dates': expected,
                'start_time': completed[-80], 'apply': args.apply, 'records': results},
                ensure_ascii=False, indent=2), encoding='utf-8')
            failed = sum(not row['after_verified'] for row in results[-len(batch):])
            if args.apply and failed:
                print('Excluded incomplete symbols without retry:', failed, flush=True)
                if failed > len(batch) / 2:
                    raise RuntimeError('majority of batch remains incomplete; stopped without retry/fallback')
        print('Report:', output, flush=True)
    finally:
        transport = getattr(client, '_transport_instance', None)
        if transport:
            transport.stop()


if __name__ == '__main__':
    main()
