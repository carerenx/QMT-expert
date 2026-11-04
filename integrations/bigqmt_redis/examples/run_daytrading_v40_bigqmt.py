"""Convenience entry for DayTradeing v40 over the Big QMT RPC bridge."""

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
src_key = os.path.normcase(os.path.abspath(SRC))
sys.path[:] = [
    path for path in sys.path
    if os.path.normcase(os.path.abspath(path or os.curdir)) != src_key
]
sys.path.insert(0, SRC)

from bigqmt_signal_trader.external_strategy_launcher import main


if __name__ == "__main__":
    sys.exit(main())
