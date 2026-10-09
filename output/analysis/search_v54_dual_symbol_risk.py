"""Search one v54 cycle-risk setting across 600584 and 600105."""
import itertools
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dayt_strict import replay


SYMBOLS = (
    ('600584.SH', '长电科技', ROOT / 'analysis/dayt_600584_5m_data_20260913'),
    ('600105.SH', '永鼎股份', ROOT / 'analysis/dayt_600105_5m_data_20260913'),
)


def load_data(path):
    daily = pd.read_csv(path / '1d.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = pd.read_csv(path / '1m.csv', dtype={'time': str}).set_index('time').sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= '20260801') & (dates <= '20260913')]
    return daily, minute


def main():
    datasets = [(symbol, name, *load_data(path)) for symbol, name, path in SYMBOLS]
    rows = []
    combinations = itertools.product(
        (0.005, 0.01, 0.015, 0.02),
        (0.10, 0.20, 0.30, 0.50),
        (1, 2, 3),
    )
    for minimum, multiplier, holding_days in combinations:
        settings = {
            'T_TARGET_VALUE': 40000.0,
            'T_POSITION_FRACTION': 1.0,
            'CYCLE_MIN_ADVERSE_PCT': minimum,
            'CYCLE_ADVERSE_ATR_MULT': multiplier,
            'CYCLE_MAX_HOLDING_DAYS': holding_days,
        }
        outcomes = {}
        for symbol, name, daily, minute in datasets:
            result = replay(
                'v54_nomom', daily, minute, rate=0.0005,
                overrides=settings, symbol=symbol, stock_name=name,
                initial_cash=100000.0, initial_shares=1000)
            outcomes[symbol] = {
                'excess_net': result['excess_net'],
                'account_net': result['account_net'],
                'maximum_drawdown': result['maximum_drawdown'],
                'completed_cycles': result['completed_cycles'],
                'final_position': result['final_position'],
                'failure': result['failure'],
            }
        values = [item['excess_net'] for item in outcomes.values()]
        rows.append({
            'minimum': minimum,
            'multiplier': multiplier,
            'holding_days': holding_days,
            'minimum_excess': min(values),
            'combined_excess': sum(values),
            'outcomes': outcomes,
        })
        print(minimum, multiplier, holding_days, [round(value, 2) for value in values])
    rows.sort(key=lambda row: (row['minimum_excess'], row['combined_excess']), reverse=True)
    target = ROOT / 'analysis/dayt_v55_dual_symbol_optimization_20260913/grid.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print('BEST', json.dumps(rows[:10], ensure_ascii=False))


def search_cutoff():
    datasets = [(symbol, name, *load_data(path)) for symbol, name, path in SYMBOLS]
    rows = []
    combinations = itertools.product(
        (0.01, 0.02, 0.03),
        (0.10, 0.30, 0.75),
        ('14:30:00', '14:40:00', '14:50:00'),
    )
    for minimum, multiplier, cutoff in combinations:
        settings = {
            'T_TARGET_VALUE': 40000.0,
            'T_POSITION_FRACTION': 1.0,
            'CYCLE_MIN_ADVERSE_PCT': minimum,
            'CYCLE_ADVERSE_ATR_MULT': multiplier,
            'CYCLE_MAX_HOLDING_DAYS': 3,
        }
        outcomes = {}
        for symbol, name, daily, minute in datasets:
            result = replay(
                'v54_nomom', daily, minute, rate=0.0005,
                overrides=settings, symbol=symbol, stock_name=name,
                initial_cash=100000.0, initial_shares=1000,
                research_short_cutoff=cutoff)
            outcomes[symbol] = {
                'excess_net': result['excess_net'],
                'account_net': result['account_net'],
                'maximum_drawdown': result['maximum_drawdown'],
                'completed_cycles': result['completed_cycles'],
                'final_position': result['final_position'],
                'failure': result['failure'],
            }
        values = [item['excess_net'] for item in outcomes.values()]
        rows.append({
            'minimum': minimum,
            'multiplier': multiplier,
            'cutoff': cutoff,
            'minimum_excess': min(values),
            'combined_excess': sum(values),
            'outcomes': outcomes,
        })
        print(minimum, multiplier, cutoff, [round(value, 2) for value in values])
    rows.sort(key=lambda row: (row['minimum_excess'], row['combined_excess']), reverse=True)
    target = ROOT / 'analysis/dayt_v55_dual_symbol_optimization_20260913/grid_cutoff.json'
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print('BEST_CUTOFF', json.dumps(rows[:10], ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'cutoff':
        search_cutoff()
    else:
        main()
