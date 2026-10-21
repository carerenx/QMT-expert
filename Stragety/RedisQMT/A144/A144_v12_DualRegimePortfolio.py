#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CSI500 leadership primary plus seven-ETF weak-market secondary.

Independent implementation of the accepted v10/v25 research combination.
Only shared core modules are imported; no earlier strategy is imported.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.A144.core.trend import features

STRATEGY = "A144V12"
COMMODITY_CODES = ('159980.SZ', '159981.SZ', '159985.SZ')
REVERSION_CODES = ('518880.SH', '513100.SH', '513500.SH', '159920.SZ')
CODES = COMMODITY_CODES + REVERSION_CODES
MAX_PRIMARY_POSITIONS = 5
MAX_PRIMARY_ENTRIES = 2
MAX_SECONDARY_ENTRIES = 2
BREADTH_MIN = .40
MIN_AVERAGE_TURNOVER = 3e7
PRIMARY_LEADER_RANK = .95
PRIMARY_MIN_EFFICIENCY = .30
PRIMARY_EFFICIENCY_WEIGHT = .30
PRIMARY_BREADTH_LOOKBACK = 5
PRIMARY_BREADTH_MIN_CHANGE = .05
PRIMARY_ENTRY_ATR_MAX = .06
PRIMARY_TRAIL_ATR = 5.
PRIMARY_HARD_STOP = .10
PRIMARY_MAX_WEIGHT = .20
PRIMARY_RISK_BUDGET = .06
PRIMARY_EXIT_RANK = .85
PRIMARY_PROFIT_ARM_1 = .15
PRIMARY_PROFIT_LOCK_1 = .02
PRIMARY_PROFIT_ARM_2 = .30
PRIMARY_PROFIT_LOCK_2 = .10
SECONDARY_WEIGHT = 1.
SECONDARY_STOP = .04
COMMODITY_PROFIT = .06
COMMODITY_HOLD_DAYS = 10
REVERSION_MA_DAYS = 200
REVERSION_RSI_MAX = 10
REVERSION_PROFIT = .02
REVERSION_HOLD_DAYS = 5
REVERSION_RECOVERY_MA_DAYS = 5
SECONDARY_ENTRY_START = '093100'
SECONDARY_ENTRY_END = '094000'
MEAN_EXIT_SLIPPAGE = .003
MEAN_EXIT_FEE_RATE = .0001
MEAN_EXIT_MIN_FEE = 5.


def primary_signals(frame, benchmark, memberships=None):
    result = features(frame, benchmark)
    if memberships is not None:
        member = pd.Series([row.code in memberships.get(row.date, set())
                            for row in result.itertuples()], index=result.index)
        for field in ('mom60', 'efficiency'):
            result[field + '_rank'] = result[field].where(member).groupby(result.date).rank(pct=True)
        breadth = result.loc[member].assign(above=lambda rows: rows.adj_close.gt(rows.ma50)).groupby('date').above.mean()
        result['breadth'] = result.date.map(breadth)
        result['member'] = member
    trend = (result.adj_close.gt(result.ma50) & result.ma20.gt(result.ma50)
             & result.ma50.gt(result.ma50_lag5) & result.mom60.gt(0)
             & result.mom60_rank.ge(.7))
    daily_breadth = result.groupby('date').breadth.first().sort_index()
    confirmed = (daily_breadth - daily_breadth.shift(PRIMARY_BREADTH_LOOKBACK)).round(12).ge(PRIMARY_BREADTH_MIN_CHANGE)
    result['entry'] = (trend & result.mom60_rank.ge(PRIMARY_LEADER_RANK) & result.adj_close.gt(result.ma20)
                       & result.market_strict & result.breadth.ge(BREADTH_MIN)
                       & result.age.ge(130) & result.amount20.ge(MIN_AVERAGE_TURNOVER)
                       & result.atr_pct.between(.01, PRIMARY_ENTRY_ATR_MAX) & result.ret.lt(.095)
                       & result.efficiency.ge(PRIMARY_MIN_EFFICIENCY) & result.date.map(confirmed))
    if memberships is not None:
        result['entry'] &= result['member']
    result['score'] = (1 - PRIMARY_EFFICIENCY_WEIGHT) * result.mom60_rank + PRIMARY_EFFICIENCY_WEIGHT * result.efficiency_rank
    result['weight'] = (PRIMARY_RISK_BUDGET / (PRIMARY_TRAIL_ATR * result.atr_pct)).clip(upper=PRIMARY_MAX_WEIGHT)
    return result


