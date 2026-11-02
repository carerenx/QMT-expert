# -*- coding: utf-8 -*-
"""Research-only weak-market recovery; requires raw-price features and entry metadata.

No live order interface. Raw-share diagnostic does not model corporate actions.
"""
from Stragety.RedisQMT.A144.core.trend import TrendConfig, signals
from Stragety.RedisQMT.A144.core.breadth_confirmation import nondeclining_breadth
from Stragety.RedisQMT.A144.core.adaptive_exit import adaptive_exit

CONFIG = TrendConfig(name="weak_market_recovery", entry="leader", leader_horizon=60,
                     trail_atr=5., risk_per_position=.06, strict_market=True,
                     min_efficiency=.30, leader_efficiency_weight=.30)


def build_signals(frame, config=CONFIG):
    if not frame.adj_close.eq(frame.close).all():
        raise ValueError("Research-only raw-price features required; corporate actions unmodeled")
    result = nondeclining_breadth(signals(frame, config), lookback=5, min_change=.05)
    result["entry"] &= result.atr_pct.le(.06)
    previous_ma10 = frame.groupby("code", sort=False).ma10.shift()
    breadth_change = frame.groupby("date").breadth.first().sort_index().diff(5)
    recovery = (~frame.market_strict & frame.breadth.ge(.30) & frame.date.map(breadth_change).gt(0) &
                frame.adj_close.gt(frame.ma10) & frame.previous.le(previous_ma10) &
                frame.adj_close.le(frame.high20*.92) & frame.mom60_rank.ge(.50) &
                frame.age.ge(130) & frame.amount20.ge(3e7) & frame.atr_pct.between(.01, .06) &
                frame.ret.gt(0) & frame.ret.lt(.095))
    result["recovery_entry"] = recovery
    result["entry"] |= recovery
    result.loc[recovery, "weight"] = .10
    rebound_rank = result.groupby("date").ret.rank(pct=True)
    result.loc[recovery, "score"] = .5*result.loc[recovery, "mom60_rank"] + .5*rebound_rank.loc[recovery]
    return result


def exit_reason(position, row, config=CONFIG, stop=.04, target=.06, hold=10):
    if position.get("recovery_entry", False):
        change = row["adj_close"] / position["entry_price"] - 1
        if change <= -stop:
            return "recovery-stop"
        if change >= target:
            return "recovery-profit"
        if position["days"] >= hold:
            return "recovery-time"
        if position["days"] >= 3 and row["adj_close"] < row["ma10"]:
            return "recovery-failed"
        return ""
    reason = adaptive_exit(position, row, config, "profit_floor")
    if reason == "hard-stop":
        return reason
    return "index-stress" if row["market_exit"] else reason
