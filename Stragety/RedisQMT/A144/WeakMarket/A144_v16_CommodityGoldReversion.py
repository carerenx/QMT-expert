# -*- coding: utf-8 -*-
"""Standalone gold pullback sleeve alongside a separately funded commodity trend sleeve.

The runner freezes the original800k commodity book. This independent policy
defines the new200k gold sleeve; it imports no sibling strategy versions.
"""
import pandas as pd


GOLD_CODE = '518880.SH'
GOLD_CASH = 200000.
COMMODITY_CASH = 800000.
GOLD_STOP = .04
GOLD_PROFIT = .02
GOLD_HOLD_DAYS = 5
GOLD_RSI_LIMIT = 10
GOLD_TREND_DAYS = 200


def gold_gates(macro, trade_dates):
    calendar = macro.drop_duplicates('date').sort_values('date').set_index('date')
    weak = (~calendar.market_strict | calendar.breadth.lt(.4)).shift()
    return {date: bool(weak.loc[date]) for date in trade_dates}


def gold_signals(daily):
    frame = daily[GOLD_CODE].sort_index()
    delta = frame.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=.5, adjust=False, min_periods=20).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=.5, adjust=False, min_periods=20).mean()
    rsi = 100*gain/(gain+loss).replace(0, float('nan'))
    average = frame.close.rolling(GOLD_TREND_DAYS, min_periods=GOLD_TREND_DAYS).mean()
    good = (frame.close.gt(average) & rsi.lt(GOLD_RSI_LIMIT)
            & frame.amount.rolling(20, min_periods=20).mean().ge(3e7))
    signals = pd.DataFrame(dict(prior_gold_good=good.shift(fill_value=False),
        prior_gold_rsi=rsi.shift(), prior_gold_ma200=average.shift(),
        prior_gold_close=frame.close.shift()), index=frame.index)
    return {GOLD_CODE: signals}


def gold_features(bars):
    return bars.copy()


def gold_entry(row, time, enabled, unused):
    return bool(enabled and '093100' <= time <= '094000' and row['prior_gold_good']
                and row['close'] >= row['prior_gold_close'])


def gold_exit(position, row, time, age, unused=None):
    change = row['close']/(position['buy_notional']/position['bought'])-1
    if change <= -GOLD_STOP:
        return 'stop'
    if change >= GOLD_PROFIT:
        return 'profit'
    if age >= GOLD_HOLD_DAYS*240 and time >= '145000':
        return 'holding_expiry'
    return ''
