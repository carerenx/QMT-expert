"""Train-only conditional net-edge estimates and fixed trading-horizon paired T.

Forward prices/dividends appear only in training labels, never enrich/policy.
The label is a counterfactual minute proxy, not a continuous-account profit.
"""
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from .dayt_regime_minute_lab import POLICIES, fee


HORIZONS = (30, 60, 240, 720)
CANDIDATES = ('HOLD', 'SYMMETRIC', 'ALWAYS_LONG_240', 'ALWAYS_SHORT_240',
              'EMPIRICAL_MEAN', 'EMPIRICAL_SCENE', 'EMPIRICAL_CONTEXT')
ROBUST = ('EMPIRICAL_SCENE', 'EMPIRICAL_CONTEXT')


def enrich(frame):
    result = frame.copy()
    turn = np.where(result['ret3'] * result['ret20'] >= 0, 'continue', 'reverse')
    stretch = np.where(result['vwap_gap'].abs() / result['atr_pct'] >= 0.25, 'far', 'near')
    daily = np.where(result['daily_up'], 'dailyup', 'dailydown')
    result['context'] = result['regime'] + '|' + turn + '|' + stretch + '|' + daily
    return result


def eligible(row, horizon):
    mode = row['regime']
    if mode in ('UNKNOWN', 'ILLIQUID'):
        return False
    minute = row['minute']
    if horizon >= 240:
        return minute == '0951'
    clock = int(minute[:2]) * 60 + int(minute[2:])
    first = 9 * 60 + 51 if clock < 12 * 60 else 13 * 60 + 21
    end = 11 * 60 + 27 if clock < 12 * 60 else 14 * 60 + 50
    return clock >= first and (clock - first) % (horizon + 3) == 0 and clock + horizon <= end and minute <= '1420'


def label_examples(frame, symbol, capital, cutoff):
    """Purge labels not settled by cutoff; no overlapping same-stock/horizon returns."""
    records = frame.to_dict('records')
    stamps = frame.index.tolist()
    base = int(capital * 0.5 / records[0]['open'] / 100) * 100
    quantity = int(base * 0.20 / 100) * 100
    if quantity < 100:
        return pd.DataFrame()
    day_order = {day: index for index, day in enumerate(frame['date'].unique())}
    labels = []
    for horizon in HORIZONS:
        last_exit = -1
        for index, row in enumerate(records):
            if not eligible(row, horizon):
                continue
            if horizon == 720 and day_order[row['date']] % 3 != 0:
                continue
            entry_index = index + 3
            exit_index = index + horizon + 3
            if exit_index >= len(records) or stamps[exit_index][:8] > cutoff:
                continue
            entry = records[entry_index]
            exit_row = records[exit_index]
            if entry['session'] != row['session'] or entry_index < last_exit:
                continue
            if horizon < 240 and exit_row['session'] != row['session']:
                continue
            last_exit = exit_index
            entry_price = float(entry['open'])
            exit_price = float(exit_row['open'])
            dividends = float(exit_row['cum_cash_action'] - entry['cum_cash_action'])
            long_gross = (exit_price - entry_price + dividends) / entry_price
            for side in (1, -1):
                entry_fee = fee(side * quantity, entry_price)
                exit_fee = fee(-side * quantity, exit_price)
                costs = (entry_fee + exit_fee + quantity * (entry_price + exit_price) * 0.002) / (quantity * entry_price)
                gross = side * long_gross
                labels.append(dict(symbol=symbol, date=row['date'], time=stamps[index],
                                   entry_time=stamps[entry_index], label_end=stamps[exit_index],
                                   regime=row['regime'], context=row['context'], horizon=horizon,
                                   side=side, gross=gross, cost_rate=costs, net=gross - costs,
                                   quantity=quantity, capital=capital))
    return pd.DataFrame(labels)


