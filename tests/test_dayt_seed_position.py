import copy
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path

import pytest

from Stragety.MiniQMT_Stragety.core.dayt_seed_position import (
    SeedPolicy, SeedPositionManager, seed_shares,
)


class MemoryJournal:
    def __init__(self):
        self.saved = None
        self.writes = 0

    def acquire(self):
        pass

    def close(self):
        pass

    def read(self):
        return copy.deepcopy(self.saved)

    def write(self, data):
        self.saved = copy.deepcopy(data)
        self.writes += 1


def manager(dry_run=False, journal=None):
    # Exercise custom caps independently of the new 50%/20% production default.
    return SeedPositionManager(journal or MemoryJournal(), SeedPolicy(0.20, 0.10), dry_run, lambda text: None)


def admit(controller, **overrides):
    inputs = dict(code='600000.SH', price=10.0, last_close=10.0,
                  cash=100000.0, reserved=0.0, held=set(), active_orders=False,
                  selection_ok=True, trading_day=True, now=1000.0,
                  today='20261116', hms='10:00:00', submit=lambda *args: 101)
    inputs.update(overrides)
    return controller.admit(**inputs)


def broker_row(controller, **changes):
    pending = controller.pending
    row = dict(order_id=101, stock_code=pending['code'], order_remark=pending['remark'],
               order_type=23, order_volume=pending['shares'], order_status=50,
               traded_volume=0, traded_price=0.0)
    row.update(changes)
    return row


@pytest.mark.parametrize('change', [dict(held={'600000.SH'}), dict(active_orders=True),
                                  dict(trading_day=False), dict(hms='11:45:00'),
                                  dict(hms='09:35:00'), dict(hms='14:01:00'),
                                  dict(selection_ok=False), dict(price=10.3),
                                  dict(price=-1), dict(cash=1000),
                                  dict(reserved=100000), dict(price=float('nan'))])
def test_ineligible_seed_never_submits(change):
    controller = manager()
    calls = []
    admit(controller, submit=lambda *args: calls.append(args), **change)
    assert not calls
    assert controller.pending is None


def test_signal_mode_neither_submits_nor_writes_checkpoint():
    controller = manager(dry_run=True)
    calls = []
    assert admit(controller, submit=lambda *args: calls.append(args)) == 'signal'
    assert not calls
    assert controller.journal.writes == 0


def test_intent_persisted_before_submission_and_restart_does_not_repeat():
    journal = MemoryJournal()
    controller = manager(journal=journal)

    def submit(*args):
        assert journal.saved['seed']['pending']['code'] == '600000.SH'
        raise TimeoutError('broker may already have accepted')

    with pytest.raises(TimeoutError):
        admit(controller, submit=submit)
    restarted = manager(journal=journal)
    assert restarted.pending is not None
    calls = []
    assert admit(restarted, submit=lambda *args: calls.append(args)) == 'unresolved or active account order'
    assert not calls


def test_partial_fill_and_t_plus_one_are_persistent():
    controller = manager()
    admit(controller)
    row = broker_row(controller, order_status=54, traded_volume=100, traded_price=9.99)
    controller.reconcile([row], 1010, '20261116', lambda order: None)
    assert controller.pending is None
    assert controller.filled_today('600000.SH', '20261116')
    assert not controller.filled_today('600000.SH', '20261117')
    assert controller.state['spent'] > 999
    assert admit(controller) == 'already held or attempted'


def test_timeout_cancels_once_and_waits_for_terminal_state():
    controller = manager()
    admit(controller)
    row = broker_row(controller)
    cancels = []
    controller.reconcile([row], 1031, '20261116', cancels.append)
    controller.reconcile([row], 1040, '20261116', cancels.append)
    assert cancels == [101]
    assert controller.pending is not None


def test_unknown_or_mismatched_order_never_creates_fill():
    controller = manager()
    admit(controller)
    controller.reconcile([], 2000, '20261116', lambda order: None)
    assert controller.pending is not None
    row = broker_row(controller, order_volume=1)
    with pytest.raises(ValueError):
        controller.reconcile([row], 2000, '20261116', lambda order: None)
    assert controller.state['spent'] == 0


def test_campaign_and_symbol_caps_include_costs_and_cash_reserves():
    controller = manager()
    admit(controller)
    quantity = controller.pending['shares']
    assert quantity == 900
    row = broker_row(controller, order_status=56, traded_volume=quantity, traded_price=10.0)
    controller.reconcile([row], 1010, '20261116', lambda order: None)
    admit(controller, code='600001.SH', cash=90000)
    assert controller.pending['shares'] == 900
    row = broker_row(controller, order_status=56, traded_volume=900, traded_price=10.0)
    controller.reconcile([row], 1020, '20261116', lambda order: None)
    # Remaining total budget only funds one lot, despite abundant current cash.
    admit(controller, code='600002.SH', cash=1000000)
    assert controller.pending['shares'] == 100
    assert seed_shares(1000.0, 10.0) == 0


