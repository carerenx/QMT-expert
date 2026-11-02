# -*- coding: utf-8 -*-
"""Historical candidate ranking; no network, trading or performance claims.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Rank stock/ETF top deciles before breakout filters; deduplicate ETF benchmarks.
Inputs must be point-in-time certified by the future data loader. See ../ResearchDesign.md.
"""
from dataclasses import dataclass
from datetime import date, datetime, time
from math import ceil, isfinite

import pandas as pd

from .signals import daily_setup


@dataclass(frozen=True)
class SecurityState:
    code: str
    kind: str
    session: date
    known_at: datetime
    listed_sessions: int
    suspended: bool
    st: bool
    delisting: bool
    board: str
    etf_class: str
    benchmark: str


@dataclass(frozen=True)
class Candidate:
    code: str
    kind: str
    session: date
    momentum: float
    atr: float
    prior_close: float


def rank_candidates(states, daily, sessions, decision_at):
    """Naive Shanghai-local times; daily index and calendar use local midnight.

    Metadata must describe the decision session and already be known then.
    A missing eligible asset history raises instead of silently shrinking the pool.
    Returns only top-decile names that also pass the frozen breakout conditions.
    """
    session = decision_at.date()
    if decision_at.time() < time(15):
        raise ValueError('Daily decisions require the completed close')
    expected = pd.DatetimeIndex(sessions)[-61:]
    if len(expected) < 61 or expected[-1].date() != session:
        raise ValueError('Need 61 certified exchange sessions ending on decision day')
    if not expected.is_monotonic_increasing or expected.has_duplicates:
        raise ValueError('Exchange sessions must be unique and increasing')
    rows = []
    seen = set()
    for state in states:
        if state.code in seen:
            raise ValueError('Duplicate historical security: ' + state.code)
        seen.add(state.code)
        if state.session != session or state.known_at > decision_at:
            raise ValueError('Security metadata is stale or unavailable at decision time')
        if state.kind not in ('stock', 'etf'):
            raise ValueError('Unknown asset kind: ' + state.kind)
        if state.listed_sessions < 250 or state.suspended or state.delisting:
            continue
        if state.kind == 'stock' and (state.board != 'mainboard' or state.st):
            continue
        if state.kind == 'etf' and state.etf_class != 'domestic_equity':
            continue
        if state.kind == 'etf' and not state.benchmark:
            raise ValueError('Missing historical ETF benchmark: ' + state.code)
        frame = daily.get(state.code)
        if frame is None:
            raise ValueError('Missing eligible asset history: ' + state.code)
        history = frame.loc[frame.index <= expected[-1]].iloc[-61:]
        if not history.index.equals(expected):
            raise ValueError('Incomplete daily sessions: ' + state.code)
        prices = history[['open', 'high', 'low', 'close']]
        if not prices.map(isfinite).all().all() or not prices.gt(0).all().all():
            raise ValueError('Invalid prices: ' + state.code)
        if (not history[['amount', 'volume']].map(isfinite).all().all()
                or history[['amount', 'volume']].lt(0).any().any()):
            raise ValueError('Invalid liquidity: ' + state.code)
        amount = float(history.amount.iloc[-20:].mean())
        if amount < 100_000_000:
            continue
        momentum = float(history.close.iloc[-1] / history.close.iloc[0] - 1)
        rows.append((state, history, amount, momentum))

    # Pick the most liquid ETF before computing its class's decile denominator.
    etfs = {}
    stocks = []
    for row in sorted(rows, key=lambda item: (-item[2], item[0].code)):
        if row[0].kind == 'etf':
            etfs.setdefault(row[0].benchmark, row)
        else:
            stocks.append(row)
    selected = []
    for group in (stocks, list(etfs.values())):
        ranked = sorted(group, key=lambda item: (-item[3], item[0].code))
        for state, history, amount, momentum in ranked[:ceil(len(ranked) / 10)]:
            setup = daily_setup(history, expected[-1])
            if setup is not None:
                selected.append(Candidate(state.code, state.kind, session, momentum,
                                          setup['atr'], setup['prior_close']))
    return sorted(selected, key=lambda item: (-item.momentum, item.code))
