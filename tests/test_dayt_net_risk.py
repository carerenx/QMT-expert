"""Offline economic, timing and real v0628 execution/admission regression tests."""
import ast
from datetime import datetime, timedelta
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from Stragety.MiniQMT_Stragety.core.dayt_net_risk import (
    FeeEstimate, NetAttribution, net_space_check, risk_capped_shares, trading_seconds_between,
)
from tools.bigqmt_repair_seed_history import verify_history


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def strategy():
    path = ROOT / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0628_NetCostRiskBudgetDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_net_risk_regression', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(strategy, monkeypatch):
    monkeypatch.setattr(strategy.cfg, 'now_hms', lambda: '10:00:00')
    portfolio = strategy.PortfolioRunner(dry_run=False)
    portfolio.seed = SimpleNamespace(pending=None)
    subject = strategy.StrategyRunner(portfolio, '601869.SH')
    subject._init_state()
    subject.messages = []
    subject._log = subject.messages.append
    subject.st.update(base_shares=200, base_can_use=200,
        daily_signal={'atr_pct': 0.1, 'trend': 'bear', 'sell_trigger': 200})
    subject._account_equity = lambda: 100000.0
    subject.ctx = SimpleNamespace(get_full_tick=lambda codes: {
        subject.stock_qmt: {'time': strategy._time.time() * 1000, 'lastPrice': 100}})
    return subject


def test_unclosed_sell_is_marked_and_actual_fill_slippage_not_charged_twice():
    book = NetAttribution()
    book.record('1', -100, 200.85)
    metrics = book.mark(210.08)
    assert metrics['realized_gross'] == 0
    assert metrics['inventory_component'] == pytest.approx(-923)
    assert metrics['incremental_net_estimate'] == pytest.approx(-923 - book.fees.fee(200.85, 100, 'SELL'))
    assert book.mark(210.08) == metrics
    with pytest.raises(ValueError, match='duplicate'):
        book.record('1', -100, 200.85)


def test_partial_close_and_complete_cycle_reconcile_cash_inventory_and_fees():
    book = NetAttribution(fund=True)
    book.record('a', -100, 10)
    book.record('b', 50, 9, realized_gross=50)
    metrics = book.mark(8)
    assert metrics['incremental_gross_vs_hold'] == 150
    assert metrics['inventory_component'] == 100
    book.record('c', 50, 8, realized_gross=100)
    assert book.mark(99)['incremental_gross_vs_hold'] == 150
    assert book.mark(99)['inventory_component'] == 0
    assert book.mark(99)['incremental_net_estimate'] == 135


def test_stock_and_fund_fees_are_separate_estimates():
    fees = FeeEstimate()
    assert fees.fee(1.7, 100, 'SELL', fund=True) == 5
    assert fees.fee(10, 1000, 'SELL') == pytest.approx(10.1)
    assert fees.fee(10, 1000, 'BUY') == pytest.approx(5.1)


@pytest.mark.parametrize('start,end,seconds', [
    ('11:29:45','13:00:15',30), ('08:00:00','09:30:30',30),
    ('14:59:45','15:15:00',15), ('11:40:00','12:50:00',0),
    ('10:00:00','10:00:31',31), ('10:00:30','10:00:00',0),
])
def test_time_is_session_seconds_not_poll_count(start, end, seconds):
    day = '2026-11-24 '
    assert trading_seconds_between(datetime.fromisoformat(day+start), datetime.fromisoformat(day+end)) == seconds


def test_time_does_not_introduce_cross_day_cycle_management():
    assert trading_seconds_between(datetime(2026,11,23,14), datetime(2026,11,24,10)) == 0


def test_risk_budget_never_forces_an_unaffordable_minimum_lot():
    assert risk_capped_shares(100,100,200,0.1,70000,0.005,1,FeeEstimate(),'SELL') == 0


def test_risk_budget_is_maximal_whole_lot_inside_cost_adjusted_limit():
    fees = FeeEstimate()
    shares = risk_capped_shares(10000,100,10,0.02,100000,0.005,1,fees,'SELL')
    loss = lambda quantity: quantity*0.2+fees.round_trip(10,10.2,quantity,'SELL')
    assert shares > 0 and shares % 100 == 0
    assert loss(shares) <= 500 < loss(shares+100)


@pytest.mark.parametrize('equity,atr', [(0,0.1),(10000,0),(float('nan'),0.1)])
def test_invalid_risk_inputs_block_new_legs(equity, atr):
    assert risk_capped_shares(10000,100,10,atr,equity,0.005,1,FeeEstimate(),'BUY') == 0


def test_small_fund_trade_cannot_cover_minimum_costs():
    result = net_space_check(1.75,1.74,100,'SELL',FeeEstimate(),fund=True)
    assert result['allowed'] is False
    assert result['stress_cost'] > result['gross_space']


