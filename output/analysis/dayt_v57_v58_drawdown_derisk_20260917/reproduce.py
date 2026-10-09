from pathlib import Path
import runpy

root = Path(__file__).resolve().parents[2]
runpy.run_path(str(root / 'analysis' / 'backtest_v57_v58_drawdown_derisk.py'), run_name='__main__')
