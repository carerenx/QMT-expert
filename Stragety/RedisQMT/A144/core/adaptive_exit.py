# -*- coding: utf-8 -*-
"""Experimental completed-session exits; no intraday or future-price inputs."""
from dataclasses import replace

from .trend import exit_reason


def adaptive_exit(position, row, config, mode):
    change = round(row["adj_close"] / position["entry_price"] - 1, 12)
    peak_gain = round(position["peak"] / position["entry_price"] - 1, 12)
    if mode == "asymmetric_trail":
        multiple = 7 if peak_gain >= .15 else 3
        return exit_reason(position, row, replace(config, trail_atr=multiple))
    reason = exit_reason(position, row, config)
    if reason == "hard-stop":
        return reason
    if mode.startswith("rank_confirm"):
        count = position.get("rank_weak_days", 0) + 1 if row["leader_rank"] < .85 else 0
        position["rank_weak_days"] = count
        if reason == "leadership-lost" and count < int(mode.removeprefix("rank_confirm")):
            return ""
    elif mode == "no_rank" and reason == "leadership-lost":
        return ""
    elif mode == "winner_no_rank" and reason == "leadership-lost" and change >= .10:
        return ""
    elif mode == "hold5" and position["days"] < 5 and reason in ("leadership-lost", "trend-break", "weak-market"):
        return ""
    elif mode == "profit_floor":
        floor = .10 if peak_gain >= .30 else (.02 if peak_gain >= .15 else None)
        if floor is not None and change <= floor:
            return "profit-floor"
    elif mode == "ma20_profit" and change >= .15 and row["adj_close"] < row["ma20"]:
        return "profit-ma20"
    return reason
