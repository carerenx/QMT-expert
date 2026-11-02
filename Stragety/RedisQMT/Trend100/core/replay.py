# -*- coding: utf-8 -*-
"""Offline continuous replay of the frozen Trend100 rules; no data fetching.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Prior-close ranking/gate -> next-session minute signals -> next-minute ledger.
150000 yuan, four positions/two ETFs, T+1 and corporate entitlements carried forward.
No live command or certified backtest CLI; real-data audit remains required.
See ../ResearchDesign.md.
"""
from dataclasses import dataclass, replace

import pandas as pd

from .candidates import rank_candidates
from .audit import audit_minute_session
from .corporate import CorporateBook
from .execution import MinuteBar, PaperLedger
from .portfolio import PortfolioPlanner
from .signals import market_open


def session_clock(day):
    start = pd.Timestamp(day)
    morning = pd.date_range(start + pd.Timedelta(hours=9, minutes=30), periods=120, freq='min')
    afternoon = pd.date_range(start + pd.Timedelta(hours=13), periods=120, freq='min')
    return morning.append(afternoon)


@dataclass
class ReplaySession:
    session: object
    calendar: object
    states_at_previous_close: list
    adjusted_daily_at_previous_close: dict
    index_closes: object
    minutes: dict
    raw_daily_for_session: dict


@dataclass
class ReplayResult:
    ledger: PaperLedger
    corporate: CorporateBook
    audits: list


def run_replay(days, fees, distributions=(), slippage=0.001, use_market_gate=True):
    """Inputs are normalized, Shanghai-local, naive timestamps, amount=yuan/volume=shares.

    Every daily vintage must be adjusted only with actions known at that close and
    anchored to that session's raw close. Supplied normalized states are not proof
    of historical universe completeness. No formal performance certification here.
    """
    days = list(days)
    if not days:
        raise ValueError('Replay needs sessions')
    if [day.session for day in days] != sorted({day.session for day in days}):
        raise ValueError('Replay sessions must be unique and increasing')
    clock = session_clock(days[0].session)
    for day in days[1:]:
        clock = clock.append(session_clock(day.session))
    ledger = PaperLedger(clock, fees, slippage)
    planner = PortfolioPlanner()
    planner.risk = ledger.risk
    corporate = CorporateBook(distributions)
    audits = []
    # The requested account starts empty: pre-start record-date holdings are zero.
    for event in corporate.events:
        if event.record_session < days[0].session:
            corporate.entitlements[event.event_id] = None
    previous_processed = None
    for day in days:
        calendar = pd.DatetimeIndex(day.calendar)
        if not calendar.is_monotonic_increasing or calendar.has_duplicates:
            raise ValueError('Invalid exchange calendar')
        current = pd.Timestamp(day.session)
        location = calendar.get_indexer([current])[0]
        if location < 61:
            raise ValueError('Replay day absent from calendar or insufficient warmup')
        previous = calendar[location - 1]
        if previous_processed is not None and previous.date() != previous_processed:
            raise ValueError('Cannot omit intervening exchange sessions')
        candidates = rank_candidates(day.states_at_previous_close,
            day.adjusted_daily_at_previous_close, calendar[:location],
            previous + pd.Timedelta(hours=15))
        allow_market = market_open(day.index_closes, previous) if use_market_gate else True
        factors = corporate.reference_factors(day.session)
        candidates = [replace(item, atr=item.atr * factors.get(item.code, 1),
                               prior_close=item.prior_close * factors.get(item.code, 1))
                      for item in candidates]
        needed = set(ledger.holdings) | set(ledger.share_receivable) | {item.code for item in candidates}
        kinds = {item.code: item.kind for item in candidates}
        kinds.update({code: holding.kind for code, holding in ledger.holdings.items()})
        kinds.update(corporate.reserved_holdings())
        needed |= {event.code for event in corporate.events
                   if event.delivery_session == day.session and corporate.entitlements.get(event.event_id)}
        for event in corporate.events:
            recorded = corporate.entitlements.get(event.event_id)
            if event.delivery_session == day.session and recorded:
                kinds[event.code] = recorded.kind
        expected = session_clock(day.session)
        minute_frames = {}
        for code in needed:
            frame = day.minutes.get(code)
            if frame is None:
                raise ValueError('Missing required minute session: ' + code)
            raw_daily = day.raw_daily_for_session.get(code)
            if raw_daily is None:
                raise ValueError('Missing raw daily reconciliation row: ' + code)
            if raw_daily.get('asset_kind') != kinds[code]:
                raise ValueError('Minute phase classification differs from portfolio asset kind: ' + code)
            audit = audit_minute_session(frame, raw_daily, expected)
            audit.update({'session': str(day.session), 'code': code})
            audits.append(audit)
            if not audit['passed']:
                raise ValueError('Minute audit failed: ' + code + ': ' + ','.join(audit['issues'][:5]))
            minute_frames[code] = frame
        corporate.open_session(day.session, ledger)
        for at in expected:
            bars = {code: MinuteBar(**frame.loc[at, list(MinuteBar.__dataclass_fields__)].to_dict())
                    for code, frame in minute_frames.items()}
            equity = ledger.process(at, bars)
            for code, holding in list(ledger.holdings.items()):
                entry_location = calendar.get_indexer([pd.Timestamp(holding.entry_date)])[0]
                if entry_location < 0:
                    raise ValueError('Entry session missing from exchange calendar')
                held = location - entry_location + 1
                reason = holding.exit_state.observe(at, bars[code].close, held,
                                                     holding.sellable(at.date()))
                if reason:
                    ledger.sell(code, reason)
            occupied = {code: holding.kind for code, holding in ledger.holdings.items()}
            occupied.update(corporate.reserved_holdings())
            # All prior-minute buys have now filled or expired; only sells persist.
            intents = planner.plan_buys(at, previous.date(), candidates, minute_frames,
                                        occupied, [], equity, ledger.cash, allow_market)
            for intent in intents:
                ledger.buy(intent)
        corporate.record_close(expected[-1], ledger)
        previous_processed = day.session
    return ReplayResult(ledger, corporate, audits)
