# -*- coding: utf-8 -*-
"""Independent weak-market trend rotation across five T+0 ETFs."""
import pandas as pd


CODES = ("159980.SZ", "159981.SZ", "159985.SZ", "518880.SH", "513100.SH")
MAX_ENTRIES_PER_CODE = 2
STOP = .04
PROFIT = .06
HOLD_DAYS = 5


def daily_signals(daily, scheme):
    result = {}
    for code in CODES:
        frame = daily[code].sort_index()
        ma20 = frame.close.rolling(20, min_periods=20).mean()
        ma50 = frame.close.rolling(50, min_periods=50).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        volatility = frame.close.pct_change(fill_method=None).rolling(20, min_periods=20).std()
        amount20 = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(ma20) & momentum.gt(0) & amount20.ge(3e7)
        score = momentum if scheme == "raw60" else momentum / volatility.replace(0, float("nan"))
        result[code] = pd.DataFrame(dict(prior_ma20=ma20.shift(), prior_ma50=ma50.shift(),
            prior_good=good.shift(fill_value=False), prior_score=score.shift()), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_score.where(result[code].prior_good) for code in CODES})
    leader = scores.fillna(-float("inf")).idxmax(axis=1)
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
    if row["close"] < row["prior_ma50"]:
        return "trend_exit"
    if age >= HOLD_DAYS * 240 and time >= "145000":
        return "holding_expiry"
    return ""
