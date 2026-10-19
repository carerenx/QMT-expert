# -*- coding: utf-8 -*-
"""Reporting must not mutate trading inputs, ledger, or create broker orders."""
import copy
import unicodedata
from types import SimpleNamespace

import pandas as pd

from Stragety.RedisQMT.A144 import A144_v12_DualRegimePortfolio as policy
from Stragety.RedisQMT.A144.core.dual_regime_execution import Journal, new_state
from Stragety.RedisQMT.A144.core.dual_regime_report import daily_report, execution_report, minute_report, pad


def example_market():
    dates = ['20261009', '20261012', '20261013', '20261014', '20261015', '20261016']
    rows = [dict(date=date, code=code, member=True, breadth=.2, market_strict=False,
        adj_close=12., close=12., ma50=10., ma20=11., ma50_lag5=9., mom60=.2,
        mom60_rank=.99, age=200, amount20=1e8, atr_pct=.04, ret=.02, efficiency=.5,
        score=.95, weight=.2, entry=False)
        for date in dates for code in ('600001.SH', '600002.SH')]
    primary = pd.DataFrame(rows)
    frame = pd.DataFrame(dict(close=[10 + index * .01 for index in range(220)], amount=1e8))
    daily = {code: frame.copy() for code in policy.CODES}
    daily['000905.SH'] = frame.copy()
    return SimpleNamespace(policy=policy, primary=primary, asof=dates[-1], weak=True,
        candidates=primary.iloc[:0], daily=daily, secondary=policy.secondary_signals(daily, True),
        name=lambda code: '甲股份' if code == '600001.SH' else '乙乙股份')


def test_daily_report_explains_zero_result_and_lists_all_etfs_without_mutation():
    market = example_market()
    state = new_state('fake', 90000.)
    original_state = copy.deepcopy(state)
    original_frame = market.primary.copy(deep=True)
    logs = []
    daily_report(market, state, 80000., {}, logs.append)
    assert any('[PrimaryScreen] index_MA60' in line and 'remaining=  0' in line for line in logs)
    assert sum(line.startswith('[PrimaryPreMarket] 600') for line in logs) == 2
    assert sum(line.startswith('[SecondaryScreen] ') and 'Group=' in line for line in logs) == 7
    assert any('159920.SZ' in line and 'Group=reversion' in line for line in logs)
    assert any('final_selected=0' in line for line in logs)
    assert any('not an order' in line for line in logs)
    assert state == original_state
    pd.testing.assert_frame_equal(market.primary, original_frame)
    prefixes = [line.split('|')[0] for line in logs if line.startswith('[PrimaryPreMarket] 600')]
    widths = [sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1 for char in text) for text in prefixes]
    assert widths[0] == widths[1]


def test_minute_plan_is_not_reported_as_submission_or_fill(tmp_path):
    market = example_market()
    signal = '20261019093100'
    state = new_state('fake', 90000.)
    code = policy.COMMODITY_CODES[0]
    state['parents'][code] = dict(signal=signal, sleeve='secondary', budget=10000., spent=0.)
    original = copy.deepcopy(state)
    bars = {code: pd.DataFrame([dict(close=12., high=12., low=11., volume=10000.)]) for code in policy.CODES}
    intent = dict(code=code, sleeve='secondary', side='BUY', quantity=100, limit=12.12, key='fake-key')
    logs = []
    path = tmp_path / 'state.json'
    minute_report(market, Journal(path, state), {}, signal, bars, [intent], logs.append)
    assert any('[OrderPlan]' in line and 'not_submitted_yet' in line for line in logs)
    assert any('[ParentPlan]' in line and 'budget=10000.00' in line for line in logs)
    assert not any(line.startswith(('[Submission]', '[Fill]')) for line in logs)
    assert state == original and not path.exists()


def test_execution_report_emits_only_new_fill_status_and_closed_position():
    market = example_market()
    state = new_state('fake', 90000.)
    code = policy.COMMODITY_CODES[0]
    state['fills'].append(dict(code=code, side='SELL', quantity=100, notional=1200., estimated_fee=5., key='fake-key'))
    state['orders']['fake-key'] = dict(code=code, status='56', terminal=True, applied_quantity=100, quantity=100)
    state['trades'].append(dict(code=code, sleeve='secondary', pnl=100.))
    logs = []
    original = copy.deepcopy(state)
    execution_report(market, state, (0, 0, {}), logs.append)
    assert sum(line.startswith('[Fill]') for line in logs) == 1
    assert any('cumulative_filled=100/100' in line for line in logs)
    assert any('pnl_after_estimated_fees=+100.00' in line for line in logs)
    logs.clear()
    execution_report(market, state, (1, 1, {'fake-key': ('56', True)}), logs.append)
    assert not logs and state == original


def test_cjk_name_padding_uses_display_width():
    assert pad('甲', 6) == '甲    '
    assert pad('ABC', 6) == 'ABC   '


def test_daily_exit_plans_do_not_latch_exits_or_submit_stop_orders():
    market = example_market()
    state = new_state('fake', 90000.)
    base = dict(quantity=100, bought=100, buy_notional=1000., buy_fee=5.,
        sell_notional=0., sell_fee=0., entry_date='20261012', exit_reason='')
    state['positions']['600001.SH'] = dict(base, sleeve='primary')
    state['positions']['159981.SZ'] = dict(base, sleeve='secondary')
    market.daily['600001.SH'] = market.daily['159981.SZ'].copy()
    original = copy.deepcopy(state)
    logs = []
    daily_report(market, state, 80000., {}, logs.append)
    assert any('[ExitPlan] 600001.SH' in line and 'daily_exit=index-stress' in line for line in logs)
    assert any('[ExitPlan] 159981.SZ' in line and 'StopReference=9.600' in line
               and 'not_a_resting_broker_stop' in line for line in logs)
    assert state == original
