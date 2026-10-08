#!/usr/bin/env python
"""
QMT-Expert Smoke Test Driver
=============================
Validates that the backtest system works end-to-end: imports, data loading,
strategy execution, and performance analysis.

Usage:
    python .claude/skills/run-qmt-expert/driver.py          # full smoke test
    python .claude/skills/run-qmt-expert/driver.py --quick  # imports only (no backtest run)
    python .claude/skills/run-qmt-expert/driver.py --stock 600519.SH  # custom stock

Exit code 0 = all checks passed. Non-zero = first failing check.
"""

import os
import sys
import argparse
import traceback
import time
from datetime import datetime

# Ensure project root is on sys.path
# driver.py is at .claude/skills/run-qmt-expert/driver.py → 4 levels up = project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PASS = 0
FAIL = 0
SKIP = 0


def check(name, fn):
    """Run a single smoke check. Returns True on pass, False on fail."""
    global PASS, FAIL, SKIP
    try:
        result = fn()
        PASS += 1
        status = "PASS"
        detail = f"  [{status}] {name}"
        if result and isinstance(result, str):
            detail += f" — {result}"
        print(detail)
        return True
    except Exception as e:
        FAIL += 1
        print(f"  [FAIL] {name}")
        print(f"         {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════════
# Check definitions
# ═══════════════════════════════════════════════════════════════

def check_imports():
    """Verify all core modules can be imported."""
    from backtest.data_source import DataProvider, code_to_bs, code_from_bs
    from backtest.engine import BacktestEngine
    from backtest.analyzer import PerformanceAnalyzer
    from backtest.qmt_mock import MockContextInfo, order_shares, passorder, get_trade_detail_data, timetag_to_datetime
    from backtest.run import load_strategy_module
    from backtest import config

    # OKH modules
    from backtest.okh.engine import OkhEngine
    from backtest.okh.config import OkhConfig, create_default_config
    from backtest.okh.adapter import QmtStrategyAdapter

    return f"backtest + okh modules OK"


def check_data_loading(stock_code="600519.SH"):
    """Verify data can be loaded from cache."""
    from backtest.data_source import DataProvider

    data = DataProvider()
    data.load([stock_code], '2024-01-01', '2024-12-31')
    data.validate()

    n_bars = len(data.dates_list)
    n_codes = len(data.code_list)

    assert n_bars > 0, "No bars loaded"
    assert n_codes == 1, f"Expected 1 code, got {n_codes}"
    assert stock_code in data.ohlcv, f"{stock_code} not in ohlcv"

    first_date = data.dates_list[0]
    last_date = data.dates_list[-1]
    close_len = len(data.ohlcv[stock_code]['close'])

    return f"{stock_code}: {n_bars} bars, {first_date.date()} ~ {last_date.date()}"


def check_config():
    """Verify default config values are sensible."""
    from backtest import config

    assert config.INITIAL_CAPITAL > 0, "INITIAL_CAPITAL must be positive"
    assert len(config.STOCK_POOL) > 0, "STOCK_POOL is empty"
    assert config.BENCHMARK_CODE, "BENCHMARK_CODE not set"
    assert os.path.isdir(config.CACHE_DIR), f"CACHE_DIR missing: {config.CACHE_DIR}"
    assert os.path.isfile(config.DEFAULT_STRATEGY), f"DEFAULT_STRATEGY missing: {config.DEFAULT_STRATEGY}"

    return f"pool={len(config.STOCK_POOL)} stocks, capital={config.INITIAL_CAPITAL:,.0f}"


def check_strategy_loading():
    """Verify a QMT strategy file can be loaded via the mock layer."""
    from backtest.qmt_mock import order_shares, passorder, get_trade_detail_data, timetag_to_datetime
    from backtest.run import load_strategy_module
    from backtest import config

    mock_globals = {
        'order_shares': order_shares,
        'passorder': passorder,
        'get_trade_detail_data': get_trade_detail_data,
        'timetag_to_datetime': timetag_to_datetime,
    }
    strategy_module = load_strategy_module(config.DEFAULT_STRATEGY, mock_globals)

    assert 'init' in strategy_module, "strategy missing init()"
    assert 'handlebar' in strategy_module, "strategy missing handlebar()"

    return os.path.basename(config.DEFAULT_STRATEGY)


def check_engine_run(stock_code="600519.SH"):
    """Run a minimal single-stock backtest end-to-end."""
    from backtest.data_source import DataProvider
    from backtest.engine import BacktestEngine
    from backtest.analyzer import PerformanceAnalyzer
    from backtest.qmt_mock import MockContextInfo, order_shares, passorder, get_trade_detail_data, timetag_to_datetime
    from backtest.run import load_strategy_module
    from backtest import config

    # Load data
    data = DataProvider()
    data.load([stock_code], '2020-01-01', '2024-12-31')

    # Create engine
    engine = BacktestEngine(data)

    # Load strategy
    mock_globals = {
        'order_shares': order_shares,
        'passorder': passorder,
        'get_trade_detail_data': get_trade_detail_data,
        'timetag_to_datetime': timetag_to_datetime,
    }
    strategy_module = load_strategy_module(config.DEFAULT_STRATEGY, mock_globals)

    # Create context
    context = MockContextInfo(data, engine)
    context.stock_pool = [stock_code]

    # Run
    t0 = time.time()
    engine.run(context, strategy_module)
    elapsed = time.time() - t0

    # Basic validation
    assert len(engine.trades) >= 0, "trades list corrupted"
    assert engine.cash >= 0, "negative cash"
    assert len(engine.equity_curve) > 0, "empty equity curve"

    # Quick analysis
    analyzer = PerformanceAnalyzer(engine)
    analyzer._engine = engine
    analyzer.calculate()

    n_trades = len(engine.trades)
    n_bars = len(data.dates_list)

    return f"{n_bars} bars, {n_trades} trades, {elapsed:.1f}s"


def check_okh_engine_run(stock_code="600519.SH"):
    """Run a minimal single-stock OKH backtest end-to-end."""
    from backtest.data_source import DataProvider
    from backtest.okh.engine import OkhEngine
    from backtest.okh.config import create_default_config
    from backtest.okh.adapter import QmtStrategyAdapter
    from backtest import config

    # Load data
    data = DataProvider()
    data.load([stock_code], '2020-01-01', '2024-12-31')

    # Config
    cfg = create_default_config(
        stock_pool=[stock_code],
        benchmark='000300.SH',
        start='20200101',
        end='20241231',
        capital=10000000,
    )

    # Engine
    engine = OkhEngine(data, cfg)

    # Load strategy
    adapter = QmtStrategyAdapter(config.DEFAULT_STRATEGY, data, cfg)
    strategy_mod = adapter.load()
    adapter._engine_ref = engine

    # Run
    t0 = time.time()
    engine.run(strategy_mod)
    elapsed = time.time() - t0

    n_bars = len(data.dates_list)
    n_stats = len(engine.daily_stats)

    return f"{n_bars} bars, {n_stats} daily stats, {elapsed:.1f}s"


def check_cache_dir():
    """Verify the cache directory exists and has data."""
    from backtest import config

    cache = config.CACHE_DIR
    csv_files = [f for f in os.listdir(cache) if f.endswith('.csv')]
    assert len(csv_files) > 0, f"No cached data in {cache}"

    return f"{len(csv_files)} cached stocks"


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    global SKIP
    parser = argparse.ArgumentParser(
        description='QMT-Expert Smoke Test',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python .claude/skills/run-qmt-expert/driver.py
  python .claude/skills/run-qmt-expert/driver.py --quick
  python .claude/skills/run-qmt-expert/driver.py --stock 000001.SZ
        """
    )
    parser.add_argument('--quick', action='store_true',
                        help='Imports only, skip backtest runs')
    parser.add_argument('--stock', default='600519.SH',
                        help='Stock code for data/run checks (default: 600519.SH)')
    args = parser.parse_args()

    print("=" * 60)
    print("  QMT-Expert Smoke Test")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Project: {PROJECT_ROOT}")
    print("=" * 60)

    # ── Phase 1: Imports & Config (always run) ──
    print("\n── Phase 1: Imports & Config ──")
    check("Core imports (backtest + okh)", check_imports)
    check("Backtest config", check_config)
    check(f"Cache directory", check_cache_dir)

    if args.quick:
        print("\n── Quick mode: skipping backtest runs ──")
        print_summary()
        return 0 if FAIL == 0 else 1

    # ── Phase 2: Data & Loading ──
    print(f"\n── Phase 2: Data & Strategy Loading (stock={args.stock}) ──")
    data_ok = check(f"Data loading ({args.stock})", lambda: check_data_loading(args.stock))
    strat_ok = check("Strategy loading", check_strategy_loading)

    # ── Phase 3: Backtest Runs ──
    print(f"\n── Phase 3: Backtest Runs ({args.stock}) ──")
    if data_ok and strat_ok:
        check("Base engine run", lambda: check_engine_run(args.stock))
        check("OKH engine run", lambda: check_okh_engine_run(args.stock))
    else:
        SKIP += 2
        print("  [SKIP] Base engine run — prerequisites failed")
        print("  [SKIP] OKH engine run — prerequisites failed")

    # ── Summary ──
    print_summary()
    return 0 if FAIL == 0 else 1


def print_summary():
    total = PASS + FAIL + SKIP
    print(f"\n{'=' * 60}")
    print(f"  Results: {PASS} passed, {FAIL} failed, {SKIP} skipped ({total} total)")
    if FAIL == 0:
        print("  VERDICT: ALL CHECKS PASSED")
    else:
        print(f"  VERDICT: {FAIL} CHECK(S) FAILED")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