def test_new_strategy_is_standalone_and_keeps_dayt_blocked_for_today_seed():
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_v0624_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SEED_STOCK_POOL == ()
    assert module.SEED_SELECTION_ENABLED is True
    assert module.SEED_TOTAL_CASH_FRACTION == 0.50
    assert module.SEED_SYMBOL_CASH_FRACTION == 0.20
    portfolio = module.PortfolioRunner()
    portfolio.seed = manager()
    admit(portfolio.seed)
    runner = module.StrategyRunner(portfolio, '600000.SH')
    assert runner._new_leg_block_reason() == 'account order outcome uncertain'


def test_seed_fill_is_not_discovered_as_dayt_holding_until_next_day(monkeypatch):
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_v0624_handoff_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.now()
    portfolio = module.PortfolioRunner()
    portfolio.seed = manager()
    portfolio.seed.state['attempts']['600000.SH'] = {
        'fill_date': now.strftime('%Y%m%d'), 'filled': 100,
    }
    position = SimpleNamespace(stock_code='600000.SH', volume=100, stock_name='test')
    portfolio.conn.trader = SimpleNamespace(query_stock_positions=lambda account: [position])
    portfolio.refresh_holdings(1000)
    assert not portfolio.runners
    tomorrow = now + timedelta(days=1)
    monkeypatch.setattr(module, 'datetime', SimpleNamespace(now=lambda: tomorrow))
    portfolio.refresh_holdings(1100)
    assert set(portfolio.runners) == {'600000.SH'}
    assert portfolio.runners['600000.SH'].st == {}


def test_user_selected_50_total_20_symbol_budget_and_dip_buy_are_supported():
    policy = SeedPolicy()
    assert policy.total_fraction == 0.50
    assert policy.symbol_fraction == 0.20
    controller = SeedPositionManager(MemoryJournal(), policy, False, lambda text: None)
    # Existing v11 rules select dips: a price below yesterday's close is allowed.
    assert admit(controller, price=9.9) == 'submitted'
    quantity = controller.pending['shares']
    assert quantity == 2000
    row = broker_row(controller, order_status=56, traded_volume=quantity, traded_price=9.9)
    controller.reconcile([row], 1010, '20261116', lambda order: None)
    admit(controller, code='600001.SH', price=9.9, cash=80000)
    assert controller.pending['shares'] == 2000
    row = broker_row(controller, order_status=56, traded_volume=2000, traded_price=9.9)
    controller.reconcile([row], 1020, '20261116', lambda order: None)
    admit(controller, code='600002.SH', price=9.9, cash=60000)
    assert controller.pending['shares'] == 1000


def test_plan_preview_does_not_consume_budget_or_write_and_matches_submission():
    controller = manager()
    before = copy.deepcopy(controller.state)
    plan = controller.plan('600000.SH', 10.0, 10.0, 100000.0, 0.0,
                           set(), False, True, True, '10:00:00')
    assert plan['shares'] == 900
    assert plan['budget'] == 10000
    assert plan['total_cap'] == 20000
    assert plan['estimated_cost'] > 9000
    assert controller.state == before
    assert controller.journal.writes == 0
    orders = []
    admit(controller, submit=lambda *args: orders.append(args) or 101)
    assert orders[0][1] == plan['shares']
    assert orders[0][2] == plan['price']


def test_plan_logging_deduplicates_and_retains_wait_reason():
    logs = []
    controller = SeedPositionManager(MemoryJournal(), SeedPolicy(), True, logs.append)
    plan = controller.plan('600000.SH', 10.0, 10.0, 100000.0, 0.0,
                           set(), True, True, True, '10:00:00')
    controller.log_plan(plan)
    controller.log_plan(plan)
    assert len(logs) == 1
    assert 'unresolved or active account order' in logs[0]
    assert 'mode=SIGNAL' in logs[0]
    assert 'remaining=' in logs[0]
    assert controller.pending is None


def test_dayt_plan_heartbeat_reaches_console_without_broker_queries():
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_v0624_plan_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    portfolio = module.PortfolioRunner()
    portfolio.seed = manager()
    runner = module.StrategyRunner(portfolio, '600000.SH')
    messages = []
    runner._log = messages.append
    runner._file_log = lambda message: pytest.fail('plan must not be file-only')
    runner._heartbeat(10.0)
    assert any('[NEXT-PLAN]' in message for message in messages)
    assert any('[HB]' in message for message in messages)
