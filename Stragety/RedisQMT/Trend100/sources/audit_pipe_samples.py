# -*- coding: utf-8 -*-
"""Run: python Stragety/RedisQMT/Trend100/sources/audit_pipe_samples.py

Local-only diagnostics: retain partial sessions, compare minute/day aggregates,
and compare observed tick cumulative increments with alternative minute labels.
No executable data conversion, formal audit threshold changes or return output.
See ../PipeClockAudit.md. Inputs are the preserved Named Pipe source samples.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[4] / 'output/analysis/trend100_alternative_sources_20261102'


def load(path, fmt):
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index.astype(str), format=fmt)
    return frame


def inspect(code, tick_name):
    minute = load(OUT / f'pipe_qmt_{code}_target_1m.csv', '%Y%m%d%H%M%S')
    daily = load(OUT / f'pipe_qmt_{code}_target_1d.csv', '%Y%m%d')
    ticks = load(OUT / tick_name, '%Y%m%d%H%M%S')
    counts = minute.groupby(minute.index.normalize()).size()
    aggregates = minute.groupby(minute.index.normalize()).agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum', 'amount': 'sum'})
    joined = aggregates.join(daily, how='inner', lsuffix='_minute', rsuffix='_daily')
    joined['minute_rows'] = counts.reindex(joined.index)
    for column in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        joined[column + '_delta'] = joined[column + '_minute'] - joined[column + '_daily']
    joined.to_csv(OUT / f'pipe_daily_reconciliation_{code}.csv')
    complete = joined[joined.minute_rows.eq(241)]
    # The count is descriptive: exact clock semantics are separately assessed.
    rows = []
    previous = None
    previous_label = None
    for label, group in ticks.groupby(ticks.index.floor('min')):
        last = group.iloc[-1]
        contiguous = previous_label is not None and label - previous_label == pd.Timedelta(minutes=1)
        opening = label.time() == pd.Timestamp('09:30').time() and previous_label is not None and previous_label.time() == pd.Timestamp('09:25').time()
        if (contiguous or opening) and (label.hour >= 9) and last.lastPrice > 0:
            for offset in [-1, 0, 1]:
                target = label + pd.Timedelta(minutes=offset)
                if target not in minute.index:
                    continue
                bar = minute.loc[target]
                rows.append({'tick_minute': label, 'bar_label': target, 'offset_minutes': offset,
                    'close_delta': bar.close-last.lastPrice,
                    'volume_delta': bar.volume-(last.volume-previous.volume),
                    'amount_delta': bar.amount-(last.amount-previous.amount)})
        previous = last
        previous_label = label
    comparison = pd.DataFrame(rows)
    comparison.to_csv(OUT / f'pipe_tick_clock_comparison_{code}.csv', index=False)
    offsets = {}
    for offset, part in comparison.groupby('offset_minutes'):
        offsets[str(offset)] = {'comparisons': len(part),
            'close_matches': int(part.close_delta.abs().le(1e-9).sum()),
            'volume_matches': int(part.volume_delta.eq(0).sum()),
            'amount_within_one_cny': int(part.amount_delta.abs().le(1).sum()),
            'max_abs_amount_delta': float(part.amount_delta.abs().max())}
    interior = comparison[comparison.offset_minutes.eq(1)
        & comparison.tick_minute.dt.strftime('%H:%M').ne('11:29')
        & comparison.tick_minute.dt.strftime('%H:%M').ne('14:59')]
    edges = []
    day = ticks.index[0].normalize()
    for name, label, before, through in [
        ('opening_auction', '09:30', None, '09:25:59'),
        ('morning_close', '11:30', '11:29', '11:30:59'),
        ('closing_auction', '15:00', '15:00', '15:00:59')]:
        stamp = day + pd.Timedelta(label+':00')
        last_rows = ticks[ticks.index <= day + pd.Timedelta(through)]
        if last_rows.empty or stamp not in minute.index:
            continue
        last = last_rows.iloc[-1]
        baseline = None if before is None else ticks[ticks.index < day + pd.Timedelta(before+':00')].iloc[-1]
        bar = minute.loc[stamp]
        edges.append({'edge': name, 'source_label': str(stamp),
            'close_delta': float(bar.close-last.lastPrice),
            'volume_delta': float(bar.volume-last.volume+(0 if baseline is None else baseline.volume)),
            'amount_delta': float(bar.amount-last.amount+(0 if baseline is None else baseline.amount))})
    share_volume = minute.volume * 100
    exact_amount = minute.amount.between(minute.low*share_volume-1, minute.high*share_volume+1)
    return {'rows': len(minute), 'duplicate_labels': int(minute.index.duplicated().sum()),
        'nonfinite_cells': int((~np.isfinite(minute)).sum().sum()),
        'daily_comparisons_all': len(joined), 'daily_comparisons_241_rows': len(complete),
        'daily_max_abs_delta_241_rows': complete.filter(like='_delta').abs().max().to_dict(),
        'amount_price_inconsistent_if_hands_times_100': int((~exact_amount).sum()),
        'tick_rows': len(ticks), 'tick_cumulative_volume_decreases': int(ticks.volume.diff().lt(0).sum()),
        'tick_cumulative_amount_decreases': int(ticks.amount.diff().lt(0).sum()),
        'clock_offset_comparison': offsets,
        'interior_excluding_morning_and_closing_boundary': {
            'comparisons': len(interior), 'close_matches': int(interior.close_delta.abs().le(1e-9).sum()),
            'volume_matches': int(interior.volume_delta.eq(0).sum()),
            'max_abs_amount_delta_cny': float(interior.amount_delta.abs().max())},
        'special_edges': edges, 'formal_backtest_ready': False}


if __name__ == '__main__':
    result = {
        '600000.SH': inspect('600000.SH', 'pipe_ticks_600000.SH_20261009_extended.csv'),
        '510300.SH': inspect('510300.SH', 'pipe_ticks_510300.SH_20261009.csv')}
    (OUT / 'pipe_local_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
