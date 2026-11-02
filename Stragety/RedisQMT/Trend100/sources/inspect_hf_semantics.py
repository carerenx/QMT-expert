# -*- coding: utf-8 -*-
"""Inspect local historical samples; run from repository root:
python Stragety/RedisQMT/Trend100/sources/inspect_hf_semantics.py

Checks 241-label grids, opening/closing-edge behavior and amount differences.
No network, conversion into executable bars, parameter tuning or performance output.
See ../AlternativeDataSources.md. Source clock interpretation remains unverified.
"""
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.Trend100.core.phases import has_closing_auction


OUT = ROOT / 'output/analysis/trend100_alternative_sources_20261102'


def inspect(kind, asset_kind):
    frame = pd.read_parquet(OUT / (kind + '_annual_subset_20250807_20260806.parquet'))
    days = []
    for day, part in frame.groupby(frame.index.normalize()):
        morning = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), periods=121, freq='min')
        afternoon = pd.date_range(day + pd.Timedelta(hours=13, minutes=1), periods=120, freq='min')
        expected = morning.append(afternoon)
        opening = part.between_time('09:30', '09:30').iloc[0]
        late = part.between_time('14:58', '14:59')
        auction = has_closing_auction('SH', asset_kind, day.date())
        days.append({'session': str(day.date()), 'matches_observed_241_grid': part.index.equals(expected),
            'opening_nonflat': bool(opening.high - opening.low > 1e-5),
            'late_positive_volume': bool(late.volume.gt(0).any()),
            'closing_auction_by_dated_rule': auction,
            'late_volume_under_end_label_hypothesis_needs_explanation': bool(auction and late.volume.gt(0).any())})
    day_frame = pd.DataFrame(days)
    day_frame.to_csv(OUT / (kind + '_clock_diagnostics.csv'), index=False)
    comparison = pd.read_csv(OUT / (kind + '_daily_crosscheck.csv')).set_index('Unnamed: 0')
    discrepancies = []
    for day in comparison.index[comparison.amount_delta.abs() > 100]:
        amounts = frame.loc[day].turnover.to_numpy()
        as32 = amounts.astype('float32')
        upper = np.nextafter(as32, np.float32(np.inf)).astype('float64') - as32.astype('float64')
        lower = as32.astype('float64') - np.nextafter(as32, np.float32(-np.inf)).astype('float64')
        discrepancies.append({'session': day, 'delta_cny': float(comparison.loc[day, 'amount_delta']),
            'relative_to_sample_daily_turnover': float(abs(comparison.loc[day, 'amount_delta']) / amounts.sum()),
            'exact_float32_fraction': float((as32.astype('float64') == amounts).mean()),
            'hypothetical_float32_sum_half_ulp': float((np.maximum(upper, lower) / 2).sum()),
            'cause_verified': False})
    return {'days': len(day_frame), 'matches_241_grid_days': int(day_frame.matches_observed_241_grid.sum()),
        'opening_nonflat_days': int(day_frame.opening_nonflat.sum()),
        'late_volume_hypothesis_conflict_days': int(day_frame.late_volume_under_end_label_hypothesis_needs_explanation.sum()),
        'amount_discrepancies': discrepancies, 'clock_semantics_verified': False,
        'executable_conversion_created': False, 'formal_backtest_ready': False}


def main():
    records = {'hf_stock': inspect('hf_stock', 'stock'), 'hf_etf': inspect('hf_etf', 'etf')}
    (OUT / 'hf_semantics_checks.json').write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
