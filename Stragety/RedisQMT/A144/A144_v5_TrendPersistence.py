#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A144 v5: persistent trends, breakout/pullback entries, research only."""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144.core.signal_runtime import run
from Stragety.RedisQMT.A144.core.trend import TrendConfig


CONFIG = TrendConfig(name="dual_wide_trail", trail_atr=4.0)


def main(argv=None):
    return run(CONFIG, argv)


if __name__ == "__main__":
    raise SystemExit(main())