def fit_model(labels, variant, cutoff):
    if labels.empty:
        return dict(variant=variant, cutoff=cutoff, rules={}, diagnostics=[])
    assert (labels['label_end'].str[:8] <= cutoff).all(), 'unsettled training label'
    key = 'context' if variant != 'EMPIRICAL_SCENE' else 'regime'
    robust = variant != 'EMPIRICAL_MEAN'
    training_days = sorted(labels['date'].unique())
    midpoint = training_days[len(training_days) // 2]
    rules = {}
    diagnostics = []
    for values, subset in labels.groupby([key, 'horizon', 'side'], sort=True):
        state = values[0]
        horizon = int(values[1])
        side = int(values[2])
        # Equal-weight stocks inside date, then equal-weight calendar clusters.
        stock_days = subset.groupby(['date', 'symbol'])[['net', 'gross']].mean()
        daily = stock_days.groupby('date').mean()
        count = len(daily)
        net = float(daily['net'].mean())
        gross = float(daily['gross'].mean())
        se_net = float(daily['net'].std(ddof=1) / math.sqrt(count)) if count > 1 else math.inf
        se_gross = float(daily['gross'].std(ddof=1) / math.sqrt(count)) if count > 1 else math.inf
        lower = net - 2.58 * se_net if robust else net
        gross_lower = gross - 2.58 * se_gross if robust else gross
        stocks_positive = int((subset.groupby('symbol')['net'].mean() > 0).sum())
        early = subset.loc[subset['date'] < midpoint, 'net']
        late = subset.loc[subset['date'] >= midpoint, 'net']
        enough = (len(subset) >= 30 and count >= 20 and stocks_positive >= 3
                  and not early.empty and not late.empty and early.mean() > 0 and late.mean() > 0) if robust else (len(subset) >= 10 and count >= 5)
        qualified = bool(enough and lower >= 0.001)
        item = dict(state=state, horizon=horizon, side=side, examples=len(subset), days=count,
                    stocks=int(subset['symbol'].nunique()), positive_stocks=stocks_positive,
                    net_mean=net, gross_mean=gross, net_lower=lower if math.isfinite(lower) else None,
                    gross_lower=gross_lower if math.isfinite(gross_lower) else None,
                    positive_example_fraction=float((subset['net'] > 0).mean()),
                    positive_day_fraction=float((daily['net'] > 0).mean()), qualified=qualified,
                    last_label_end=str(subset['label_end'].max()))
        diagnostics.append(item)
        if qualified:
            rules.setdefault(state, []).append(item)
    for state in rules:
        rules[state].sort(key=lambda item: (-item['net_lower'], item['horizon'], -item['side']))
    return dict(variant=variant, cutoff=cutoff, rules=rules, diagnostics=diagnostics)


@dataclass(frozen=True)
class EmpiricalPolicy:
    name: str
    model: dict
    blocked_scenes: tuple = ()
    distance: float = 0.30
    max_minutes: int = 10_000_000

    def select(self, row):
        if row['regime'] in self.blocked_scenes:
            return None
        key = row['regime'] if self.name == 'EMPIRICAL_SCENE' else row['context']
        for item in self.model['rules'].get(key, []):
            if eligible(row, item['horizon']):
                return item
        return None

    def entry(self, row):
        item = self.select(row)
        return item['side'] if item is not None else 0

    def execution_terms(self, signal, quantity, price, direction):
        item = self.select(signal)
        if item is None or item['side'] != direction:
            return None
        # Quotes are known at execution; this guard is not a future label input.
        if abs(price / signal['close'] - 1) > 0.005:
            return None
        broker = fee(quantity, price) + fee(-quantity, price)
        cost_rate = broker / (quantity * price) + 0.004
        expected_net_lower = item['gross_lower'] - cost_rate
        if expected_net_lower < 0.001:
            return None
        return dict(target_pct=1000.0, stop_pct=1000.0, max_trading_bars=item['horizon'],
                    training_cutoff=self.model['cutoff'], training_last_label=item['last_label_end'],
                    state=item['state'], forecast_side=item['side'], forecast_net_lower=expected_net_lower,
                    forecast_gross_lower=item['gross_lower'], estimated_round_trip=cost_rate,
                    positive_example_fraction=item['positive_example_fraction'])


@dataclass(frozen=True)
class FixedHorizonPolicy:
    name: str
    side: int
    distance: float = 0.30
    max_minutes: int = 10_000_000

    def entry(self, row):
        return self.side if eligible(row, 240) else 0

    def execution_terms(self, signal, quantity, price, direction):
        return dict(target_pct=1000.0, stop_pct=1000.0, max_trading_bars=240)


def catalog(models, blocked=None):
    result = dict(POLICIES)
    result['ALWAYS_LONG_240'] = FixedHorizonPolicy('ALWAYS_LONG_240', 1)
    result['ALWAYS_SHORT_240'] = FixedHorizonPolicy('ALWAYS_SHORT_240', -1)
    for name, model in models.items():
        result[name] = EmpiricalPolicy(name, model, tuple((blocked or {}).get(name, [])))
    return result
