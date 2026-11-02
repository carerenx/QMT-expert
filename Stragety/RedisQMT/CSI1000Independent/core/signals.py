# -*- coding: utf-8 -*-
"""Independent v1 signals; all daily decisions use the previous session."""
import numpy as np
import pandas as pd


def daily_features(raw, actions):
    frame = raw.sort_index().copy()
    if frame.index.duplicated().any():
        raise ValueError('Duplicate daily timestamps')
    valid = np.isfinite(frame[['open', 'high', 'low', 'close', 'volume', 'amount']]).all(axis=1)
    valid &= frame[['open', 'high', 'low', 'close']].gt(0).all(axis=1)
    valid &= frame.high.ge(frame[['open', 'low', 'close']].max(axis=1))
    valid &= frame.low.le(frame[['open', 'high', 'close']].min(axis=1))
    frame = frame.loc[valid].copy()
    factor = pd.Series(1.0, index=frame.index)
    for event in actions:
        if event['date'] >= str(frame.index[0]):
            factor.loc[factor.index >= event['date']] *= event['dr']
    frame['adjusted_close'] = frame.close * factor
    close = frame.adjusted_close
    frame['ma20'] = close.rolling(20).mean()
    frame['ma60'] = close.rolling(60).mean()
    frame['ret20'] = close.pct_change(20, fill_method=None)
    frame['ret3'] = close.pct_change(3, fill_method=None)
    frame['amount20'] = frame.amount.rolling(20).mean()
    frame['amount3'] = frame.amount.rolling(3).mean()
    frame['prior_amount20'] = frame.amount.shift(3).rolling(20).mean()
    frame['age'] = np.arange(1, len(frame) + 1)
    frame['eligible'] = (
        close.gt(frame.ma20) & close.gt(frame.ma60)
        & frame.ma20.gt(frame.ma20.shift(5))
        & frame.ret20.between(.05, .30) & frame.ret3.between(-.08, -.02)
        & frame.amount3.lt(frame.prior_amount20)
        & frame.amount20.ge(50_000_000) & frame.age.ge(120)
        & frame.volume.gt(0) & frame.amount.gt(0))
    frame['daily_exit'] = close.lt(frame.ma20)
    return frame


def ranked_candidates(features, benchmark, snapshots):
    calendar = benchmark.index.sort_values()
    prior = dict(zip(calendar[1:], calendar[:-1]))
    market = benchmark.close.gt(benchmark.close.rolling(60).mean())
    market_ret = benchmark.close.pct_change(20, fill_method=None)
    starts = sorted(snapshots)
    pools = {date: set(snapshots[max(start for start in starts if start <= date)])
             for date in calendar if date >= starts[0]}
    rows = []
    for code, frame in features.items():
        for trade_date, signal_date in prior.items():
            if trade_date not in pools or code not in pools[trade_date] or signal_date not in frame.index:
                continue
            row = frame.loc[signal_date]
            if bool(row.eligible) and bool(market.loc[signal_date]):
                rows.append({'trade_date': trade_date, 'signal_date': signal_date,
                             'code': code, 'score': float(row.ret20 - market_ret.loc[signal_date])})
    result = pd.DataFrame(rows, columns=['trade_date', 'signal_date', 'code', 'score'])
    result = result.sort_values(['trade_date', 'score', 'code'], ascending=[True, False, True])
    result['rank'] = result.groupby('trade_date').cumcount() + 1
    return result.loc[result['rank'] <= 10].reset_index(drop=True)


def minute_entry_signals(raw):
    """Use completed continuous-session bars; returned signal needs next-bar fill."""
    frame = raw.sort_index().copy()
    if frame.index.duplicated().any():
        raise ValueError('Duplicate minute timestamps')
    clock = frame.index.astype(str).str[-6:]
    continuous = ((clock >= '093100') & (clock <= '113000')) | ((clock >= '130100') & (clock <= '150000'))
    frame = frame.loc[continuous].copy()
    dates = frame.index.astype(str).str[:8]
    group = frame.groupby(dates, sort=False)
    previous_high = group.high.transform(lambda values: values.shift().rolling(15).max())
    # QMT stock volume is in lots; amount is yuan. Opening-auction bar excluded.
    vwap = group.amount.cumsum() / (group.volume.cumsum() * 100).replace(0, np.nan)
    clock = frame.index.astype(str).str[-6:]
    return (frame.close.gt(previous_high) & frame.close.gt(vwap)
            & frame.volume.gt(0) & frame.amount.gt(0)
            & (clock >= '094500') & (clock < '143000')).rename('entry')