def test_larger_space_can_clear_costs_without_claiming_positive_expectancy():
    assert net_space_check(100,97,1000,'SELL',FeeEstimate())['allowed'] is True


def test_repair_requires_real_dates_and_ohlc_not_download_progress():
    frame = pd.DataFrame({'high':[2,2], 'low':[1,1], 'close':[1.5,1.5]}, index=['20261120','20261123'])
    assert verify_history(frame,['20261120','20261123'])
    assert not verify_history(frame,['20261119','20261123'])
    frame.loc['20261123','close'] = 3
    assert not verify_history(frame,['20261120','20261123'])


def test_execution_uses_confirmed_fill_and_only_records_it_once(strategy, runner):
    order = SimpleNamespace(order_id=123, order_sysid='', stock_code='601869.SH',
                            traded_volume=100, traded_price=200.85, order_status=56)
    runner.conn = SimpleNamespace(trader=SimpleNamespace(query_stock_order=lambda *args: order), _account_obj=object())
    runner._submitted_order_id = 123
    runner._refresh_position = lambda: None
    status, delta = runner._wait_for_fill(-100,'REV-T sell',200)
    assert (status,delta) == ('FILLED',-100)
    assert runner.net_attribution.mark(210.08)['incremental_gross_vs_hold'] == pytest.approx(-923)
    assert runner._cycle_opened_at is not None


def test_new_order_cost_check_cannot_be_bypassed_by_planned_quantity(strategy, runner, monkeypatch):
    runner.ctx = SimpleNamespace(get_full_tick=lambda codes: {runner.stock_qmt: {
        'time': strategy._time.time()*1000, 'lastPrice': 1.75}})
    runner._new_t_shares = lambda *args: 10000
    runner._clamp_buy_shares = lambda *args: 100
    runner.st['daily_signal']['atr_pct'] = 0.001
    monkeypatch.setattr(strategy, 'order_shares', lambda *args: pytest.fail('must not submit'))
    assert runner._submit_order(10000,1.75,'FWD-T buy') == ('SKIP',0)


def test_closing_order_is_not_blocked_by_entry_cost_hurdle(strategy, runner, monkeypatch):
    runner._clamp_buy_shares = lambda *args: 100
    runner._net_admission = lambda *args: pytest.fail('closing is not a new leg')
    monkeypatch.setattr(strategy,'order_shares',lambda *args: 123)
    runner._wait_for_fill = lambda *args: ('FILLED',100)
    assert runner._submit_order(100,201,'REV-T buyback(INTRADAY-RISK)',style='FIX') == ('FILLED',100)


def test_new_order_can_execute_when_whole_lot_risk_and_net_space_both_pass(strategy, runner, monkeypatch):
    runner._paired_long_capacity = lambda price: {'pairing_shares':200}
    runner._account_equity = lambda: 1000000.0
    runner._clamp_sell_shares = lambda shares: shares
    calls = []
    monkeypatch.setattr(strategy,'order_shares',lambda *args: calls.append(args) or 123)
    runner._wait_for_fill = lambda *args: ('FILLED',-100)
    assert runner._submit_order(-100,100,'REV-T sell') == ('FILLED',-100)
    assert calls[0][:4] == ('601869.SH',-100,'FIX',100)


def test_submission_cutoff_is_rechecked_after_a_signal_was_armed(strategy, runner, monkeypatch):
    monkeypatch.setattr(strategy.cfg,'now_hms',lambda: '14:20:00')
    runner._new_t_shares = lambda *args: pytest.fail('should stop before sizing/submission')
    assert runner._submit_order(-100,200,'REV-T sell') == ('SKIP',0)


def test_legacy_cross_day_and_initialization_methods_are_unchanged():
    paths = ['DayT_v0624_SeedPositionRegimeDayT.py','DayT_v0628_NetCostRiskBudgetDayT.py']
    trees = [ast.parse((ROOT/'Stragety/MiniQMT_Stragety/DayT'/name).read_text(encoding='utf-8')) for name in paths]
    for method in ('_init_state','_reset_previous_day','save_checkpoint','_run_inner_state_machine','_handle_dipping','_handle_bt_bought','_handle_bt_spiking'):
        bodies = [next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name == method) for tree in trees]
        assert ast.dump(bodies[0],include_attributes=False) == ast.dump(bodies[1],include_attributes=False)
    assert not any(isinstance(node,ast.ImportFrom) and node.module and 'v0624' in node.module for node in ast.walk(trees[1]))


def test_no_new_stop_loss_timeout_or_cross_day_management(strategy):
    assert not hasattr(strategy.StrategyRunner, '_intraday_risk_exit')
    assert not hasattr(strategy, 'RISK_PROFILES')
    assert strategy.cfg.EMERGENCY_BUYBACK is False
    assert strategy.cfg.ENABLE_FORCE_CLOSE is False


