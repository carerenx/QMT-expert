# -*- coding: utf-8 -*-
"""Independent secondary prototype: previous-close entry, T+1 intraday exits.

Triggers use completed minute closes. Orders may execute only at a later minute;
an intraminute high touching the profit target is never assumed to be a fill.
"""
import numpy as np

from Stragety.RedisQMT.A144.core.trend import TrendConfig

CONFIG = TrendConfig(name="weak_market_intraday_reversion", entry="reversion",
                     max_weight=.2, hard_stop=.08, cooldown=5)
PROFIT_TARGET = .04
MAX_HOLD_DAYS = 5


def build_signals(frame):
    if "causal_factor" not in frame:
        raise ValueError("Causal corporate-action price adjustment required")
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
    frame["weight"] = CONFIG.max_weight
    return frame


def minute_trigger(position, completed_price, causal_factor,
                   stop=CONFIG.hard_stop, target=PROFIT_TARGET):
    change = completed_price * causal_factor / (position["entry_price"] * position["entry_factor"]) - 1
    if change <= -stop:
        return "intraday-stop"
    if change >= target:
        return "intraday-profit"
    return ""
