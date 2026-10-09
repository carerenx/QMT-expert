---
name: run-qmt-expert
description: Build, run, smoke-test, and drive the QMT-expert quantitative trading backtest system. Use when asked to run a backtest, verify the engine works, validate a strategy, test data loading, or check that the backtest system is healthy.
---

QMT-expert is a Python quantitative trading strategy library with a CLI backtest system.
Drive it via `.claude/skills/run-qmt-expert/driver.py` (smoke test) or `python -m backtest.run` (full CLI).

All paths below are relative to the repo root.

## Prerequisites

Pure Python — no system packages required. The project uses Python 3.13+ with these pip packages:

```bash
pip install numpy pandas matplotlib baostock akshare
```

## Setup

```bash
# Restore strategy files if deleted from working tree
git restore MyPy-Q/
```

No env vars or config files needed. The backtest engine uses defaults from `backtest/config.py`.

## Run (agent path)

The **smoke test driver** validates imports, data loading, strategy loading, and runs a minimal single-stock backtest on both engines:

```bash
python .claude/skills/run-qmt-expert/driver.py
```

Options:

```bash
python .claude/skills/run-qmt-expert/driver.py --quick          # imports only, skip backtest runs
python .claude/skills/run-qmt-expert/driver.py --stock 000001.SZ  # use a different cached stock
```

Exit code 0 = all checks passed. Non-zero = first failing check reported with a traceback.

The driver runs 7 checks across 3 phases:
1. **Imports & Config** — validates all core modules import, config is sensible, cache exists
2. **Data & Strategy Loading** — loads one stock from cache, loads default strategy via mock layer
3. **Backtest Runs** — runs full end-to-end backtest on both base `BacktestEngine` and `OkhEngine`

## Run (CLI path)

Full backtest with the default strategy and stock pool:

```bash
python -m backtest.run
```

With custom parameters:

```bash
python -m backtest.run --strategy "MyPy-Q/Alpha144_流动性冲击择时策略.py" --start 2020-01-01 --end 2024-12-31
python -m backtest.run --param RISK_PER_TRADE 0.01 --param MAX_POSITIONS 3 --no-plot
```

OKH enhanced backtest:

```bash
python -m backtest.run_okh --strategy "MyPy-Q/Alpha144_流动性冲击择时策略.py" --start 2020-01-01 --end 2025-12-31
```

Output lands in `backtest/output/` (trades.csv, daily_stats.csv, metrics.csv, equity_curve.png).

## Direct invocation (programmatic)

For PRs that touch engine internals, import and run without the full CLI:

```python
import sys; sys.path.insert(0, '.')
from backtest.data_source import DataProvider
from backtest.engine import BacktestEngine
from backtest.qmt_mock import MockContextInfo, order_shares, get_trade_detail_data, passorder, timetag_to_datetime
from backtest.run import load_strategy_module
from backtest import config

# Load one stock from cache (fast)
data = DataProvider()
data.load(['600519.SH'], '2020-01-01', '2024-12-31')

engine = BacktestEngine(data)

mock_globals = {
    'order_shares': order_shares, 'passorder': passorder,
    'get_trade_detail_data': get_trade_detail_data,
    'timetag_to_datetime': timetag_to_datetime,
}
strategy_module = load_strategy_module(config.DEFAULT_STRATEGY, mock_globals)

context = MockContextInfo(data, engine)
context.stock_pool = ['600519.SH']

engine.run(context, strategy_module)
print(f"Trades: {len(engine.trades)}, Final cash: {engine.cash:,.0f}")
```

For the enhanced OKH engine:

```python
from backtest.okh.engine import OkhEngine
from backtest.okh.config import create_default_config
from backtest.okh.adapter import QmtStrategyAdapter

cfg = create_default_config(
    stock_pool=['600519.SH'], benchmark='000300.SH',
    start='20200101', end='20241231', capital=10000000,
)
engine = OkhEngine(data, cfg)
adapter = QmtStrategyAdapter(config.DEFAULT_STRATEGY, data, cfg)
strategy_mod = adapter.load()
adapter._engine_ref = engine
engine.run(strategy_mod)
```

## Run (human path)

```bash
python -m backtest.run                        # full 500-stock backtest (takes 30+ min)
python -m backtest.run --no-plot --start 2024-01-01 --end 2024-06-30  # shorter window
```

Ctrl-C to stop. Results saved to `backtest/output/report_<timestamp>/`.

## Test

There is no standalone test suite. The smoke test driver (`driver.py`) serves as the integration test. Run it before and after changes:

```bash
python .claude/skills/run-qmt-expert/driver.py
```

## Gotchas

- **MyPy-Q directory may be missing** — strategy files are frequently deleted from the working tree during refactoring. Run `git restore MyPy-Q/` before running backtests.
- **Strategy files use GBK encoding** — the loader tries GBK first, then UTF-8. Non-ASCII characters (Chinese) in strategy files can cause decode errors if files are saved as UTF-8 without the `# -*- coding: gbk -*-` header.
- **Cache covers only 83 stocks** — the full `STOCK_POOL` has 500 CSI 500 constituents. Loading all 500 via baostock takes ~5 minutes. Use `--stock <cached_code>` with the smoke test driver for instant runs.
- **OkhEngine has different internals** — it stores results in `daily_stats` (not `trades`), positions in `trade_mgr.positions`, and assets in `trade_mgr.assets`. The base `BacktestEngine` uses `trades`, `positions`, and `equity_curve` directly.
- **QMT-injected globals are undefined outside QMT** — `order_shares`, `passorder`, `get_trade_detail_data`, `timetag_to_datetime` are global in QMT but must be mocked via `backtest.qmt_mock` for standalone runs.
- **`python -m backtest.run` from project root** — the backtest modules use relative imports (`from . import config`) and must be run as `python -m backtest.run` from the repo root, not as `python backtest/run.py`.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'backtest'`** — you're not running from the repo root. `cd` to the project root and ensure it's on `sys.path`.
- **`FileNotFoundError: 策略文件不存在`** — the `MyPy-Q/` directory has been deleted. Run `git restore MyPy-Q/`.
- **baostock login timeout** — baostock requires internet access. If behind a proxy, the `DataProvider` will retry 3 times before giving up on individual stocks. Backtests using only cached data (the driver default) don't hit the network.
- **`UnicodeDecodeError` in `load_strategy_module`** — the strategy file encoding doesn't match GBK. Re-save as GBK or add `# -*- coding: utf-8 -*-` to the file header.
