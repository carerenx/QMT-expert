# -*- coding: utf-8 -*-
"""Offline comparison on the verified 2026-08-04--11-03 v12 window.

Run from the project root:
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant baseline
python -m Stragety.RedisQMT.A144.research.replay_frozen_window --variant month_continuation --full-market

Uses archived raw minutes and daily bars only, never imports a bridge client.
Frozen primary routing is permitted ONLY while primary/entry policy is identical
and the recorded primary candidates are empty. Baseline fills/equity must match
before a candidate is accepted. See ../MonthMomentumTrendContinuation.md.
"""
import argparse
import ast
import hashlib
import importlib.util
import inspect
import json
import math
from pathlib import Path
import sys

import pandas as pd

from ..core.dual_regime_execution import Journal, fee, new_state
from ..core.dual_regime_runtime import Market, TIMES, build_intents, normalize

ROOT = Path(__file__).resolve().parents[4]
CACHE = ROOT / 'output/analysis/a144_v12_recent3m_20261104'
OUT = ROOT / 'output/analysis/a144_v13_optimization_20261104'


def load_policy(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def execution(order, bar, state, slip):
    """Minute-lifetime limit fill; the order's signal uses only prior bars."""
    buy = order['side'] == 'BUY'
    if (buy and bar.low > order['limit']) or (not buy and bar.high < order['limit']):
        return 0, 0.
    price = min(order['limit'], bar.open*(1+slip)) if buy else max(order['limit'], bar.open*(1-slip))
    price = min(float(bar.high), max(float(bar.low), price))
    ticks = 1000 if order['sleeve'] == 'secondary' else 100
    price = (math.ceil(price*ticks-1e-8) if buy else math.floor(price*ticks+1e-8))/ticks
    if (buy and price > order['limit']) or (not buy and price < order['limit']):
        return 0, 0.
    quantity = min(order['quantity'], math.floor(float(bar.volume)*.05)*100)
    if buy:
        while quantity and quantity*price+fee(quantity*price, 'BUY', order['date'], order['sleeve']) > state['cash']:
            quantity -= 100
    else:
        position = state['positions'][order['code']]
        if order['sleeve'] == 'primary' and position['entry_date'] >= order['date']:
            return 0, 0.
        quantity = min(quantity, position['quantity'])
    return quantity, price


def verify_frozen_contract(policy, baseline, secondary_signals_changed=False):
    for name in ['primary_signals', 'primary_exit', 'secondary_signals', 'secondary_entry']:
        if name == 'secondary_signals' and secondary_signals_changed:
            continue
        candidate_source = ast.dump(ast.parse(inspect.getsource(getattr(policy, name))))
        baseline_source = ast.dump(ast.parse(inspect.getsource(getattr(baseline, name))))
        assert candidate_source == baseline_source, name
    changed_exit_constants = {'COMMODITY_PROFIT', 'COMMODITY_HOLD_DAYS', 'SECONDARY_WEIGHT'}
    for name, value in vars(baseline).items():
        if name.isupper() and name not in changed_exit_constants | {'ROOT', 'STRATEGY'}:
            assert getattr(policy, name) == value, name
    source = ast.parse((CACHE / 'replay_v12.py').read_text(encoding='utf-8'))
    original = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'execution')
    assert ast.dump(original) == ast.dump(ast.parse(inspect.getsource(execution)).body[0])
    frozen_hashes = json.loads((OUT / 'baseline_source_hashes.json').read_text())
    for name, digest in frozen_hashes.items():
        if Path(name).name in {'A144_v12_DualRegimePortfolio.py', 'trend.py', 'membership.json'}:
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name


class ArchivedData:
    def __init__(self, policy):
        self.minutes = {}
        self.daily = {}
        self.equity = pd.read_csv(CACHE / 'equity.csv', dtype={'date': str})
        self.sessions = self.equity.date.tolist()
        benchmark = pd.read_pickle(CACHE / 'daily/000905.SH_none.pkl').set_index('stime')
        self.dates = list(benchmark.index)
        self.index_ok = benchmark.close.ge(benchmark.close.rolling(60).mean()).shift(1)
        expected = ~self.equity.set_index('date').weak
        assert self.index_ok.reindex(expected.index).astype(bool).equals(expected)
        diagnostic = json.loads((CACHE / 'primary_diagnostic.json').read_text())
        assert all(row['final_candidates'] == 0 for row in diagnostic['details'])
        assert {row['date'] for row in diagnostic['details']} == set(expected.loc[expected].index)
        for code in policy.CODES:
            frame = pd.read_pickle(CACHE / 'daily' / (code+'_none.pkl'))
            frame.index = frame.stime.astype(str)
            self.daily[code] = normalize(frame, '20261103')
        audit = json.loads((CACHE / 'minute_audit.json').read_text())
        for item in audit:
            path = CACHE / 'minutes' / (item['code']+'_'+item['date']+'.pkl')
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'], path
            frame = pd.read_pickle(path)
            frame.index = frame.stime.astype(str)
            frame = normalize(frame, item['date']+'150000', daily=False)
            assert list(frame.index) == [item['date']+time for time in TIMES]
            self.minutes[(item['code'], item['date'])] = frame
        assert len(self.minutes) == len(policy.CODES)*len(self.sessions)

    def get_market_data_ex(self, field_list, stock_list, period, start_time='', end_time='', **kwargs):
        assert period == '1m'
        dates = [date for date in self.dates if start_time[:8] <= date <= end_time[:8]]
        result = {}
        for code in stock_list:
            frame = pd.concat([self.minutes[(code, date)] for date in dates])
            result[code] = frame.loc[(frame.index >= start_time) & (frame.index <= end_time)]
        return result


