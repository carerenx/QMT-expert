# -*- coding: utf-8 -*-
"""Independent overnight ETF reversion, research-only, no sibling imports."""
CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
WEIGHT = .30
MAX_ENTRIES_PER_CODE = 2
DEVIATION = .01
STOP = .03
PROFIT = .04


def features(bars):
    result = bars.copy()
    typical = (result.high + result.low + result.close) / 3
    volume = result.volume.clip(lower=0)
    result["mean"] = (typical * volume).cumsum() / volume.cumsum().replace(0, float("nan"))
    result["previous_close"] = result.close.shift()
    return result


def entry(row, time, enabled, deviation):
    return bool(enabled and "095100" <= time <= "141500" and
                row["close"] <= row["mean"] * (1 - deviation) and
                row["close"] > row["previous_close"])


def exit_reason(position, row, time, age, hold_days=3, control=False):
    # Control isolates overnight expiry while preserving v3's price exits.
    stop = .01 if control else STOP
    profit = .015 if control else PROFIT
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -stop:
        return "stop"
    if change >= profit:
        return "profit"
    if control and change >= .01 and row["close"] >= row["mean"]:
        return "mean_recovered"
    # Complete 240-minute sessions are required by the data gate. All entries
    # before14:15, so14:50 on D+N is always >= N*240 elapsed trading minutes.
    if age >= hold_days * 240 and time >= "145000":
        return "holding_expiry"
    return ""
