"""Restart a v39-style strategy each session while preserving the real account."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from output.analysis.compare_v51_v39_minute import Broker, Clock, OUT, load_strategy
from backtest.dayt_strict import StrictBroker


def replay_day(version, history, bars, cash, shares, rate,
               symbol='601869.SH', stock_name='601869', target_base_shares=200,
               target_value=None):
    mod = load_strategy(version)
    broker = StrictBroker(history, bars, rate, symbol, stock_name)
    broker.exchange.cash = cash
    broker.exchange.shares = shares
    broker.exchange.sellable = shares
    logs = []

    class Factory:
        def __new__(cls):
            return broker

        load_daily_snapshot = StrictBroker.load_daily_snapshot
        get_history_data = Broker.get_history_data

    with ExitStack() as stack:
        replacements = [
            (mod, 'MiniQMTConnector', Factory),
            (mod, '_time', Clock),
            (mod, 'datetime', Clock),
            (Clock, 'sleep', broker.sleep),
            (mod, '_log', lambda message: logs.append(str(message))),
            (mod, 'get_logger', lambda: SimpleNamespace(close=lambda: None)),
            (mod, 'set_global_conn', lambda *args: None),
            (mod, 'order_shares', broker.order),
            (mod, 'get_trade_detail_data', lambda account, asset, kind:
                broker.query_positions() if kind == 'POSITION' else [broker.query_account()]),
            (mod.cfg, 'now_hms', lambda: Clock.current.strftime('%H:%M:%S')),
        ]
        symbol_code = symbol.split('.')[0]
        for obj, name, value in (
                (mod, 'STOCK_CODE', symbol_code),
                (mod, 'STOCK_QMT', symbol),
                (mod, 'STOCK_NAME', stock_name),
                (mod.cfg, 'STOCK_CODE', symbol_code),
                (mod.cfg, 'STOCK_QMT', symbol),
                (mod.cfg, 'STOCK_NAME', stock_name)):
            if hasattr(obj, name):
                replacements.append((obj, name, value))
        if hasattr(mod, 'TARGET_BASE_SHARES'):
            replacements.append((mod, 'TARGET_BASE_SHARES', target_base_shares))
        for obj, name, value in replacements:
            stack.enter_context(patch.object(obj, name, value))
        runner = mod.StrategyRunner(False)
        original_submit = runner._submit_order
        original_clamp_buy = runner._clamp_buy_shares

        def clamp_buy(quantity, price):
            executable = original_clamp_buy(quantity, price)
            return executable // 100 * 100

        def submit(quantity, price, label, style='COMPETE'):
            broker.label = label
            if target_value is not None and label in (
                    'REV-T sell', 'FWD-T buy', 'MOM short', 'MOM long'):
                lots = max(1, int(float(target_value) / float(price) / 100))
                quantity = (1 if quantity > 0 else -1) * lots * 100
            return original_submit(quantity, price, label, style)

        stack.enter_context(patch.object(runner, '_submit_order', submit))
        stack.enter_context(patch.object(runner, '_clamp_buy_shares', clamp_buy))
        broker.current_day = broker.events[0][2]
        broker.daily = broker.all_daily.loc[
            broker.all_daily.index < broker.current_day]
        generator = runner.run()
        failure = None
        try:
            while broker.cursor < len(broker.events):
                phase = broker.advance()
                if phase not in ('CLOSE', 'PREOPEN'):
                    continue
                try:
                    next(generator)
                except StopIteration:
                    failure = 'strategy loop ended at {}'.format(
                        Clock.current.isoformat())
                    break
                errors = [line for line in logs[-20:]
                          if '[ERROR]' in line or 'init failed' in line]
                if errors:
                    failure = str(errors[-1])
                    break
        finally:
            generator.close()
        while broker.cursor < len(broker.events):
            broker.advance()
        close = float(bars.iloc[-1].close)
        discarded = {
            'short': list(runner.st.get('short_legs', [])),
            'long': list(runner.st.get('long_legs', [])),
            'mom_shares': int(runner.st.get('mom_leg_shares', 0) or 0),
            'state': runner.st.get('fstate'),
        }
        return {
            'date': bars.index[0][:8],
            'failure': failure,
            'start_cash': cash,
            'start_position': shares,
            'end_cash': broker.cash,
            'end_position': broker.position,
            'close': close,
            'end_equity': broker.cash + broker.position * close,
            'fees': sum(order.fee for order in broker.exchange.orders.values()),
            'orders': [vars(order) for order in broker.exchange.orders.values()],
            'trades': broker.exchange.fills,
            'rejections': broker.exchange.rejections,
            'completed_cycle_gross': list(broker.closed),
            'discarded_strategy_state': discarded,
        }


def replay(version, daily, minute, rate=0.0005,
           symbol='601869.SH', stock_name='601869', minimum_bars=230,
           initial_cash=100000.0, initial_shares=200, target_value=None):
    cash = initial_cash
    shares = initial_shares
    initial_open = float(minute.iloc[0].open)
    days = []
    for date, bars in minute.groupby(minute.index.str[:8]):
        history = daily.loc[daily.index < date]
        if len(bars) < minimum_bars or len(history) < 80:
            continue
        result = replay_day(version, history, bars, cash, shares, rate,
                            symbol, stock_name, initial_shares, target_value)
        days.append(result)
        if result['failure']:
            break
        cash = result['end_cash']
        shares = result['end_position']
    last_close = float(minute.iloc[-1].close)
    final_equity = cash + shares * last_close
    initial_equity = initial_cash + initial_shares * initial_open
    hold_final = initial_cash + initial_shares * last_close
    return {
        'model': 'DAILY_STRATEGY_RESTART_CONTINUOUS_ACCOUNT',
        'version': version,
        'rate': rate,
        'failure': next((day['failure'] for day in days if day['failure']), None),
        'initial_cash': initial_cash,
        'initial_position': initial_shares,
        'initial_equity': initial_equity,
        'final_cash': cash,
        'final_position': shares,
        'final_equity': final_equity,
        'account_net': final_equity - initial_equity,
        'hold_final': hold_final,
        'excess_net': final_equity - hold_final,
        'fees': sum(day['fees'] for day in days),
        'sessions': len(days),
        'days': days,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='v39_nomom')
    parser.add_argument('--start', default='20260801')
    parser.add_argument('--end', default='20260912')
    parser.add_argument('--rate', default=0.0005, type=float)
    parser.add_argument('--symbol', default='601869.SH')
    parser.add_argument('--stock-name', default='601869')
    parser.add_argument('--data-dir', type=Path, default=OUT)
    parser.add_argument('--minimum-bars', type=int, default=230)
    parser.add_argument('--initial-cash', type=float, default=100000.0)
    parser.add_argument('--initial-shares', type=int, default=200)
    parser.add_argument('--target-value', type=float)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('refusing to overwrite {}'.format(args.output))
    daily = pd.read_csv(
        args.data_dir / '1d.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = pd.read_csv(
        args.data_dir / '1m.csv', dtype={'time': str}).set_index('time').sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= args.start) & (dates <= args.end)]
    if minute.empty:
        raise ValueError('no minute data in requested range')
    result = replay(args.version, daily, minute, args.rate,
                    args.symbol, args.stock_name, args.minimum_bars,
                    args.initial_cash, args.initial_shares, args.target_value)
    result['symbol'] = args.symbol
    result['stock_name'] = args.stock_name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    summary_keys = [
        'failure', 'sessions', 'final_cash', 'final_position', 'final_equity',
        'account_net', 'excess_net', 'fees']
    print({key: result[key] for key in summary_keys})


if __name__ == '__main__':
    main()
