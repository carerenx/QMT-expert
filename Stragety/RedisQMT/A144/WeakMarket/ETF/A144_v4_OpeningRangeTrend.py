# -*- coding: utf-8 -*-
"""Independent research-only ETF opening-range breakout with trailing exit."""
CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
WEIGHT = .30
MAX_ENTRIES_PER_CODE = 2
STOP = .01
TRAIL = .01


def features(bars):
    result = bars.copy()
    result["previous_close"] = result.close.shift()
    for window in (15, 30):
        end = "094500" if window == 15 else "100000"
        # Early rows retain their then-known high; freeze only after the range.
        result[f"range{window}"] = result.high.cummax().where(result.index.astype(str).str[8:] <= end).ffill()
    return result


def entry(row, time, enabled, window):
    end = "094500" if window == 15 else "100000"
    level = row[f"range{window}"]
    return bool(enabled and end < time <= "141500" and row["close"] > level and row["previous_close"] <= level)


def exit_reason(position, row, time, age):
    cost = position["buy_notional"] / position["bought"]
    position["peak_close"] = max(position.get("peak_close", cost), row["close"])
    if row["close"] <= cost * (1 - STOP):
        return "stop"
    if row["close"] <= position["peak_close"] * (1 - TRAIL):
        return "trailing_exit"
    if time >= "145000":
        return "session_exit"
    return ""
