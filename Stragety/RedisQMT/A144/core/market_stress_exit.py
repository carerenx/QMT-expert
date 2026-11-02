# -*- coding: utf-8 -*-
"""Research-only completed-session market stress exits; no threshold fills."""
from .trend import exit_reason


def market_stress_features(frame, benchmark):
    result = frame.copy()
    index = benchmark.copy()
    index.index = index.index.astype(str).str[:8]
    close = index.sort_index().close
    result["index_ret"] = result.date.map(close.pct_change(fill_method=None))
    result["index_ret5"] = result.date.map(close.pct_change(5, fill_method=None))
    breadth = result.groupby("date").breadth.first().sort_index()
    result["breadth_change5"] = result.date.map(breadth - breadth.shift(5))
    return result


def market_stress_exit(position, row, config, mode):
    original = exit_reason(position, row, config)
    if original == "hard-stop":
        return original
    if mode == "index_all" and row["market_exit"]:
        return "index-stress"
    if mode == "breadth_all" and row["breadth"] < .4:
        return "breadth-stress"
    if mode == "shock_all" and row["index_ret"] <= -.02:
        return "index-shock"
    if mode == "joint_shock" and row["index_ret5"] <= -.03 and row["breadth_change5"] <= -.1:
        return "joint-shock"
    return original
