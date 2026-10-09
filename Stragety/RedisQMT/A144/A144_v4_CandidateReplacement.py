#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RedisQMT Alpha144 v4 production: daily live, DayT shadow-only."""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144 import config_v4
from Stragety.RedisQMT.A144.live_runtime_v4 import run


def main(argv=None):
    return run(config_v4, __file__, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
