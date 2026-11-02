# -*- coding: utf-8 -*-
"""Check alternative sources from repository root:
python Stragety/RedisQMT/Trend100/sources/probe_public.py

Public Tencent/Sina snapshots, bounded TDX and BaoStock samples. No Redis/QMT,
credentials, subscriptions or orders. Raw responses stay source-separated.
Samples cannot certify annual 1-minute coverage. See ../AlternativeDataSources.md.
"""
import hashlib
import json
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'


def save_json(name, value):
    path = OUT / (name + '.json')
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def http_probe(name, url, params):
    record = {'name': name, 'source': url, 'params': params, 'not_redis_qmt': True,
              'retrieved_utc': datetime.now(timezone.utc).isoformat(), 'certified_for_backtest': False}
    try:
        response = requests.get(url, params=params, timeout=(5, 15),
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/' if name.startswith('sina') else 'https://gu.qq.com/'})
        payload = response.content
        (OUT / (name + '.raw')).write_bytes(payload)
        record.update(status=response.status_code, bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        response.raise_for_status()
        text = response.text
        if name.startswith('sina'):
            rows = json.loads(text[text.index('=(') + 2:text.rindex(');')]) or []
            record.update(rows=len(rows), first=rows[0] if rows else None, last=rows[-1] if rows else None)
        else:
            data = response.json()
            node = (data.get('data') or {}).get(params['param'].split(',')[0], {})
            key = 'day' if 'daily' in name else 'm1'
            rows = node.get(key) or []
            record.update(api_code=data.get('code'), keys=list(node), rows=len(rows),
                          first=rows[0] if rows else None, last=rows[-1] if rows else None)
    except Exception as error:
        record['error_type'] = type(error).__name__
    save_json(name, record)
    return record


def tdx_worker():
    from mootdx.quotes import Quotes
    record = {'source': 'TDX/mootdx', 'not_redis_qmt': True, 'attempts': [], 'samples': {},
              'certified_for_backtest': False}
    for host in ('119.97.185.59', '124.70.133.119'):
        client = None
        try:
            with socket.create_connection((host, 7709), timeout=2):
                pass
            client = Quotes.factory(market='std', server=(host, 7709), timeout=3,
                                    auto_retry=False, raise_exception=True)
            frame = client.bars(symbol='600000', frequency=8, start=0, offset=800)
            if frame is None or frame.empty:
                raise ValueError('No quote rows despite TCP connection')
            frame.to_csv(OUT / 'tdx_stock_latest.csv', index=True)
            record['attempts'].append({'host': host, 'quote_rows': len(frame)})
            record['samples']['latest'] = {'rows': len(frame), 'first': str(frame.index[0]), 'last': str(frame.index[-1])}
            older = client.bars(symbol='600000', frequency=8, start=57600, offset=800)
            if older is not None and not older.empty:
                older.to_csv(OUT / 'tdx_stock_offset57600.csv', index=True)
                record['samples']['older'] = {'rows': len(older), 'first': str(older.index[0]), 'last': str(older.index[-1])}
            else:
                record['samples']['older'] = {'rows': 0}
            break
        except Exception as error:
            record['attempts'].append({'host': host, 'error_type': type(error).__name__})
        finally:
            if client is not None:
                client.client.disconnect()
            save_json('tdx', record)


def baostock_worker():
    import baostock as bs
    record = {'source': 'BaoStock', 'not_redis_qmt': True, 'frequency_1m_supported': False,
              'purpose': 'historical universe/daily and 5m diagnostics only',
              'samples': {}, 'certified_for_backtest': False}
    socket.setdefaulttimeout(5)
    try:
        login = bs.login()
        record['login_code'] = login.error_code
        save_json('baostock', record)
        if login.error_code != '0':
            return
        queries = [
            ('historical_universe', lambda: bs.query_all_stock(day='2025-10-10')),
            ('stock_daily', lambda: bs.query_history_k_data_plus('sh.600000',
                'date,code,open,high,low,close,volume,amount,tradestatus,isST',
                start_date='2025-10-10', end_date='2025-10-10', frequency='d', adjustflag='3')),
            ('stock_5m', lambda: bs.query_history_k_data_plus('sh.600000',
                'date,time,code,open,high,low,close,volume,amount',
                start_date='2025-10-10', end_date='2025-10-10', frequency='5', adjustflag='3')),
        ]
        for name, query in queries:
            result = query()
            frame = result.get_data()
            frame.to_csv(OUT / ('baostock_' + name + '.csv'), index=False)
            record['samples'][name] = {'error_code': result.error_code, 'rows': len(frame),
                                       'fields': list(frame.columns),
                                       'first': frame.iloc[0].to_dict() if len(frame) else None}
            save_json('baostock', record)
        bs.logout()
    except Exception as error:
        record['error_type'] = type(error).__name__
    finally:
        save_json('baostock', record)


def bounded_worker(name):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', name],
                                capture_output=True, timeout=40)
        status = {'worker': name, 'returncode': result.returncode}
    except subprocess.TimeoutExpired:
        status = {'worker': name, 'timeout_seconds': 40}
    save_json(name + '_process', status)
    return status


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        {'tdx': tdx_worker, 'baostock': baostock_worker}[sys.argv[2]]()
        return
    if (OUT / 'summary.json').exists():
        print((OUT / 'summary.json').read_text(encoding='utf-8'))
        return
    tasks = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for symbol in ('sh600000', 'sh510300'):
            tasks.append(pool.submit(http_probe, 'tencent_minute_' + symbol,
                'https://ifzq.gtimg.cn/appstock/app/kline/mkline', {'param': symbol + ',m1,,320'}))
            tasks.append(pool.submit(http_probe, 'tencent_daily_' + symbol,
                'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get',
                {'param': symbol + ',day,2025-10-10,2026-10-09,400,'}))
            tasks.append(pool.submit(http_probe, 'sina_minute_' + symbol,
                'https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData',
                {'symbol': symbol, 'scale': '1', 'ma': 'no', 'datalen': '1970'}))
        tasks.append(pool.submit(bounded_worker, 'tdx'))
        tasks.append(pool.submit(bounded_worker, 'baostock'))
        records = [task.result() for task in tasks]
    summary = {'retrieved_date_shanghai': '2026-11-02', 'redis_calls': 0, 'orders': 0,
               'certified_for_backtest': False, 'records': records}
    save_json('summary', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
