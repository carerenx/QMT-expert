# -*- coding: utf-8 -*-
"""Independent gold/Nasdaq rebound sleeve beside a separately funded commodity book."""
import pandas as pd


REVERSION_CODES = ('518880.SH', '513100.SH')
REVERSION_CASH = 200000.
COMMODITY_CASH = 800000.
STOP = .04
PROFIT = .02
HOLD_DAYS = 5
RSI_LIMIT = 10
TREND_DAYS = 200


def reversion_gates(macro, trade_dates):
    calendar = macro.drop_duplicates('date').sort_values('date').set_index('date')
    weak = (~calendar.market_strict | calendar.breadth.lt(.4)).shift()
    return {date: bool(weak.loc[date]) for date in trade_dates}


def reversion_signals(daily):
    result = {}
    for code in REVERSION_CODES:
        frame = daily[code].sort_index()
        delta = frame.close.diff()
        gain = delta.clip(lower=0).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=.5, adjust=False, min_periods=20).mean()
        rsi = 100*gain/(gain+loss).replace(0, float('nan'))
        average = frame.close.rolling(TREND_DAYS, min_periods=TREND_DAYS).mean()
        good = (frame.close.gt(average) & rsi.lt(RSI_LIMIT)
                & frame.amount.rolling(20, min_periods=20).mean().ge(3e7))
        result[code] = pd.DataFrame(dict(prior_gold_good=good.shift(fill_value=False),
            prior_gold_rsi=rsi.shift(), prior_gold_ma200=average.shift(),
            prior_gold_close=frame.close.shift()), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_gold_rsi.where(result[code].prior_gold_good)
                           for code in REVERSION_CODES})
    leader = scores.fillna(float('inf')).idxmin(axis=1)
    for code in REVERSION_CODES:
        result[code]['prior_reversion_leader'] = result[code].prior_gold_good & leader.eq(code)
    return result


def reversion_features(bars):
    return bars.copy()


def reversion_entry(row, time, enabled, unused):
    return bool(enabled and '093100' <= time <= '094000' and row['prior_reversion_leader']
                and row['close'] >= row['prior_gold_close'])


def reversion_exit(position, row, time, age, unused=None):
    change = row['close']/(position['buy_notional']/position['bought'])-1
    if change <= -STOP:
        return 'stop'
    if change >= PROFIT:
        return 'profit'
    if age >= HOLD_DAYS*240 and time >= '145000':
        return 'holding_expiry'
    return ''
