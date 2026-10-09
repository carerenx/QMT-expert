#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RedisQMT Alpha144 v3 — v2 daily strategy plus reverse-T overlay.

Daily strategy:
  python ./Stragety/RedisQMT/A144/A144_v3_ReverseTOverlay.py plan
  python ./Stragety/RedisQMT/A144/A144_v3_ReverseTOverlay.py execute --mode live --confirm LIVE

Reverse-T observation/live loop:
  python ./Stragety/RedisQMT/A144/A144_v3_ReverseTOverlay.py dayt --mode signal
  python ./Stragety/RedisQMT/A144/A144_v3_ReverseTOverlay.py dayt --mode live --confirm LIVE

Explicit v2 ownership migration:
  python ./Stragety/RedisQMT/A144/A144_v3_ReverseTOverlay.py migrate-v2 --mode live --confirm MIGRATE
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144 import config_v3
from Stragety.RedisQMT.A144.live_runtime_v3 import run


def main(argv=None):
    return run(config_v3, __file__, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
