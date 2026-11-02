# -*- coding: utf-8 -*-
"""Independent commodity ETF trend entry with fixed stop/profit/time exits."""
import pandas as pd


CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
MAX_ENTRIES_PER_CODE = 2
STOP = .04
PROFIT = .06


def daily_signals(daily):
    result = {}
    for code in CODES:
        frame = daily[code].sort_index()
        average = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        amount = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(average) & momentum.gt(0) & amount.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=average.shift(),
            prior_momentum=momentum.shift(), prior_good=good.shift(fill_value=False)), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good) for code in CODES})
    leader = scores.fillna(-float("inf")).idxmax(axis=1)
    for code in CODES:
        result[code]["prior_leader"] = result[code].prior_good & leader.eq(code)
    return result


def features(bars):
    return bars.copy()


def entry(row, time, enabled, unused):
    return bool(enabled and "093100" <= time <= "094000" and row["prior_leader"] and
                row["close"] >= row["prior_ma20"])


def exit_reason(position, row, time, age, hold_days):
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -STOP:
        return "stop"
    if change >= PROFIT:
        return "profit"
    if age >= hold_days * 240 and time >= "145000":
        return "holding_expiry"
    return ""
