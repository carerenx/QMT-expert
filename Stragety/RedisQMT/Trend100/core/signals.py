# -*- coding: utf-8 -*-
"""Offline signal core; check from repository root:
python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v

60-session momentum, 20-session breakout, next-session opening-range confirmation.
Risk: 1.5% per trade, 25% position cap; drawdown 10% halves risk, 20% halts.
No data fetching, broker API, order submission or live command. See ../ResearchDesign.md.
"""
from dataclasses import dataclass
from datetime import time
from math import floor, isfinite

import pandas as pd


def daily_setup(frame, asof):
    """Input adjusted daily OHLCV/amount; caller certifies historical eligibility.

    asof is a completed daily timestamp. None means insufficient history or filters.
    Ranking is separate; this function alone does not certify a tradable candidate.
    """
    history = frame.loc[frame.index <= asof]
    if not history.index.is_monotonic_increasing or history.index.has_duplicates:
        raise ValueError('Daily history must have unique increasing timestamps')
    if len(history) < 61 or history.index[-1] != asof:
        return None
    history = history.iloc[-61:]
    if not history[['open', 'high', 'low', 'close']].gt(0).all().all():
        raise ValueError('Daily history contains nonpositive prices')
    close = float(history.close.iloc[-1])
    previous = history.close.shift(1)
    ranges = pd.concat([history.high - history.low,
                        (history.high - previous).abs(),
                        (history.low - previous).abs()], axis=1).max(axis=1)
    atr = float(ranges.iloc[-14:].mean())
    liquid = history.amount.iloc[-20:].mean() >= 100_000_000
    breakout = close > history.high.iloc[-21:-1].max()
    volume = history.volume.iloc[-1] >= history.volume.iloc[-21:-1].mean() * 1.5
    if not (liquid and breakout and volume and 0.02 <= atr / close <= 0.06):
        return None
    return {'momentum': close / float(history.close.iloc[-61]) - 1,
            'atr': atr, 'prior_close': close}


def market_open(closes, asof):
    history = closes.loc[closes.index <= asof]
    if not history.index.is_monotonic_increasing or history.index.has_duplicates:
        raise ValueError('Index history must have unique increasing timestamps')
    if len(history) < 60 or history.index[-1] != asof:
        return False
    if not history.iloc[-60:].map(isfinite).all() or not history.iloc[-60:].gt(0).all():
        raise ValueError('Index history contains invalid prices')
    mean60 = history.iloc[-60:].mean()
    return bool(history.iloc[-1] > mean60 and history.iloc[-20:].mean() > mean60)


def entry_signal(minutes, completed_at, prior_close):
    """Return signal only; execution must occur strictly after completed_at.

    Index uses Shanghai-local minute-start labels. Caller provides completed bars.
    No fill, fees or exchange-limit assumption is made here.
    """
    history = minutes.loc[minutes.index <= completed_at]
    history = history.loc[history.index.date == completed_at.date()]
    if history.empty or history.index[-1] != completed_at:
        return False
    if not time(9, 45) <= completed_at.time() <= time(10, 30):
        return False
    opening = history.between_time('09:30', '09:44')
    expected = pd.date_range(completed_at.normalize() + pd.Timedelta(hours=9, minutes=30),
                             periods=15, freq='min')
    if not opening.index.equals(expected):
        return False
    price = float(history.close.iloc[-1])
    return bool(price > opening.high.max() and price <= prior_close * 1.03)


@dataclass
class AccountRisk:
    peak: float = 150_000.0
    level: int = 0

    def update(self, equity):
        if not isfinite(equity) or equity < 0:
            raise ValueError('Equity must be finite and nonnegative')
        self.peak = max(self.peak, equity)
        if equity <= self.peak * 0.8:
            self.level = 2
        elif equity <= self.peak * 0.9:
            self.level = max(self.level, 1)

    def size(self, equity, cash, limit_price, atr, signal_amount):
        """100-share lots; reserve min-5-yuan, 3bp commission; no executed fill."""
        if self.level == 2:
            return 0
        stop_distance = min(max(2 * atr, limit_price * 0.06), limit_price * 0.10)
        scale = 0.5 if self.level == 1 else 1.0
        risk_shares = equity * 0.015 * scale / stop_distance
        cap_shares = equity * 0.25 * scale / limit_price
        liquidity_shares = signal_amount * 0.01 / limit_price
        cash_shares = min(max(cash - 5, 0) / limit_price,
                          cash / (limit_price * 1.0003))
        return max(0, floor(min(risk_shares, cap_shares, liquidity_shares,
                                cash_shares) / 100) * 100)


@dataclass
class PositionExit:
    entry_price: float
    entry_atr: float
    entry_date: object
    highest_close: float = 0.0

    def observe(self, completed_at, close, held_sessions, sellable):
        """Return a pending next-minute exit reason, never an assumed fill.

        held_sessions counts exchange sessions, entry day=1; sellable is actual
        available volume. Portfolio halt and exchange constraints belong to replay.
        """
        self.highest_close = max(self.highest_close, self.entry_price, close)
        distance = min(max(2 * self.entry_atr, self.entry_price * 0.06),
                       self.entry_price * 0.10)
        stop = self.entry_price - distance
        if self.highest_close >= self.entry_price * 1.12:
            stop = max(stop, self.highest_close * 0.94)
        if completed_at.date() <= self.entry_date or sellable <= 0:
            return None
        if close <= stop:
            return 'stop'
        if held_sessions >= 20 and completed_at.time() >= time(14, 55):
            return 'holding_limit'
        return None