def test_tightening_uses_seconds_instead_of_number_of_checks(runner):
    runner.st.update(fstate='SOLD',sell_fill_price=223.01,buyback_target=215.77,
        buyback_target_pct=(1-215.77/223.01)*100,sell_elapsed_bars=999,
        short_legs=[(223.01,100)],buyback_trigger_mult=0.30)
    runner._elapsed_cycle_seconds = lambda: 5
    runner._handle_sold(222)
    assert runner.st['buyback_target'] == 215.77
    runner._elapsed_cycle_seconds = lambda: 31
    runner.st['sell_elapsed_bars'] = 0
    runner._handle_sold(222)
    assert runner.st['buyback_target'] == 218.67


@pytest.mark.parametrize('age', [11, 14400, -3, -1800])
def test_same_day_stale_or_future_quote_is_rejected(runner, age):
    now = datetime(2026, 11, 25, 14).timestamp()
    assert not runner._tick_is_current({'time': (now-age)*1000}, '20261125', now)


def test_fresh_quote_and_distinct_event_guard(runner):
    now = datetime(2026, 11, 25, 10).timestamp()
    tick = {'time': now*1000, 'lastPrice': 100}
    assert runner._tick_is_current(tick, '20261125', now)
    assert runner._consume_tick(tick)
    assert not runner._consume_tick(tick)
    assert not runner._consume_tick(dict(tick, lastPrice=101))
    assert runner._consume_tick(dict(tick, time=(now+1)*1000))
    assert not runner._tick_is_current(dict(tick, timetag='20261125 09:00:00'), '20261125', now)


def test_submit_rechecks_quote_before_sizing(strategy, runner):
    runner.ctx = SimpleNamespace(get_full_tick=lambda codes: {runner.stock_qmt: {
        'time': (strategy._time.time()-60)*1000, 'lastPrice': 100}})
    runner._new_t_shares = lambda *args: pytest.fail('stale quote reached sizing')
    assert runner._submit_order(-100, 100, 'REV-T sell') == ('SKIP', 0)


def test_quote_can_expire_during_sizing_and_must_be_rechecked(strategy, runner, monkeypatch):
    now = strategy._time.time()
    ticks = iter([{'time':now*1000, 'lastPrice':100},
                  {'time':(now-60)*1000, 'lastPrice':100}])
    runner.ctx = SimpleNamespace(get_full_tick=lambda codes: {runner.stock_qmt: next(ticks)})
    runner._new_t_shares = lambda *args: 100
    runner._clamp_sell_shares = lambda shares: shares
    runner._net_admission = lambda *args: True
    monkeypatch.setattr(strategy, 'order_shares', lambda *args: pytest.fail('stale final quote submitted'))
    assert runner._submit_order(-100, 100, 'REV-T sell') == ('SKIP', 0)


@pytest.mark.parametrize('tick', [{}, {'time':float('nan')}, {'time':float('inf')}, {'time':'invalid'}])
def test_invalid_timestamp_fails_closed(runner, tick):
    assert not runner._tick_is_current(tick, '20261125', datetime(2026,11,25,10).timestamp())


def test_cash_clamp_includes_minimum_fee(runner):
    runner._available_cash = lambda: 175.2
    assert runner._clamp_buy_shares(100, 1.75) < 100
    runner._available_cash = lambda: 181
    assert runner._clamp_buy_shares(100, 1.75) == 100


@pytest.mark.parametrize('code', ['601869.SH', '510300.SH'])
def test_odd_buy_residual_is_explicit_and_blocks_new_entries(strategy, code, monkeypatch):
    portfolio = strategy.PortfolioRunner(dry_run=False)
    portfolio.seed = SimpleNamespace(pending=None)
    subject = strategy.StrategyRunner(portfolio, code)
    subject._init_state()
    messages = []
    subject._log = messages.append
    monkeypatch.setattr(strategy, 'order_shares', lambda *args: pytest.fail('must not overbuy'))
    assert subject._submit_order(50, 10, 'REV-T buyback') == ('SKIP', 0)
    assert portfolio.manual_review[code] == 50
    assert subject._new_leg_block_reason()
    assert any('RESIDUAL' in message for message in messages)


def _broker_fill(runner, volume=100, status=56, price=200):
    order = SimpleNamespace(order_id=123, order_sysid='', stock_code=runner.stock_qmt,
        traded_volume=volume, traded_price=price, order_status=status)
    runner.conn = SimpleNamespace(trader=SimpleNamespace(query_stock_order=lambda *args: order),
        _account_obj=object(), cancel_order=lambda *args: pytest.fail('confirmed order cancelled'))
    runner._submitted_order_id = 123
    return order


