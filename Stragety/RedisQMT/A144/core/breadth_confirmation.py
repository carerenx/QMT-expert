# -*- coding: utf-8 -*-
"""Require nondeclining broad participation using completed session breadth."""
def nondeclining_breadth(frame, log_filter=None, lookback=5, min_change=0.0):
    result = frame.copy()
    daily = result.groupby("date").breadth.first().sort_index()
    change = (daily - daily.shift(lookback)).round(12)
    mask = result.date.map(change) >= min_change
    result["entry"] &= mask
    if log_filter is not None:
        log_filter(f"Breadth confirmation: change over {lookback} sessions>={min_change:.1%}", mask, result["entry"])
    return result
