# -*- coding: utf-8 -*-
"""Offline buy-intent planner; intents are not fills or executable broker orders.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Four occupied/reserved symbols, at most two ETFs, once per symbol/session.
No live command. Execution/fees/corporate-action ledger remain separate.
See ../ResearchDesign.md.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .signals import AccountRisk, entry_signal


@dataclass(frozen=True)
class BuyIntent:
    code: str
    kind: str
    created_at: object
    shares: int
    limit_price: float
    atr: float

    @property
    def reserved_cash(self):
        amount = self.shares * self.limit_price
        return amount + max(5, amount * 0.0003)


class PortfolioPlanner:
    def __init__(self):
        self.risk = AccountRisk()
        self.attempted = set()

    def plan_buys(self, completed_at, previous_session, candidates, minutes,
                  holdings, pending_buys, equity, cash, allow_market):
        """Call after processing confirmed fills and marking this minute's equity.

        holdings maps actually held codes to asset kinds; pending_buys contains
        still-unfilled reservations. Unfilled exits remain in holdings and cash.
        Input cash is broker-ledger cash before subtracting pending reservations.
        Caller enforces chronological minute processing and releases expired buys.
        """
        self.risk.update(equity)
        if not allow_market or self.risk.level == 2:
            return []
        occupied = dict(holdings)
        for intent in pending_buys:
            occupied[intent.code] = intent.kind
        available = cash - sum(intent.reserved_cash for intent in pending_buys)
        planned = []
        for candidate in sorted(candidates, key=lambda item: (-item.momentum, item.code)):
            if candidate.session != previous_session or previous_session >= completed_at.date():
                raise ValueError('Candidates must come from the preceding exchange session')
            key = (completed_at.date(), candidate.code)
            if candidate.code in occupied or key in self.attempted or len(occupied) >= 4:
                continue
            if candidate.kind == 'etf' and list(occupied.values()).count('etf') >= 2:
                continue
            frame = minutes[candidate.code]
            if not entry_signal(frame, completed_at, candidate.prior_close):
                continue
            bar = frame.loc[completed_at]
            # Rounding down never exceeds the prior-close chase cap.
            tick = Decimal('0.001') if candidate.kind == 'etf' else Decimal('0.01')
            raw = min(float(bar.close) * 1.001, candidate.prior_close * 1.03)
            limit = float((Decimal(str(raw)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)
            shares = self.risk.size(equity, available, limit, candidate.atr, float(bar.amount))
            if shares == 0:
                continue
            intent = BuyIntent(candidate.code, candidate.kind, completed_at,
                               shares, limit, candidate.atr)
            planned.append(intent)
            available -= intent.reserved_cash
            occupied[candidate.code] = candidate.kind
            self.attempted.add(key)
        return planned
