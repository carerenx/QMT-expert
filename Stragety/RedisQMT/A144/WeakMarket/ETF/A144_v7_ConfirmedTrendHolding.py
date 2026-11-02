# -*- coding: utf-8 -*-
"""Commodity ETF trend holding with confirmed relative-rank exit."""
import pandas as pd

CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
MAX_ENTRIES_PER_CODE = 2
STOP = .04
PROFIT = .06
HOLD_DAYS = 5
RANK_MARGIN = .05


def daily_signals(daily):
    result = {}
    for code in CODES:
        frame = daily[code].sort_index()
        ma20 = frame.close.rolling(20, min_periods=20).mean()
        ma50 = frame.close.rolling(50, min_periods=50).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        amount20 = frame.amount.rolling(20, min_periods=20).mean()
        good = frame.close.gt(ma20) & momentum.gt(0) & amount20.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=ma20.shift(), prior_ma50=ma50.shift(),
            prior_momentum=momentum.shift(), prior_good=good.shift(fill_value=False)), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good) for code in CODES})
    leader = scores.fillna(-float("inf")).idxmax(axis=1)
    best = scores.max(axis=1).fillna(-float("inf"))
    for code in CODES:
        result[code]["prior_leader"] = result[code].prior_good & leader.eq(code)
        result[code]["prior_best_momentum"] = best
    return result


def features(bars):
    return bars.copy()


def entry(row, time, enabled, unused):
    return bool(enabled and "093100" <= time <= "094000" and row["prior_leader"] and
                row["close"] >= row["prior_ma20"])


def exit_reason(position, row, time, age, policy):
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -STOP:
        return "stop"
    if change >= PROFIT:
        return "profit"
    rank_lost = not row["prior_leader"]
    if policy in ("rank_margin", "combined"):
        rank_lost &= row["prior_best_momentum"] - row["prior_momentum"] >= RANK_MARGIN
    if rank_lost:
        return "rank_exit"
    floor = row["prior_ma50"] if policy in ("ma50", "combined") else row["prior_ma20"]
    if row["close"] < floor:
        return "trend_exit"
    if age >= HOLD_DAYS * 240 and time >= "145000":
        return "holding_expiry"
    return ""
