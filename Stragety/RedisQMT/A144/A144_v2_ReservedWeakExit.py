#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RedisQMT Alpha144 v2 — reserved-slot weak-position exit.

The strategy keeps the v1 Alpha144 entry signal and normalized inverse-ATR
budget.  A position may exit after 12 bars when its return is at most -4% and
its adjusted close is below MA20.  The vacated slot stays reserved until the
original 20-bar holding horizon, preventing replacement churn from changing
the strategy's right-tail winner path.
python .\Stragety\RedisQMT\A144\A144_v2_ReservedWeakExit.py plan
python .\Stragety\RedisQMT\A144\A144_v2_ReservedWeakExit.py execute --mode live --confirm LIVE
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144 import config_v2
from Stragety.RedisQMT.A144.live_runtime import run


def main(argv=None):
    return run(config_v2, __file__, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
