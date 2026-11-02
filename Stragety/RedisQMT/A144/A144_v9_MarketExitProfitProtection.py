#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Research candidate: EOD entry scanner and offline market/profit exit policy.

The CLI emits entry candidates only. It neither executes exits nor submits orders.
Historical replay must explicitly use this module's exit_reason function.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144.core.adaptive_exit import adaptive_exit
from Stragety.RedisQMT.A144.core.signal_runtime import run
from Stragety.RedisQMT.A144.core.trend import TrendConfig

CONFIG = TrendConfig(name="leader60_market_exit_profit_protection", entry="leader", leader_horizon=60,
                     trail_atr=5.0, risk_per_position=0.03, strict_market=True,
                     min_efficiency=0.30, leader_efficiency_weight=0.30)


def exit_reason(position, row, config=CONFIG):
    reason = adaptive_exit(position, row, config, "profit_floor")
    if reason == "hard-stop":
        return reason
    if row["market_exit"]:
        return "index-stress"
    return reason


def main(argv=None):
    return run(CONFIG, argv)


if __name__ == "__main__":
    raise SystemExit(main())
