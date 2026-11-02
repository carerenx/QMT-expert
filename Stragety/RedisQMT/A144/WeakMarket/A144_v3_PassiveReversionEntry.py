# -*- coding: utf-8 -*-
"""Independent weak-market stock reversion with causal passive entry children."""
import numpy as np

BUY_OFFSET = .002


def build_signals(frame, buy_offset=BUY_OFFSET):
    if "causal_factor" not in frame:
        raise ValueError("Causal corporate-action adjustment required")
    frame = frame.sort_values(["code", "date"]).copy()
    group = frame.groupby("code", sort=False)
    delta = group.adj_close.diff()
    gain = delta.clip(lower=0).groupby(frame.code).transform(
        lambda x: x.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    loss = (-delta.clip(upper=0)).groupby(frame.code).transform(
        lambda x: x.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    rsi = 100 * gain / (gain + loss).replace(0, np.nan)
    ret3 = group.adj_close.pct_change(3, fill_method=None)
    down2 = frame.ret.lt(0) & group.ret.shift().lt(0)
    frame["secondary_active"] = ~frame.market_strict | frame.breadth.lt(.4)
    frame["entry"] = (frame.secondary_active & down2 & frame.mom60_rank.ge(.8)
                      & frame.adj_close.gt(frame.ma50) & ret3.le(-.04)
                      & frame.age.ge(130) & frame.amount20.ge(3e7)
                      & frame.atr_pct.between(.01, .06) & frame.close.ge(3))
    frame["score"] = 1 - rsi / 100 + .1 * frame.mom60_rank
    frame["weight"] = .2
    frame["buy_offset"] = buy_offset
    return frame


def minute_trigger(position, completed_price, causal_factor):
    change = completed_price * causal_factor / (position["entry_price"] * position["entry_factor"]) - 1
    if change <= -.08:
        return "intraday-stop"
    if change >= .04:
        return "intraday-profit"
    return ""
