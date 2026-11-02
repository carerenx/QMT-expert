# -*- coding: utf-8 -*-
"""Independent weak-market pullback setup, next completed day reversal entry."""
from Stragety.RedisQMT.A144.core.trend import TrendConfig

CONFIG = TrendConfig(name="weak_market_pullback_reversal", entry="reversion", max_weight=.2, hard_stop=.08, cooldown=5)
PROFIT_TARGET = .04


def build_signals(frame, require_volume=False):
    if "causal_factor" not in frame:
        raise ValueError("Causal corporate-action price adjustment required")
    result = frame.sort_values(["code", "date"]).copy()
    group = result.groupby("code", sort=False)
    result["secondary_active"] = ~result.market_strict | result.breadth.lt(.4)
    setup = (result.secondary_active & result.down2 & result.mom60_rank.ge(.8) & result.adj_close.gt(result.ma50)
             & result.ret3.le(-.04) & result.age.ge(130) & result.amount20.ge(3e7)
             & result.atr_pct.between(.01, .06) & result.close.ge(3))
    prior_setup = setup.groupby(result.code).shift(fill_value=False)
    result["entry"] = (prior_setup & result.secondary_active & result.ret.gt(0) & result.adj_close.gt(result.adj_open)
                       & result.adj_close.gt(result.ma50) & result.amount20.ge(3e7)
                       & result.atr_pct.between(.01, .06) & result.close.ge(3))
    if require_volume:
        result["entry"] &= result.volume.gt(group.volume.shift())
    result["score"] = 1 - group.rsi2.shift() / 100 + .1 * result.mom60_rank
    result["weight"] = CONFIG.max_weight
    return result


def minute_trigger(position, completed_price, causal_factor):
    change = completed_price * causal_factor / (position["entry_price"] * position["entry_factor"]) - 1
    if change <= -CONFIG.hard_stop:
        return "intraday-stop"
    if change >= PROFIT_TARGET:
        return "intraday-profit"
    return ""
