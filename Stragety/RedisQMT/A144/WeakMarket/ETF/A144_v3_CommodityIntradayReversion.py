# -*- coding: utf-8 -*-
"""Research-only commodity ETF T0 reversion; signals use completed bars only."""
CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
WEIGHT = .30
MAX_ENTRIES_PER_CODE = 2
STOP = .01
PROFIT = .015
MAX_HOLD_BARS = 90


def features(bars):
    result = bars.copy()
    # Volume-weighted typical price proxy, not exchange trade VWAP/IOPV.
    typical = (result.high + result.low + result.close) / 3
    volume = result.volume.clip(lower=0)
    result["mean"] = (typical * volume).cumsum() / volume.cumsum().replace(0, float("nan"))
    result["previous_close"] = result.close.shift()
    return result


def entry(row, time, enabled, deviation):
    return bool(enabled and "095100" <= time <= "141500" and
                row["close"] <= row["mean"] * (1 - deviation) and
                row["close"] > row["previous_close"])


def exit_reason(position, row, time, age):
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -STOP:
        return "stop"
    if change >= PROFIT:
        return "profit"
    if change >= .01 and row["close"] >= row["mean"]:
        return "mean_recovered"
    if time >= "145000":
        return "session_exit"
    if age >= MAX_HOLD_BARS:
        return "time_exit"
    return ""
