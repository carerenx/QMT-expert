# -*- coding: utf-8 -*-
"""Research-only weak-market ETF rotation; no broker order functions."""

ETF_NAMES = {"518880.SH": "黄金ETF华安", "513100.SH": "纳指ETF国泰",
             "511010.SH": "国债ETF国泰", "512800.SH": "银行ETF华宝"}


def build_signals(frame, horizon=20, weight=.5):
    """Inputs require CSI500 breadth, never breadth of the four ETFs."""
    frame = frame.copy()
    frame["secondary_active"] = ~frame.market_strict | frame.csi500_breadth.lt(.4)
    momentum = frame[f"mom{horizon}"]
    eligible = (momentum.gt(0) & frame.adj_close.gt(frame.ma20)
                & frame.amount20.ge(3e7) & frame.age.ge(130))
    ranks = momentum.where(eligible).groupby(frame.date).rank(method="first", ascending=False)
    frame["entry"] = frame.secondary_active & eligible & ranks.eq(1)
    frame["score"] = momentum
    frame["weight"] = weight
    frame["rotation_exit"] = ~frame.secondary_active | ~eligible | ~ranks.eq(1)
    return frame


def exit_reason(position, row):
    close = row["adj_close"]
    if close <= position["entry_price"] * position["entry_factor"] * .92:
        return "hard-stop"
    if close <= position["peak"] - 3 * row["atr"]:
        return "atr-trail"
    if row["rotation_exit"]:
        return "rotation-or-regime"
    return ""
