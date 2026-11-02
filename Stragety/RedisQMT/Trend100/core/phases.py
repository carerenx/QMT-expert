# -*- coding: utf-8 -*-
"""Dated closing-phase rules for the current research window, not a data adapter.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
SH ETFs continuously traded at the close before 2026-07-06; stock/SZ closing
auction and the SH ETF transition must not be conflated. See ../AlternativeDataSources.md.
No live command or network access.
"""
from datetime import date


def has_closing_auction(exchange, kind, session):
    if exchange not in ('SH', 'SZ') or kind not in ('stock', 'etf'):
        raise ValueError('Unknown exchange or asset kind for phase rules')
    if not date(2025, 1, 1) <= session <= date(2026, 11, 2):
        raise ValueError('Phase rules not verified outside 2025-01-01 through 2026-11-02')
    return not (exchange == 'SH' and kind == 'etf' and session < date(2026, 7, 6))
