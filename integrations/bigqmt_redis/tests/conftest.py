"""Keep the vendored bridge tests runnable from the QMT-export root."""

from pathlib import Path
import sys

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, str(BRIDGE / "src"))
