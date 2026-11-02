# -*- coding: utf-8 -*-
"""Independent ETF trend-pullback hypothesis; research only, no broker calls."""
import numpy as np


def build_signals(frame, entry_mode="rsi2", weight=.5):
    frame = frame.sort_values(["code", "date"]).copy()
    group = frame.groupby("code", sort=False)
    delta = group.adj_close.diff()
    gain = delta.clip(lower=0).groupby(frame.code).transform(
        lambda x: x.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    loss = (-delta.clip(upper=0)).groupby(frame.code).transform(
        lambda x: x.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    rsi2 = 100 * gain / (gain + loss).replace(0, np.nan)
    ret3 = group.adj_close.pct_change(3, fill_method=None)
    entries = {"rsi2": rsi2.lt(10), "dip3": ret3.le(-.03)}
    frame["secondary_active"] = ~frame.market_strict | frame.csi500_breadth.lt(.4)
    eligible = (frame.secondary_active & entries[entry_mode] & frame.mom60.gt(0)
                & frame.adj_close.gt(frame.ma50) & frame.atr_pct.between(.005, .06)
                & frame.amount20.ge(3e7) & frame.age.ge(130))
    score = 1 - rsi2 / 100
    rank = score.where(eligible).groupby(frame.date).rank(method="first", ascending=False)
    frame["entry"] = eligible & rank.eq(1)
    frame["score"] = score
    frame["weight"] = weight
    return frame


def exit_reason(position, row):
    change = row["adj_close"] / (position["entry_price"] * position["entry_factor"]) - 1
    if change <= -.08:
        return "hard-stop"
    if change >= .04 or row["adj_close"] >= row["ma10"]:
        return "rebound"
    if position["days"] >= 10:
        return "time"
    return ""