def primary_exit(position, row, history):
    # Recompute the adjusted entry/peak with today's adjustment basis.
    if position['entry_date'] > str(row['date']):
        return ''  # The entry session has not completed yet (stock T+1).
    held = history.loc[history.date.ge(position['entry_date']) & history.date.le(row['date'])]
    if held.empty:
        raise ValueError('Primary entry is outside supplied daily history')
    first = held.iloc[0]
    entry = position['buy_notional'] / position['bought'] * first.adj_close / first.close
    peak = max(entry, float(held.adj_close.max()))
    close = row['adj_close']
    if close <= entry * (1 - PRIMARY_HARD_STOP):
        return 'hard-stop'
    if not row['market_strict']:
        return 'index-stress'
    peak_gain = round(peak / entry - 1, 12)
    floor = PRIMARY_PROFIT_LOCK_2 if peak_gain >= PRIMARY_PROFIT_ARM_2 else (PRIMARY_PROFIT_LOCK_1 if peak_gain >= PRIMARY_PROFIT_ARM_1 else None)
    if floor is not None and round(close / entry - 1, 12) <= floor:
        return 'profit-floor'
    if close <= peak - PRIMARY_TRAIL_ATR * row['atr']:
        return 'atr-trail'
    if close < row['ma50']:
        return 'trend-break'
    if row['mom60_rank'] < PRIMARY_EXIT_RANK:
        return 'leadership-lost'
    return ''


def secondary_signals(daily, index_down):
    result = {}
    for code in COMMODITY_CODES:
        frame = daily[code]
        average = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        good = frame.close.gt(average) & momentum.gt(0) & frame.amount.rolling(20).mean().ge(MIN_AVERAGE_TURNOVER)
        result[code] = dict(ma20=float(average.iloc[-1]), score=float(momentum.iloc[-1]),
                            good=bool(good.iloc[-1]), index_down=index_down, is_reversion=False)
    eligible = [code for code in COMMODITY_CODES if result[code]['good']]
    winner = max(eligible, key=lambda code: result[code]['score']) if eligible else None
    for code in COMMODITY_CODES:
        result[code]['leader'] = code == winner
    for code in REVERSION_CODES:
        frame = daily[code]
        delta = frame.close.diff()
        gain = delta.clip(lower=0).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        rsi = 100 * gain / (gain + loss).replace(0, float('nan'))
        good = (frame.close.gt(frame.close.rolling(REVERSION_MA_DAYS).mean()) & rsi.lt(REVERSION_RSI_MAX)
                & frame.amount.rolling(20).mean().ge(MIN_AVERAGE_TURNOVER))
        result[code] = dict(score=float(rsi.iloc[-1]), good=bool(good.iloc[-1]),
                            prior_close=float(frame.close.iloc[-1]),
                            recovery_ma5=float(frame.close.tail(REVERSION_RECOVERY_MA_DAYS).mean()), is_reversion=True)
    eligible = [code for code in REVERSION_CODES if result[code]['good']]
    winner = min(eligible, key=lambda code: result[code]['score']) if eligible else None
    for code in REVERSION_CODES:
        result[code]['leader'] = code == winner
    return result


def secondary_entry(signal, bar, midpoint, time, enabled):
    if not enabled or not SECONDARY_ENTRY_START <= time <= SECONDARY_ENTRY_END or not signal['leader']:
        return False
    if signal['is_reversion']:
        return bar['close'] >= signal['prior_close']
    return signal['index_down'] and bar['close'] >= signal['ma20'] and bar['close'] >= midpoint


def secondary_exit(position, signal, bar, time, age):
    change = bar['close'] / (position['buy_notional'] / position['bought']) - 1
    reversion = signal['is_reversion']
    if change <= -SECONDARY_STOP:
        return 'stop'
    if change >= (REVERSION_PROFIT if reversion else COMMODITY_PROFIT):
        return 'profit'
    proceeds = math.floor(bar['low'] * (1 - MEAN_EXIT_SLIPPAGE) * 1000 + 1e-8) / 1000 * position['quantity']
    net = (position['sell_notional'] - position['sell_fee'] + proceeds - max(MEAN_EXIT_MIN_FEE, proceeds * MEAN_EXIT_FEE_RATE)
           - position['buy_notional'] - position['buy_fee'])
    if reversion and bar['close'] >= signal['recovery_ma5'] and net > 0:
        return 'mean_recovery'
    if age >= (REVERSION_HOLD_DAYS if reversion else COMMODITY_HOLD_DAYS) * 240 and time >= '145000':
        return 'holding_expiry'
    return ''


def main(argv=None):
    from Stragety.RedisQMT.A144.core.dual_regime_runtime import run
    return run(sys.modules[__name__], argv)


if __name__ == '__main__':
    raise SystemExit(main())
