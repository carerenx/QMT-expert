#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Research candidate with breadth confirmation and completed-close exits.

CLI emits entry candidates only; no orders or position-aware exit execution.
Historical replay must use build_signals and explicitly use exit_reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144.core.adaptive_exit import adaptive_exit
from Stragety.RedisQMT.A144.core.breadth_confirmation import nondeclining_breadth
from Stragety.RedisQMT.A144.core.signal_runtime import run
from Stragety.RedisQMT.A144.core.trend import TrendConfig, signals

CONFIG = TrendConfig(name="leader60_breadth_confirmation", entry="leader", leader_horizon=60,
                     trail_atr=5.0, risk_per_position=0.06, strict_market=True,
                     min_efficiency=0.30, leader_efficiency_weight=0.30)
BREADTH_LOOKBACK = 5
BREADTH_MIN_CHANGE = 0.05
ENTRY_ATR_MAX = 0.06


def build_signals(frame, config=CONFIG, log_filter=None):
    result = nondeclining_breadth(signals(frame, config, log_filter=log_filter), log_filter,
                                  lookback=BREADTH_LOOKBACK, min_change=BREADTH_MIN_CHANGE)
    mask = result.atr_pct <= ENTRY_ATR_MAX
    result["entry"] &= mask
    if log_filter is not None:
        log_filter(f"Entry ATR ratio<={ENTRY_ATR_MAX:.1%}", mask, result["entry"])
    return result


def exit_reason(position, row, config=CONFIG):
    reason = adaptive_exit(position, row, config, "profit_floor")
    if reason == "hard-stop":
        return reason
    if row["market_exit"]:
        return "index-stress"
    return reason


def main(argv=None):
    return run(CONFIG, argv, signal_builder=build_signals)


if __name__ == "__main__":
    raise SystemExit(main())
