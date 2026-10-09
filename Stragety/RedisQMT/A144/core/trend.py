# -*- coding: utf-8 -*-
"""Causal trend signals. All rows represent a completed trading session."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrendConfig:
    name: str = "dual_entry"
    entry: str = "dual"
    impact_weight: float = 0.0
    trail_atr: float = 3.0
    strict_market: bool = False
    max_positions: int = 5
    max_entries: int = 2
    risk_per_position: float = 0.015
    max_weight: float = 0.20
    hard_stop: float = 0.10
    cooldown: int = 10
    leader_horizon: int = 60


def features(frame: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["code", "date"]).copy()
    group = frame.groupby("code", sort=False)
    frame["ret"] = group["adj_close"].pct_change(fill_method=None)
    frame["previous"] = group["adj_close"].shift()
    for window in (10, 20, 50):
        frame[f"ma{window}"] = group["adj_close"].transform(
            lambda values: values.rolling(window, min_periods=window).mean())
    frame["ma20_lag5"] = group["ma20"].shift(5)
    frame["ma50_lag5"] = group["ma50"].shift(5)
    frame["mom20"] = group["adj_close"].pct_change(20, fill_method=None)
    frame["mom60"] = group["adj_close"].pct_change(60, fill_method=None)
    frame["mom120"] = group["adj_close"].pct_change(120, fill_method=None)
    frame["age"] = group.cumcount() + 1
    frame["amount20"] = group["amount"].transform(lambda x: x.rolling(20).mean())
    frame["amount5_previous"] = group["amount"].transform(
        lambda x: x.shift().rolling(5).mean())
    frame["high20"] = group["adj_high"].transform(lambda x: x.shift().rolling(20).max())
    frame["high10close"] = group["adj_close"].transform(lambda x: x.shift().rolling(10).max())
    frame["minret60"] = group["ret"].transform(lambda x: x.rolling(60).min())
    frame["range"] = (frame["adj_high"] - frame["adj_low"]) / frame["adj_close"]
    frame["maxrange5"] = group["range"].transform(lambda x: x.rolling(5).max())
    tr = pd.concat([frame["adj_high"] - frame["adj_low"],
                    (frame["adj_high"] - frame["previous"]).abs(),
                    (frame["adj_low"] - frame["previous"]).abs()], axis=1).max(axis=1)
    frame["atr"] = tr.groupby(frame["code"]).transform(lambda x: x.rolling(20).mean())
    frame["atr_pct"] = frame["atr"] / frame["adj_close"]
    frame["abs_change"] = (frame["adj_close"] - frame["previous"]).abs()
    path = group["abs_change"].transform(lambda x: x.rolling(20).sum())
    frame["efficiency"] = (frame["adj_close"] - group["adj_close"].shift(20)) / path.replace(0, np.nan)
    frame["impact"] = np.where(frame["ret"] < 0,
                                -frame["ret"] / frame["amount"].replace(0, np.nan), 0)
    frame["impact20"] = group["impact"].transform(lambda x: x.rolling(20).sum())
    for offset in (1, 2, 3):
        frame[f"up{offset}"] = group["ret"].shift(offset) > 0
    daily = frame.groupby("date")
    for field in ("mom20", "mom60", "mom120", "efficiency", "impact20"):
        frame[field + "_rank"] = daily[field].rank(pct=True)
    frame["breadth"] = (frame["adj_close"] > frame["ma50"]).groupby(frame["date"]).transform("mean")
    index = benchmark.copy()
    index.index = index.index.astype(str).str[:8]
    index = index.sort_index()
    close = index["close"]
    index["market_ok"] = close >= close.rolling(20).mean() * 0.97
    index["market_strict"] = close >= close.rolling(60).mean()
    frame["market_ok"] = frame["date"].map(index["market_ok"]).fillna(False).astype(bool)
    frame["market_strict"] = frame["date"].map(index["market_strict"]).fillna(False).astype(bool)
    return frame


def signals(frame: pd.DataFrame, config: TrendConfig) -> pd.DataFrame:
    """Return immutable close-time entry, exit and sizing inputs."""
    frame = frame.copy()
    trend = ((frame["adj_close"] > frame["ma50"]) &
             (frame["ma20"] > frame["ma50"]) &
             (frame["ma50"] > frame["ma50_lag5"]) &
             (frame["mom60"] > 0) & (frame["mom60_rank"] >= 0.7))
    breakout = ((frame["adj_close"] > frame["high20"]) &
                (frame["amount"] >= frame["amount5_previous"] * 1.1))
    pullback = ((frame["adj_low"] <= frame["ma10"]) &
                (frame["adj_close"] >= frame["ma10"]) &
                (frame["ret"] > 0) &
                (frame["amount"] <= frame["amount20"] * 1.2) &
                (frame["adj_close"] <= frame["ma20"] + 2 * frame["atr"]))
    leader_rank = frame[f"mom{config.leader_horizon}_rank"]
    leader = (leader_rank >= 0.95) & (frame["adj_close"] > frame["ma20"])
    entry_modes = {"breakout": breakout, "pullback": pullback,
                   "dual": breakout | pullback, "leader": leader}
    if config.entry not in entry_modes:
        raise ValueError("unknown entry mode")
    market = frame["market_strict"] if config.strict_market else frame["market_ok"]
    frame["entry"] = (trend & entry_modes[config.entry] & market &
                       (frame["breadth"] >= 0.4) & (frame["age"] >= 130) &
                       (frame["amount20"] >= 3e7) &
                       frame["atr_pct"].between(0.01, 0.08) &
                       (frame["ret"] < 0.095))
    frame["score"] = (0.7 * frame["mom60_rank"] + 0.3 * frame["efficiency_rank"] +
                       config.impact_weight * frame["impact20_rank"])
    if config.entry == "leader":
        frame["score"] = leader_rank
    frame["leader_rank"] = leader_rank
    frame["market_exit"] = ~market
    frame["weight"] = np.minimum(config.max_weight,
                                  config.risk_per_position / (config.trail_atr * frame["atr_pct"]))
    return frame


def exit_reason(position: dict, row: dict, config: TrendConfig) -> str:
    close = float(row["adj_close"])
    if close <= position["entry_price"] * (1 - config.hard_stop):
        return "hard-stop"
    if close <= position["peak"] - config.trail_atr * float(row["atr"]):
        return "atr-trail"
    if close < float(row["ma50"]):
        return "trend-break"
    if bool(row["market_exit"]) and close < float(row["ma20"]):
        return "weak-market"
    if config.entry == "leader" and float(row["leader_rank"]) < 0.85:
        return "leadership-lost"
    return ""