class FrozenMarket(Market):
    def prepare(self, today, state):
        assert not any(position['sleeve'] == 'primary' for position in state['positions'].values())
        prior = [date for date in self.xtdata.dates if date < today]
        self.day = today
        self.asof = prior[-1]
        self.dates = prior[-299:] + [today]
        self.weak = not bool(self.xtdata.index_ok.loc[today])
        self.daily = {code: frame.loc[:self.asof].tail(300) for code, frame in self.xtdata.daily.items()}
        self.secondary = self.policy.secondary_signals(self.daily, self.weak)
        self.candidates = pd.DataFrame(columns=['code', 'weight', 'amount20'])
        self.primary = pd.DataFrame(columns=['code', 'date'])


class FullArchivedData(ArchivedData):
    def __init__(self, policy):
        super().__init__(policy)
        self.full_daily = {}
        for path in (CACHE / 'daily').glob('*.pkl'):
            code, adjustment = path.stem.rsplit('_', 1)
            frame = pd.read_pickle(path)
            if not frame.empty:
                frame.index = frame.stime.astype(str)
            self.full_daily[(code, adjustment)] = frame
        self.details = json.loads((OUT / 'ipo_details.json').read_text(encoding='utf-8'))['details']

    def get_trading_dates(self, exchange, end_time, count):
        return [date for date in self.dates if date <= end_time][-count:]

    def get_instrument_detail(self, code):
        return self.details[code]

    def get_market_data_ex(self, field_list, stock_list, period, start_time='', end_time='',
                           count=-1, dividend_type='none', **kwargs):
        if period != '1d':
            return super().get_market_data_ex(field_list, stock_list, period, start_time, end_time, **kwargs)
        result = {}
        for code in stock_list:
            frame = self.full_daily[(code, dividend_type)]
            frame = frame.loc[frame.index <= end_time[:8]]
            result[code] = frame.tail(count) if count > 0 else frame
        return result


