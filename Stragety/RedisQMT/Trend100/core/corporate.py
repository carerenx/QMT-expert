# -*- coding: utf-8 -*-
"""Distribution entitlement ledger; never infer dividends from price gaps.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Record-date shares, ex-date receivables/reference change, actual payment/delivery.
Net cash and price factors require audited source/tax-model inputs, not guesses.
No network or live command. See ../ResearchDesign.md for unsupported actions.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import time, timedelta
from math import isfinite

from .execution import Holding, _money


@dataclass(frozen=True)
class Distribution:
    event_id: str
    code: str
    announced_at: object
    record_session: object
    ex_session: object
    payment_session: object
    net_cash_per_share: float
    bonus_per_share: float
    delivery_session: object
    sellable_session: object
    price_factor: float
    source: str
    tax_model_source: str

    def __post_init__(self):
        if not self.event_id or not self.source or not self.tax_model_source:
            raise ValueError('Distribution id, source and tax model are required')
        if (self.announced_at.date() > self.record_session
                or not self.record_session < self.ex_session <= self.payment_session
                or not self.ex_session <= self.delivery_session <= self.sellable_session):
            raise ValueError('Invalid distribution timeline')
        values = (self.net_cash_per_share, self.bonus_per_share, self.price_factor)
        if any(not isfinite(value) for value in values) or min(values[:2]) < 0 or self.price_factor <= 0:
            raise ValueError('Invalid cash, bonus or reference factor')


class CorporateBook:
    def __init__(self, events):
        self.events = list(events)
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError('Duplicate distribution id')
        if len({(event.code, event.ex_session) for event in self.events}) != len(self.events):
            raise ValueError('Combine same-security same-ex-date distribution records first')
        self.entitlements = {}
        self.ex_applied = set()
        self.paid = set()
        self.delivered = set()
        self.entries = []
        self.last_open = None

    def record_close(self, completed_at, ledger):
        if completed_at.time() != time(14, 59):
            raise ValueError('Record snapshot requires the completed final minute')
        for event in self.events:
            if event.record_session != completed_at.date() or event.event_id in self.entitlements:
                continue
            if event.announced_at > completed_at + timedelta(minutes=1):
                raise ValueError('Distribution was not known at the record close')
            holding = ledger.holdings.get(event.code)
            self.entitlements[event.event_id] = deepcopy(holding)

    def open_session(self, day, ledger):
        """Call once before the day's first minute; entitlement assets are not cash.

        This is a normalized unilateral distribution, not a rights-subscription,
        merger or split parser. Fractional allocations require explicit resolution.
        """
        if self.last_open is not None and day <= self.last_open:
            raise ValueError('Corporate sessions must advance once per day')
        for event in self.events:
            key = event.event_id
            if event.ex_session <= day and key not in self.ex_applied:
                if key not in self.entitlements:
                    raise ValueError('Missing record-date snapshot: ' + key)
                if ledger.share_receivable.get(event.code, 0):
                    raise ValueError('Overlapping undelivered share distributions need resolution: ' + key)
                recorded = self.entitlements[key]
                quantity = recorded.shares if recorded else 0
                bonus = quantity * event.bonus_per_share
                if abs(bonus - round(bonus)) > 1e-8:
                    raise ValueError('Fractional allocation needs audited resolution: ' + key)
                current = ledger.holdings.get(event.code)
                if current is not None:
                    state = current.exit_state
                    state.entry_price *= event.price_factor
                    state.entry_atr *= event.price_factor
                    state.highest_close *= event.price_factor
                if recorded:
                    state = recorded.exit_state
                    state.entry_price *= event.price_factor
                    state.entry_atr *= event.price_factor
                    state.highest_close *= event.price_factor
                cash = _money(quantity * event.net_cash_per_share)
                ledger.cash_receivable = _money(ledger.cash_receivable + cash)
                if bonus:
                    ledger.share_receivable[event.code] = round(bonus)
                self.ex_applied.add(key)
                self.entries.append({'session': day, 'event_id': key, 'kind': 'ex',
                                     'cash': cash, 'bonus': round(bonus)})
            if key not in self.ex_applied:
                continue
            recorded = self.entitlements[key]
            quantity = recorded.shares if recorded else 0
            if event.payment_session <= day and key not in self.paid:
                cash = _money(quantity * event.net_cash_per_share)
                ledger.cash = _money(ledger.cash + cash)
                ledger.cash_receivable = _money(ledger.cash_receivable - cash)
                self.paid.add(key)
                self.entries.append({'session': day, 'event_id': key, 'kind': 'payment', 'cash': cash})
            if event.delivery_session <= day and key not in self.delivered:
                bonus = round(quantity * event.bonus_per_share)
                if bonus:
                    holding = ledger.holdings.get(event.code)
                    if holding is None:
                        holding = Holding(recorded.kind, 0, recorded.entry_date,
                                          deepcopy(recorded.exit_state), 0)
                        ledger.holdings[event.code] = holding
                    holding.shares += bonus
                    holding.locked_until[event.sellable_session] = holding.locked_until.get(event.sellable_session, 0) + bonus
                    for order in ledger.pending:
                        if order.code == event.code and order.side == 'sell':
                            order.shares += bonus
                    ledger.share_receivable[event.code] -= bonus
                if ledger.share_receivable.get(event.code) == 0:
                    ledger.share_receivable.pop(event.code, None)
                self.delivered.add(key)
                self.entries.append({'session': day, 'event_id': key, 'kind': 'delivery', 'shares': bonus})
        self.last_open = day

    def reference_factors(self, day):
        factors = {}
        for event in self.events:
            if event.ex_session == day:
                factors[event.code] = factors.get(event.code, 1.0) * event.price_factor
        return factors

    def reserved_holdings(self):
        reserved = {}
        for event in self.events:
            recorded = self.entitlements.get(event.event_id)
            if (recorded and event.bonus_per_share > 0 and event.event_id in self.ex_applied
                    and event.event_id not in self.delivered):
                reserved[event.code] = recorded.kind
        return reserved
