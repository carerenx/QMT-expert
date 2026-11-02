# -*- coding: utf-8 -*-
"""Completed-minute entry decisions; next-minute open is the fill proxy."""
from __future__ import annotations


def entry_decision(bars, date, mode, previous_close, quiet_range=.03, quiet_min=-.01, quiet_max=.03):
    cutoff = "094500" if mode.endswith("0946") else "100000"
    fill_time = "094600" if mode.endswith("0946") else "100100"
    prefix = bars.loc[(bars.index >= date + "093100") & (bars.index <= date + cutoff)]
    fill = bars.loc[bars.index == date + fill_time]
    if prefix.empty or len(fill) != 1:
        raise ValueError("missing entry decision minutes")
    close = float(prefix.iloc[-1].close)
    volume = float(prefix.volume.sum())
    price = float(fill.iloc[0].open)
    allowed = mode.startswith("delay")
    vwap = float(prefix.amount.sum()) / (volume * 100) if volume > 0 else float("nan")
    if mode.startswith("confirm"):
        allowed = volume > 0 and close > previous_close and close > vwap
    elif mode.startswith("reclaim"):
        opening = float(prefix.iloc[0].open)
        allowed = volume > 0 and float(prefix.low.min()) <= opening * .995 and close > opening and close > vwap
    elif mode.startswith("breakout"):
        opening_range = prefix.loc[prefix.index <= date + "094500"]
        allowed = volume > 0 and close > float(opening_range.high.max()) and close > vwap
    elif mode.startswith("discount"):
        allowed = volume > 0 and close < vwap and close >= previous_close * .98
    elif mode.startswith("quiet"):
        allowed = (volume > 0 and previous_close * (1 + quiet_min) <= close <= previous_close * (1 + quiet_max) and
                   float(prefix.high.max() - prefix.low.min()) <= previous_close * quiet_range)
    elif not mode.startswith("delay"):
        raise ValueError(mode)
    return {"price": price if allowed else 0.0, "time": fill_time,
            "allowed": bool(allowed), "signal_close": close, "vwap": vwap}
