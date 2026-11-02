# -*- coding: utf-8 -*-
"""Independent shared-cash secondary with cost-aware prior-MA5 rebound exits."""
import math

import pandas as pd


COMMODITY_CODES = ('159980.SZ', '159981.SZ', '159985.SZ')
REVERSION_CODES = ('518880.SH', '513100.SH')
CODES = COMMODITY_CODES+REVERSION_CODES
WEIGHT = 1.
MAX_ENTRIES_PER_CODE = 2


def gates(macro, dates):
    calendar = macro.drop_duplicates('date').sort_values('date').set_index('date')
    weak = (~calendar.market_strict | calendar.breadth.lt(.4)).shift()
    return {date: bool(weak.loc[date]) for date in dates}


def daily_signals(daily, macro):
    calendar = macro.drop_duplicates('date').sort_values('date').set_index('date')
    prior_down = (~calendar.market_strict).shift()
    result = {}
    for code in COMMODITY_CODES:
        frame = daily[code].sort_index()
        average = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        amount = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(average) & momentum.gt(0) & amount.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=average.shift(),
            prior_momentum=momentum.shift(), prior_good=good.shift(fill_value=False)), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good)
                           for code in COMMODITY_CODES})
    leader = scores.fillna(-float('inf')).idxmax(axis=1)
    for code in COMMODITY_CODES:
        result[code]['prior_leader'] = result[code].prior_good & leader.eq(code)
        result[code]['prior_index_down'] = prior_down.reindex(result[code].index)
        result[code]['is_reversion'] = False
    for code in REVERSION_CODES:
        frame = daily[code].sort_index()
        delta = frame.close.diff()
        gain = delta.clip(lower=0).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        rsi = 100*gain/(gain+loss).replace(0, float('nan'))
        average = frame.close.rolling(200, min_periods=200).mean()
        good = frame.close.gt(average) & rsi.lt(10) & frame.amount.rolling(20, min_periods=20).mean().ge(3e7)
        result[code] = pd.DataFrame(dict(prior_gold_good=good.shift(fill_value=False),
            prior_gold_rsi=rsi.shift(), prior_gold_ma200=average.shift(),
            prior_gold_close=frame.close.shift(), prior_recovery_ma5=frame.close.rolling(5, min_periods=5).mean().shift()), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_gold_rsi.where(result[code].prior_gold_good)
                           for code in REVERSION_CODES})
    leader = scores.fillna(float('inf')).idxmin(axis=1)
    for code in REVERSION_CODES:
        result[code]['prior_reversion_leader'] = result[code].prior_gold_good & leader.eq(code)
        result[code]['is_reversion'] = True
    return result


def features(bars):
    result = bars.copy()
    result['session_midpoint'] = (result.high.cummax()+result.low.cummin())/2
    return result


def entry(row, time, enabled, unused):
    if not enabled or not '093100' <= time <= '094000':
        return False
    if row['is_reversion']:
        return bool(row['prior_reversion_leader'] and row['close'] >= row['prior_gold_close'])
    return bool(row['prior_index_down'] and row['prior_leader'] and row['close'] >= row['prior_ma20'] and row['close'] >= row['session_midpoint'])


def estimated_mean_exit_net(position, row):
    price = math.floor(row['low']*.997*1000+1e-8)/1000
    proceeds = price*position['quantity']
    future_fee = max(5., proceeds*.0001)
    return (position['sell_notional']-position['sell_fee']+proceeds-future_fee
            -position['buy_notional']-position['buy_fee'])


def exit_reason(position, row, time, age, unused=None):
    change = row['close']/(position['buy_notional']/position['bought'])-1
    profit = .02 if row['is_reversion'] else .06
    hold_days = 5 if row['is_reversion'] else 10
    if change <= -.04:
        return 'stop'
    if change >= profit:
        return 'profit'
    if row['is_reversion'] and row['close'] >= row['prior_recovery_ma5'] and estimated_mean_exit_net(position, row) > 0:
        return 'mean_recovery'
    if age >= hold_days*240 and time >= '145000':
        return 'holding_expiry'
    return ''
