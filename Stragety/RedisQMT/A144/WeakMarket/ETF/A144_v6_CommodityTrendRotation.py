# -*- coding: utf-8 -*-
"""Independent commodity ETF relative trend with causal minute exits."""
import pandas as pd

CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
MAX_ENTRIES_PER_CODE = 2
STOP = .04
PROFIT = .06
HOLD_DAYS = 5


def daily_signals(daily, window):
    result = {}
    for code in CODES:
        frame = daily[code].sort_index().copy()
        ma20 = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(window, fill_method=None)
        amount20 = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(ma20) & momentum.gt(0) & amount20.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=ma20.shift(), prior_momentum=momentum.shift(),
            prior_good=good.shift(fill_value=False)), index=frame.index)
    score = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good) for code in CODES})
    # CODES order is the fixed tie-breaker; all-invalid rows still buy nothing.
    leader = score.fillna(-float("inf")).idxmax(axis=1)
    for code in CODES:
        result[code]["prior_leader"] = result[code].prior_good & leader.eq(code)
    return result


def features(bars):
    return bars.copy()


def entry(row, time, enabled, unused):
    return bool(enabled and "093100" <= time <= "094000" and row["prior_leader"] and
                row["close"] >= row["prior_ma20"])


def exit_reason(position, row, time, age):
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -STOP:
        return "stop"
    if change >= PROFIT:
        return "profit"
    if not row["prior_leader"]:
        return "rank_exit"
    if row["close"] < row["prior_ma20"]:
        return "trend_exit"
    if age >= HOLD_DAYS * 240 and time >= "145000":
        return "holding_expiry"
    return ""
