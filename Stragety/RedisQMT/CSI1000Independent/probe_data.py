# -*- coding: utf-8 -*-
"""Read-only data checks: python Stragety/RedisQMT/CSI1000Independent/probe_data.py

Checks Redis connectivity, dated membership and sample daily/minute prices.
No trading, download requests or performance calculation. See ResearchProtocol.md.
"""
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))

from bigqmt_signal_trader.xtquant_compat import configure


OUT = ROOT / 'output/analysis/csi1000_independent_20261027'
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / 'readiness.json'
    if target.exists():
        print(target.read_text(encoding='utf-8'))
        return
    result = {'source': 'Redis QMT', 'orders': 0, 'downloads': 0,
              'scope': 'Sample checks only; not universe-wide certification',
              'historical': {}, 'samples': {}}

    def save():
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    try:
        connection = configure(timeout_seconds=15)
        trader = connection[0]
        data = connection[1]
        if trader.client.transport_name != 'redis':
            raise RuntimeError('Redis transport required')
        pong = trader.client.call('ping', timeout_seconds=15)
        if not pong.get('pong'):
            raise RuntimeError('Redis QMT ping failed')
        result['connection'] = {key: pong.get(key) for key in ('pong', 'server_time', 'rpc_revision')}
        save()
        for date in ('20251009', '20251215', '20260615'):
            timestamp = int(pd.Timestamp(date + ' 15:00', tz='Asia/Shanghai').value // 1_000_000)
            codes = data.get_stock_list_in_sector('中证1000', real_timetag=timestamp)
            result['historical'][date] = {'timestamp': timestamp, 'count': len(codes), 'codes': codes}
            save()
        # Separate dates test the stated annual window; no download is triggered.
        for code in ('000006.SZ', '002028.SZ', '600201.SH'):
            for date in ('20251010', '20261009'):
                for period in ('1d', '1m'):
                    key = f'{code}_{date}_{period}'
                    frames = data.get_market_data_ex(
                        field_list=FIELDS, stock_list=[code], period=period,
                        start_time=date + '000000', end_time=date + '150000',
                        dividend_type='none', fill_data=False,
                        bypass_formula=True, timeout_seconds=20)
                    frame = frames.get(code)
                    if frame is None or frame.empty:
                        result['samples'][key] = {'rows': 0, 'usable_prices': False}
                    else:
                        frame.to_parquet(OUT / (key + '.parquet'))
                        positive = frame[['open', 'high', 'low', 'close']].gt(0).all(axis=1)
                        ordered = frame.high.ge(frame[['open', 'low', 'close']].max(axis=1))
                        ordered &= frame.low.le(frame[['open', 'high', 'close']].min(axis=1))
                        result['samples'][key] = {
                            'rows': len(frame), 'positive_price_rows': int(positive.sum()),
                            'usable_prices': bool((positive & ordered).all()
                                                  and not frame.index.duplicated().any()
                                                  and frame.volume.sum() > 0
                                                  and frame.amount.sum() > 0),
                            'first': str(frame.index[0]), 'last': str(frame.index[-1])}
                    save()
                    print(key, result['samples'][key], flush=True)
        result['formal_backtest_ready'] = False
        result['reason'] = 'Historical universe and full trading-session coverage not certified'
        save()
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
        save()
        raise


if __name__ == '__main__':
    main()
