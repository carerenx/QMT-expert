# -*- coding: utf-8 -*-
"""Continuous account arithmetic; never certifies the research goal by itself.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
Uses initial 150000 equity, all recorded minutes and inclusive calendar-day span.
Suppresses CAGR for samples shorter than 365 days. No live command.
See ../ResearchDesign.md for remaining certification and trade attribution work.
"""
from math import isfinite

import pandas as pd


def account_metrics(ledger):
    if not ledger.curve:
        raise ValueError('No processed account observations')
    frame = pd.DataFrame(ledger.curve).set_index('at')
    if not frame.index.equals(ledger.clock):
        raise ValueError('Metrics require every clock minute, including no-trade minutes')
    equity = frame.equity.astype(float)
    if not equity.map(isfinite).all() or equity.lt(0).any():
        raise ValueError('Invalid account equity')
    initial = 150_000.0
    final = float(equity.iloc[-1])
    days = (frame.index[-1].normalize() - frame.index[0].normalize()).days + 1
    peaks = equity.cummax().clip(lower=initial)
    drawdown = equity / peaks - 1
    mean_equity = float(equity.mean())
    turnover = sum(fill['gross'] for fill in ledger.fills)
    receivable = frame.get('cash_receivable', pd.Series(0., index=frame.index))
    invested = (equity - frame.cash - receivable).clip(lower=0)
    fractions = invested.div(equity.where(equity > 0)).fillna(0)
    return {
        'evidence_scope': 'account_arithmetic_only; input authenticity and completeness not certified',
        'initial_equity': initial,
        'start_session': str(frame.index[0].date()),
        'end_session': str(frame.index[-1].date()),
        'final_equity': final,
        'calendar_days': days,
        'net_profit': final - initial,
        'total_return': final / initial - 1,
        'cagr': (final / initial) ** (365.25 / days) - 1 if days >= 365 else None,
        'cagr_note': '365.25-day convention; suppressed below 365 calendar days',
        'max_drawdown': float(-drawdown.min()),
        'transaction_fees': sum(fill['fees'] for fill in ledger.fills),
        'gross_traded_amount': turnover,
        'two_way_turnover': turnover / mean_equity if mean_equity else None,
        'mean_invested_fraction_per_minute': float(fractions.mean()),
        'buy_fills': sum(fill['side'] == 'buy' for fill in ledger.fills),
        'sell_fills': sum(fill['side'] == 'sell' for fill in ledger.fills),
        'open_symbols': len(ledger.holdings),
        'cash_receivable': ledger.cash_receivable,
        'share_receivable': dict(ledger.share_receivable),
        'net_closed_position_win_rate': None,
        'trade_attribution_note': 'Full position/rights attribution remains unverified; fills are not closed trades',
        'benchmark_excess_return': None,
        'benchmark_note': 'No certified common-period benchmark supplied',
    }
