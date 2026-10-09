"""Read-only reproducible daily-data audit; deliberately does not simulate fills.

Run: python analysis/validate_v51_history.py
The proxy counts below are NOT v50/v51 intraday strategy trigger rates.
"""
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Stragety/MiniQMT_Stragety'))
from core.quantile_trend_regime import compute_quantile_trend_regime
from core.intraday_strength import lower_excursion_units


def audit(rows):
    rows = sorted(rows, key=lambda r: r['date'])
    results = []
    for i in range(30, len(rows)):
        history = rows[max(0, i - 80):i]  # strictly excludes current day
        values = [[float(r[field]) for r in history]
                  for field in ('open', 'high', 'low', 'close', 'volume')]
        opening = float(rows[i]['open'])
        signal = compute_quantile_trend_regime(*values, today_open=opening)
        if signal is None:
            continue
        lower = lower_excursion_units(*values[:4])
        original = signal['sell_trigger']
        # Daily lower-reference proxy only; no intraday VWAP, low, strength,
        # warmup, pullback confirmation, or order sequence is manufactured.
        floor = min(original, float(np.ceil(opening * (1 + signal['atr_pct'] * lower) * 100 - 1e-9) / 100))
        results.append(dict(date=rows[i]['date'], original_touch=bool(rows[i]['high'] >= original),
                            floor_touch=bool(rows[i]['high'] >= floor)))
    split = int(len(results) * .7)
    summary = dict(source_days=len(rows), first=rows[0]['date'], last=rows[-1]['date'])
    for name, subset in (('check_70pct', results[:split]), ('holdout_30pct', results[split:])):
        summary[name] = dict(days=len(subset), first=subset[0]['date'], last=subset[-1]['date'],
                             original_daily_touch=sum(r['original_touch'] for r in subset),
                             q35_floor_daily_touch=sum(r['floor_touch'] for r in subset))
    returns = np.array([r['close'] for r in rows], dtype=float)
    returns = returns[1:] / returns[:-1] - 1
    groups = {True: [], False: []}
    for i in range(22, len(rows) - 1):
        strong = bool(returns[i - 1] > 0 and
                      returns[i - 1] >= np.quantile(returns[max(0, i - 81):i - 1], .75))
        groups[strong].append(rows[i + 1])
    summary['yesterday_strength_exploration'] = {
        str(key): dict(samples=len(group), next_high_open_median_pct=float(np.median([
            r['high'] / r['open'] - 1 for r in group])) * 100)
        for key, group in groups.items()}
    summary['acceptance'] = dict(status='INSUFFICIENT_INTRADAY_DATA_AND_ACTUAL_FEES',
                                 completed_cycles=None, net_pnl=None, unclosed_exposure=None,
                                 vs_hold=None, max_drawdown=None, allow_active=False)
    return summary


if __name__ == '__main__':
    daily = ROOT / 'output/601869_klines.json'
    result = audit(json.loads(daily.read_text(encoding='utf-8')))
    summaries = json.loads((ROOT / 'output/backtest_5min_raw.json').read_text(encoding='utf-8'))
    result['legacy_5min_file'] = dict(records=len(summaries),
                                    keys=sorted(summaries[0]),
                                    note='daily/result/seq summaries, not raw timestamped minute bars')
    print(json.dumps(result, ensure_ascii=False, indent=2))
