"""Convenience entry for DayTradeing v41 over the Big QMT RPC bridge."""

import os
from pathlib import Path
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
src_key = os.path.normcase(os.path.abspath(SRC))
sys.path[:] = [
    path for path in sys.path
    if os.path.normcase(os.path.abspath(path or os.curdir)) != src_key
]
sys.path.insert(0, SRC)

from bigqmt_signal_trader.external_strategy_launcher import main as _main


V41_STRATEGY = (Path(__file__).resolve().parents[3] / "Stragety" / "MiniQMT_Stragety"
    / "DayT" / "DayTradeing_v41_stragety_miniqmt.py")


def main(argv=None):
    forwarded = list(sys.argv[1:] if argv is None else argv)
    return _main(["--strategy", str(V41_STRATEGY)] + forwarded)


if __name__ == "__main__":
    sys.exit(main())
