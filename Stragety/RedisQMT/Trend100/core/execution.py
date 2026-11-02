# -*- coding: utf-8 -*-
"""Offline next-minute matching and cash ledger, not a broker connection.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Signals fill no earlier than the next certified clock minute; sells precede buys.
T+1, 1% volume/amount participation, dated fees, one-minute buys and persistent sells.
No live command. Explicit fee rows are research inputs. See ../ResearchDesign.md.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
from math import floor, isfinite

import pandas as pd

from .signals import AccountRisk, PositionExit


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class FeeRow:
    first: object
    last: object
    kind: str
    commission: float
    minimum: float
    sell_stamp: float
    transfer: float


class FeeSchedule:
    def __init__(self, rows):
        self.rows = list(rows)
        for row in self.rows:
            rates = (row.commission, row.minimum, row.sell_stamp, row.transfer)
            if row.first > row.last or any(not isfinite(rate) or rate < 0 for rate in rates):
                raise ValueError('Invalid dated fee row')

    def at(self, day, kind):
        rows = [row for row in self.rows if row.first <= day <= row.last and row.kind == kind]
        if len(rows) != 1:
            raise ValueError('Missing or overlapping fee coverage for ' + kind + ' on ' + str(day))
        return rows[0]


@dataclass(frozen=True)
class MinuteBar:
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    tradable: bool
    limit_up: float
    limit_down: float

    def validate(self):
        prices = (self.open, self.high, self.low, self.close, self.limit_up, self.limit_down)
        if (any(not isfinite(price) or price <= 0 for price in prices)
                or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
                or not self.limit_down <= self.low <= self.high <= self.limit_up
                or not isfinite(self.amount) or self.amount < 0
                or not isfinite(self.volume) or self.volume < 0):
            raise ValueError('Invalid unadjusted minute bar or exchange limits')


@dataclass
class Holding:
    kind: str
    shares: int
    entry_date: object
    exit_state: PositionExit
    cost: float
    locked_until: dict = field(default_factory=dict)

    def sellable(self, day):
        if day <= self.entry_date:
            return 0
        return self.shares - sum(shares for unlock, shares in self.locked_until.items() if day < unlock)


@dataclass
class Pending:
    code: str
    kind: str
    side: str
    created_at: object
    shares: int
    limit_price: float | None
    atr: float
    reason: str
    # Minimum commission is accumulated per order/session, not each partial fill.
    day_amount: dict = field(default_factory=dict)
    day_commission: dict = field(default_factory=dict)


class PaperLedger:
    def __init__(self, clock, fees, slippage=0.001):
        self.clock = pd.DatetimeIndex(clock)
        if (self.clock.empty or not self.clock.is_monotonic_increasing
                or self.clock.has_duplicates):
            raise ValueError('Need a unique increasing certified minute clock')
        if not isfinite(slippage) or slippage < 0:
            raise ValueError('Invalid slippage')
        self.fees = fees
        self.slippage = slippage
        self.cash = 150_000.0
        self.cash_receivable = 0.0
        self.share_receivable = {}
        self.holdings = {}
        self.pending = []
        self.fills = []
        self.cancellations = []
        self.curve = []
        self.risk = AccountRisk()
        self.cursor = -1

    @property
    def now(self):
        if self.cursor < 0:
            raise ValueError('Process a minute before creating an order')
        return self.clock[self.cursor]

    def buy(self, intent):
        if intent.created_at != self.now:
            raise ValueError('Buy must be created at the current completed minute')
        if self.cursor + 1 >= len(self.clock):
            return
        occupied = {code: holding.kind for code, holding in self.holdings.items()}
        occupied.update({order.code: order.kind for order in self.pending if order.side == 'buy'})
        if self.risk.level == 2 or intent.code in occupied or len(occupied) >= 4:
            raise ValueError('Buy violates halt, position or reservation constraints')
        if intent.kind == 'etf' and list(occupied.values()).count('etf') >= 2:
            raise ValueError('ETF position limit')
        if intent.shares <= 0 or intent.shares % 100:
            raise ValueError('Buy shares must be positive whole lots')
        if (intent.kind not in ('stock', 'etf') or not isfinite(intent.limit_price)
                or intent.limit_price <= 0 or not isfinite(intent.atr) or intent.atr <= 0):
            raise ValueError('Invalid buy asset, price or ATR')
        self.pending.append(Pending(intent.code, intent.kind, 'buy', self.now,
                                    intent.shares, intent.limit_price, intent.atr, 'entry'))

    def sell(self, code, reason):
        if any(order.code == code and order.side == 'sell' for order in self.pending):
            return
        holding = self.holdings[code]
        self.pending.append(Pending(code, holding.kind, 'sell', self.now,
                                    holding.shares, None, 0, reason))

    def _charges(self, order, gross):
        day = self.now.date()
        row = self.fees.at(day, order.kind)
        total = order.day_amount.get(day, 0) + gross
        cumulative = _money(max(row.minimum, total * row.commission))
        commission = _money(cumulative - order.day_commission.get(day, 0))
        stamp = _money(gross * row.sell_stamp) if order.side == 'sell' else 0
        transfer = _money(gross * row.transfer)
        return _money(commission + stamp + transfer), cumulative

    def _match(self, order, bar, used):
        buying = order.side == 'buy'
        if order.created_at >= self.now or not bar.tradable or bar.volume <= 0 or bar.amount <= 0:
            return
        # Minute OHLC does not certify queue position: a bar touching the adverse
        # limit is conservatively unfillable on that side, even if it later unlocks.
        if (buying and bar.high >= bar.limit_up) or (not buying and bar.low <= bar.limit_down):
            return
        if not buying:
            holding = self.holdings[order.code]
            if holding.sellable(self.now.date()) <= 0:
                return
        tick = Decimal('0.001') if order.kind == 'etf' else Decimal('0.01')
        adjustment = Decimal('1') + Decimal(str(self.slippage)) * (1 if buying else -1)
        raw = Decimal(str(bar.open)) * adjustment
        rounding = ROUND_CEILING if buying else ROUND_FLOOR
        price = float((raw / tick).to_integral_value(rounding=rounding) * tick)
        if not bar.low <= price <= bar.high:
            return
        if buying and price > order.limit_price:
            return
        consumed_shares, consumed_amount = used.get(order.code, (0, 0.0))
        capacity = min(bar.volume * 0.01 - consumed_shares,
                       (bar.amount * 0.01 - consumed_amount) / price)
        shares = min(order.shares, max(0, floor(capacity / 100) * 100))
        if not buying:
            shares = min(shares, self.holdings[order.code].sellable(self.now.date()))
        while shares > 0:
            gross = _money(shares * price)
            fee, cumulative = self._charges(order, gross)
            if not buying or gross + fee <= self.cash:
                break
            shares -= 100
        if shares <= 0:
            return
        self.cash = _money(self.cash + (-gross if buying else gross) - fee)
        order.day_amount[self.now.date()] = order.day_amount.get(self.now.date(), 0) + gross
        order.day_commission[self.now.date()] = cumulative
        order.shares -= shares
        used[order.code] = (consumed_shares + shares, consumed_amount + gross)
        if buying:
            self.holdings[order.code] = Holding(order.kind, shares, self.now.date(),
                PositionExit(price, order.atr, self.now.date()), gross + fee)
        else:
            holding = self.holdings[order.code]
            holding.cost *= (holding.shares - shares) / holding.shares
            holding.shares -= shares
            if holding.shares == 0:
                del self.holdings[order.code]
        self.fills.append({'at': self.now, 'code': order.code, 'side': order.side,
                           'shares': shares, 'price': price, 'gross': gross,
                           'fees': fee, 'reason': order.reason, 'cash_after': self.cash})

    def process(self, completed_at, bars):
        """Finish the next clock bar, settle pending sells then buys, mark equity.

        Open is a model reference; next-bar price-range and total-volume checks
        become known only at bar completion. No fill proceeds are exposed sooner.
        Suspended holdings still require certified positive valuation bars.
        """
        next_cursor = self.cursor + 1
        if next_cursor >= len(self.clock) or self.clock[next_cursor] != completed_at:
            raise ValueError('Cannot skip, repeat or reorder clock minutes')
        needed = set(self.holdings) | set(self.share_receivable) | {order.code for order in self.pending}
        for code in needed:
            if code not in bars:
                raise ValueError('Missing minute/valuation bar: ' + code)
            bars[code].validate()
        for order in self.pending:
            self.fees.at(completed_at.date(), order.kind)
        self.cursor = next_cursor
        used = {}
        for order in sorted(self.pending, key=lambda item: item.side != 'sell'):
            self._match(order, bars[order.code], used)
        remaining = []
        for order in self.pending:
            if order.side == 'buy':
                if order.shares:
                    self.cancellations.append({'at': self.now, 'code': order.code,
                                               'remaining': order.shares, 'reason': 'buy_window_expired'})
            elif order.shares:
                remaining.append(order)
        self.pending = remaining
        equity = self.cash + self.cash_receivable + sum(holding.shares * bars[code].close
                                 for code, holding in self.holdings.items())
        equity += sum(shares * bars[code].close for code, shares in self.share_receivable.items())
        self.risk.update(equity)
        self.curve.append({'at': self.now, 'cash': self.cash, 'equity': equity,
                           'cash_receivable': self.cash_receivable,
                           'risk_level': self.risk.level})
        if self.risk.level == 2:
            for code in list(self.holdings):
                self.sell(code, 'account_halt')
        return equity