def replay(policy, provider, slippage, destination, full_market=False):
    destination.mkdir(parents=True, exist_ok=True)
    state = new_state('offline-only', 90000., policy.STRATEGY)
    journal = Journal(destination / 'not_a_live_state.json', state)
    market_type = Market if full_market else FrozenMarket
    market = market_type(provider, CACHE / 'membership.json', policy)
    equity = []
    fills = []
    for date in provider.sessions:
        market.prepare(date, state)
        assert market.candidates.empty, 'New primary candidates require additional stock minute validation'
        current = {code: provider.minutes[(code, date)] for code in policy.CODES}
        for index, time in enumerate(TIMES):
            if index == 0 or time > '145600':
                continue
            stamp = date+time
            signal = date+TIMES[index-1]
            bars = {code: frame.iloc[:index] for code, frame in current.items()}
            actual = {code: dict(quantity=position['quantity'], sellable=position['quantity'])
                      for code, position in state['positions'].items()}
            intents = build_intents(policy, market, journal, actual, state['cash'], stamp, signal, bars)
            for order in intents:
                quantity, price = execution(order, current[order['code']].loc[stamp], state, slippage)
                if quantity:
                    charge = fee(quantity*price, order['side'], date, order['sleeve'])
                    journal.apply_fill(order, quantity, quantity*price, charge, date)
                    fills.append(dict(order, filled=quantity, price=price, fee=charge))
            state['last_signal'] = signal
            assert state['cash'] >= -1e-6
        value = sum(position['quantity']*float(current[code].iloc[-1].close)
                    for code, position in state['positions'].items())
        ledger = 90000.+sum(fill['notional']-fill['estimated_fee'] if fill['side']=='SELL'
                           else -fill['notional']-fill['estimated_fee'] for fill in state['fills'])
        assert abs(ledger-state['cash']) < 1e-6
        equity.append(dict(date=date, cash=state['cash'], market_value=value, equity=state['cash']+value,
                           positions=len(state['positions']), weak=market.weak, fills=len(fills)))
        if full_market:
            print('FULL', date, 'equity=%.2f' % (state['cash']+value), flush=True)
    curve = pd.DataFrame(equity)
    values = pd.Series([90000.]+curve.equity.tolist())
    summary = dict(strategy=policy.STRATEGY, start=provider.sessions[0], end=provider.sessions[-1],
        initial_cash=90000, sessions=len(curve), slippage=slippage, final_equity=float(values.iloc[-1]),
        profit=float(values.iloc[-1]-90000), total_return=float(values.iloc[-1]/90000.-1),
        max_drawdown=float((values/values.cummax()-1).min()), closed_trades=len(state['trades']),
        winning_trades=sum(trade['pnl']>0 for trade in state['trades']), fill_count=len(fills),
        estimated_fees=sum(fill['fee'] for fill in fills), minute_sessions_validated=len(provider.minutes),
        completed=True, state_positions=state['positions'])
    curve.to_csv(destination / 'equity.csv', index=False)
    pd.DataFrame(fills).to_csv(destination / 'fills.csv', index=False)
    pd.DataFrame(state['trades']).to_csv(destination / 'trades.csv', index=False)
    (destination / 'state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    (destination / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary, curve, pd.DataFrame(fills)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=['baseline', 'reserve20', 'trailing_profit', 'continuation',
                                            'rotation', 'rotation_continuation', 'month_momentum',
                                            'month_continuation'], required=True)
    parser.add_argument('--slippage', type=float, default=.001)
    parser.add_argument('--full-market', action='store_true', help='recompute all stock signals daily, without frozen routing')
    args = parser.parse_args()
    folder = ROOT / 'Stragety/RedisQMT/A144'
    baseline = load_policy(folder / 'A144_v12_DualRegimePortfolio.py', 'frozen_window_baseline')
    if args.variant in {'baseline', 'reserve20'}:
        policy = baseline
        if args.variant == 'reserve20':
            policy.SECONDARY_WEIGHT = .8
    elif args.variant in {'month_momentum', 'month_continuation'}:
        filename = ('A144_v16_MonthMomentumRecovery.py' if args.variant == 'month_momentum'
                    else 'A144_v17_MonthMomentumTrendContinuation.py')
        source = folder / filename if args.variant == 'month_continuation' else OUT / 'candidates' / filename
        policy = load_policy(source, 'frozen_window_candidate')
    elif args.variant in {'rotation', 'rotation_continuation'}:
        filename = ('A144_v14_CommodityLeaderRotation.py' if args.variant == 'rotation'
                    else 'A144_v15_LeaderRotationTrendContinuation.py')
        policy = load_policy(OUT / 'candidates' / filename, 'frozen_window_candidate')
    else:
        policy = load_policy(OUT / 'candidates/A144_v13_CommodityTrendContinuation.py', 'frozen_window_candidate')
        if args.variant == 'trailing_profit':
            policy.COMMODITY_MAX_HOLD_DAYS = policy.COMMODITY_HOLD_DAYS
    verify_frozen_contract(policy, baseline, secondary_signals_changed=args.variant in {'month_momentum', 'month_continuation'})
    provider_type = FullArchivedData if args.full_market else ArchivedData
    provider = provider_type(policy)
    tag = args.variant + ('_stress' if args.slippage != .001 else '')
    if args.full_market:
        tag += '_full'
    summary, curve, fills = replay(policy, provider, args.slippage, OUT / tag, args.full_market)
    if args.full_market:
        reference = OUT / tag.removesuffix('_full')
        pd.testing.assert_frame_equal(curve, pd.read_csv(reference / 'equity.csv', dtype={'date': str}),
                                      check_exact=False, atol=1e-7, rtol=0)
        pd.testing.assert_frame_equal(fills, pd.read_csv(reference / 'fills.csv', dtype={
            'date': str, 'signal': str, 'execution_stamp': str}), check_exact=False, atol=1e-7, rtol=0)
        summary['full_daily_selection_and_all_fills_equal'] = True
        (OUT / tag / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if args.variant == 'baseline' and args.slippage == .001:
        original = pd.read_csv(CACHE / 'equity.csv', dtype={'date': str})
        pd.testing.assert_frame_equal(curve, original, check_exact=False, atol=1e-7, rtol=0)
        pd.testing.assert_frame_equal(fills, pd.read_csv(CACHE / 'fills.csv', dtype={
            'date': str, 'signal': str, 'execution_stamp': str}), check_exact=False, atol=1e-7, rtol=0)
        summary['baseline_equity_and_all_fills_equal'] = True
        (OUT / tag / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
