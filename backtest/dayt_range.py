"""Run registered DayT strategies over an explicit date range without touching golden files.

The ``daily`` mode uses the frozen-comparison execution assumptions but limits the
sessions to the requested range.  The ``strict`` mode preserves one continuous
cash/position account and delegates to the event-driven exchange simulator.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from output.analysis import compare_v51_v39_minute as daily_replay
from backtest import dayt_strict
from backtest.dayt_registry import STRATEGIES


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_data(start, end, data_dir=None):
    data_dir = ROOT / 'analysis/v51_v39_minute' if data_dir is None else Path(data_dir)
    daily = pd.read_csv(data_dir / '1d.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = pd.read_csv(data_dir / '1m.csv', dtype={'time': str}).set_index('time').sort_index()
    selected = minute.index.str[:8]
    minute = minute.loc[(selected >= start) & (selected <= end)]
    if minute.empty:
        raise ValueError('no minute data in requested range {}..{}'.format(start, end))
    return daily, minute


def daily_alias(version):
    """Map interface-compatible variants to the legacy daily runner's names."""
    if version == 'v39_nomom':
        return 'v39'
    if version == 'v52_nomom':
        return 'v52'
    return version


def run_daily(version, daily, minute):
    alias = daily_alias(version)
    original = STRATEGIES[alias]
    STRATEGIES[alias] = STRATEGIES[version]
    try:
        results = []
        skipped = []
        for date, bars in minute.groupby(minute.index.str[:8]):
            history = daily.loc[daily.index < date]
            if len(bars) < 230 or bars.index[0][8:12] not in ('0930', '0931') or len(history) < 80:
                skipped.append(dict(date=date, bars=len(bars), reason='partial session or insufficient history'))
                continue
            for slip in (0.0, 0.01):
                result = daily_replay.replay(alias, history, bars, slip)
                result['version'] = version
                if result['failure']:
                    raise RuntimeError('{} {}: {}'.format(version, date, result['failure']))
                results.append(result)
        return dict(model='DAILY_RESET_FROZEN_COMPARISON_ASSUMPTIONS', results=results, skipped=skipped)
    finally:
        STRATEGIES[alias] = original


def run_strict(version, daily, minute, symbol='601869.SH', stock_name='601869',
               initial_cash=100000.0, initial_shares=200, target_value=None):
    settings = {}
    if version.startswith('v52'):
        settings = dict(DIRECTIONAL_THRESHOLD=0.2, DIRECTIONAL_ENABLED=True,
                        OVERNIGHT_ENABLED=True, LONG_RESEARCH_DISABLED=False)
    if target_value is not None and version.startswith(('v53', 'v54', 'v55', 'v56')):
        settings['T_TARGET_VALUE'] = target_value
        settings['T_POSITION_FRACTION'] = 1.0
    return dayt_strict.replay(version, daily, minute, rate=0.0005,
                              overrides=settings, symbol=symbol,
                              stock_name=stock_name, initial_cash=initial_cash,
                              initial_shares=initial_shares)


def summarize_daily(payload):
    output = {}
    for slip in (0.0, 0.01):
        rows = [row for row in payload['results'] if row['slip'] == slip]
        output[str(slip)] = dict(sessions=len(rows), excess_gross=sum(row['excess_gross'] for row in rows),
                                 trades=sum(len(row['trades']) for row in rows),
                                 failures=sum(bool(row['failure']) for row in rows))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', choices=sorted(STRATEGIES), required=True)
    parser.add_argument('--mode', choices=('daily', 'strict'), required=True)
    parser.add_argument('--start', default='20260801')
    parser.add_argument('--end', default='20260912')
    parser.add_argument('--symbol', default='601869.SH')
    parser.add_argument('--stock-name', default='601869')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--initial-cash', type=float, default=100000.0)
    parser.add_argument('--initial-shares', type=int, default=200)
    parser.add_argument('--target-value', type=float)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError('refusing to overwrite existing result: {}'.format(args.output))
    daily, minute = load_data(args.start, args.end, args.data_dir)
    if args.mode == 'daily':
        result = run_daily(args.version, daily, minute)
        result['summary'] = summarize_daily(result)
    else:
        result = run_strict(args.version, daily, minute, args.symbol,
                            args.stock_name, args.initial_cash,
                            args.initial_shares, args.target_value)
    strategy_path = ROOT / 'Stragety/MiniQMT_Stragety/DayT' / STRATEGIES[args.version]
    result.update(dict(version=args.version, mode=args.mode, start=args.start, end=args.end,
                       symbol=args.symbol, stock_name=args.stock_name,
                       source_sha256=source_hash(strategy_path), data_sessions=minute.index.str[:8].nunique(),
                       data_first=minute.index[0], data_last=minute.index[-1]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.mode == 'daily':
        print(json.dumps(result['summary'], ensure_ascii=False))
    else:
        print(json.dumps({key: result[key] for key in ('failure', 'account_net', 'excess_net', 'maximum_drawdown', 'completed_cycles')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
