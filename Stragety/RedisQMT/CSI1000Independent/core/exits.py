# -*- coding: utf-8 -*-
"""Check: python -m unittest discover -s Stragety/RedisQMT/CSI1000Independent/tests -v

V1 completed-minute exits: T+1, -5% stop, +8% activates 3% trailing,
previous-session MA20 break or 10 held sessions (entry session counts as 1).
Signals require next-bar execution; no live command. See ResearchProtocol.md.
"""
from dataclasses import dataclass
from dataclasses import replace
import math


@dataclass(frozen=True)
class Position:
    entry_date: str
    entry_price: float
    peak_price: float


@dataclass(frozen=True)
class ExitDecision:
    position: Position
    reason: str = ''


def completed_minute_exit(position, date, close, high, held_sessions, prior_below_ma20=False):
    """Return a signal, never a fill; prices must share the current share basis.

    The caller must apply verified corporate actions before this function and
    execute no earlier than the next valid minute, subject to sellability.
    """
    prices = (close, high, position.entry_price, position.peak_price)
    if not all(math.isfinite(price) and price > 0 for price in prices):
        raise ValueError('Missing/nonpositive prices cannot generate an exit')
    if date < position.entry_date or high + 1e-8 < close or held_sessions < 1:
        raise ValueError('Invalid position timeline or minute prices')
    updated = replace(position, peak_price=max(position.peak_price, high))
    if date == position.entry_date:
        return ExitDecision(updated)
    reason = ''
    if close <= position.entry_price * .95:
        reason = 'stop_loss'
    elif prior_below_ma20:
        reason = 'ma20_break'
    elif held_sessions >= 10:
        reason = 'holding_limit'
    elif updated.peak_price >= position.entry_price * 1.08 and close <= updated.peak_price * .97:
        reason = 'trailing_profit'
    return ExitDecision(updated, reason)