def test_confirmed_fill_survives_position_failure_and_is_idempotent(strategy, runner, monkeypatch):
    _broker_fill(runner)
    malformed = SimpleNamespace(stock_code=runner.stock_qmt, volume=100,
                                open_price=200, available='malformed')
    monkeypatch.setitem(strategy.get_trade_detail_data.__globals__, '_global_conn',
                        SimpleNamespace(query_positions=lambda: [malformed]))
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('FILLED', -100)
    first = runner.net_attribution.mark(200)
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('FILLED', -100)
    assert runner.net_attribution.mark(200) == first
    assert runner.execution_book.legs['SHORT'] == [(200, 100)]
    assert runner._new_leg_block_reason()


def test_logging_failure_after_fill_never_changes_confirmed_outcome(runner):
    _broker_fill(runner)
    runner._refresh_position = lambda: None
    runner._log = lambda message: (_ for _ in ()).throw(OSError('console unavailable'))
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('FILLED', -100)
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('FILLED', -100)
    assert len(runner.execution_book.orders) == 1
    assert not runner.portfolio.order_uncertain


def test_partial_150_close_submits_100_then_reports_50(strategy, runner, monkeypatch):
    runner.execution_book.record('opening', 'REV-T sell', -150, 200)
    runner.net_attribution.record('opening', -150, 200)
    runner._available_cash = lambda: 100000
    runner._refresh_position = lambda: None
    _broker_fill(runner, 100, 56, 100)
    calls = []
    monkeypatch.setattr(strategy, 'order_shares', lambda *args: calls.append(args) or 123)
    assert runner._submit_order(150, 100, 'REV-T buyback', 'FIX') == ('FILLED', 100)
    assert calls[0][1] == 100
    assert runner.execution_book.legs['SHORT'] == [(200, 50)]
    assert runner.portfolio.manual_review[runner.stock_qmt] == 50
    assert runner._submit_order(50, 100, 'REV-T buyback', 'FIX') == ('SKIP', 0)
    assert len(calls) == 1


def test_identity_mismatch_is_not_a_zero_fill_timeout(runner):
    order = _broker_fill(runner)
    order.stock_code = '600000.SH'
    with pytest.raises(RuntimeError, match='identity'):
        runner._wait_for_fill(-100, 'REV-T sell', 200)
    assert runner.portfolio.order_uncertain
    assert not runner.execution_book.orders


@pytest.mark.parametrize('volume,status,price', [(0,56,0),(50,56,200),(101,56,200),(100,57,200),(100,56,0)])
def test_contradictory_fill_freezes_instead_of_reopening(runner, volume, status, price):
    _broker_fill(runner, volume, status, price)
    with pytest.raises(RuntimeError, match='inconsistent'):
        runner._wait_for_fill(-100, 'REV-T sell', 200)
    assert runner.portfolio.order_uncertain
    assert not runner.execution_book.orders


def test_legitimate_cancel_and_partial_cancel_are_distinct(runner):
    order = _broker_fill(runner, 0, 54, 0)
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('TIMEOUT', 0)
    order.traded_volume = 50
    order.traded_price = 200
    runner._refresh_position = lambda: None
    assert runner._wait_for_fill(-100, 'REV-T sell', 200) == ('PARTIAL', -50)


def test_accounting_failure_cannot_commit_only_one_book(runner, monkeypatch):
    _broker_fill(runner)
    monkeypatch.setattr(type(runner.net_attribution), 'record', lambda *args: (_ for _ in ()).throw(ValueError('bad accounting')))
    with pytest.raises(RuntimeError, match='accounting'):
        runner._wait_for_fill(-100, 'REV-T sell', 200)
    assert not runner.execution_book.orders
    assert not runner.execution_book.legs
    assert runner.portfolio.order_uncertain


@pytest.mark.parametrize('valid,accepted',[(1,False),(94,False),(95,True),(100,True)])
def test_seed_worker_requires_verified_coverage_before_exposing_candidates(valid, accepted, monkeypatch):
    import threading
    from Stragety.MiniQMT_Stragety.core import ma5_ma20_seed_selection as selection
    entered = threading.Event()
    messages = []

    def screen(*args):
        args[-1]({'universe':100,'counts':{'valid':valid}})
        entered.set()
        return ['601869.SH']

    monkeypatch.setattr(selection,'screen_main_board',screen)
    worker = selection.SeedSelectionWorker(lambda: SimpleNamespace(client=None),messages.append,minimum_coverage=0.95)
    try:
        worker.request('20261124')
        assert entered.wait(2)
        worker.close()
        snapshot = worker.snapshot('20261124')
        if accepted:
            assert snapshot['codes'] == ('601869.SH',)
            assert snapshot['coverage']['counts']['valid'] == valid
        else:
            assert snapshot is None
            assert any('coverage' in message and 'blocked' in message for message in messages)
    finally:
        worker.close()
