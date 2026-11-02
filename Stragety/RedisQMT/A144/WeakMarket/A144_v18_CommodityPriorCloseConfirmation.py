# -*- coding: utf-8 -*-
"""Commodity trend entry requires completed-minute recovery of the prior daily close.

Research uses this800k account alongside the frozen200k cross-asset rebound book.
No sibling strategy imports.
"""
import pandas as pd


CODES = ('159980.SZ', '159981.SZ', '159985.SZ')
HOLD_DAYS = 10
STOP = .04
PROFIT = .06


def gates(macro, dates):
    calendar = macro.drop_duplicates('date').sort_values('date').set_index('date')
    prior_down = (~calendar.market_strict).shift()
    return {date: bool(prior_down.loc[date]) for date in dates}


def daily_signals(daily):
    result = {}
    for code in CODES:
        frame = daily[code].sort_index()
        average = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        amount = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(average) & momentum.gt(0) & amount.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=average.shift(),
            prior_momentum=momentum.shift(), prior_good=good.shift(fill_value=False),
            prior_entry_close=frame.close.shift()), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good)
                           for code in CODES})
    leader = scores.fillna(-float('inf')).idxmax(axis=1)
    for code in CODES:
        result[code]['prior_leader'] = result[code].prior_good & leader.eq(code)
    return result


def features(bars):
    return bars.copy()


def entry(row, time, enabled, unused):
    return bool(enabled and '093100' <= time <= '094000' and row['prior_leader']
                and row['close'] >= row['prior_ma20'] and row['close'] >= row['prior_entry_close'])


def exit_reason(position, row, time, age, unused=None):
    change = row['close']/(position['buy_notional']/position['bought'])-1
    if change <= -STOP:
        return 'stop'
    if change >= PROFIT:
        return 'profit'
    if age >= HOLD_DAYS*240 and time >= '145000':
        return 'holding_expiry'
    return ''
