"""Verify the actual v0624 state machine; broker submits are offline fakes."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope='module')
def strategy():
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_confirmation_line_regression', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(strategy, monkeypatch):
    monkeypatch.setattr(strategy.cfg, 'now_hms', lambda: '10:00:00')
    portfolio = strategy.PortfolioRunner()
    portfolio.seed = SimpleNamespace(pending=None)
    subject = strategy.StrategyRunner(portfolio, '601869.SH')
    subject._init_state()
    subject.st.update(fstate=strategy.STATE_IDLE, do_short=True, do_long=False,
                      base_can_use=200, base_shares=200,
                      daily_signal={'sell_trigger': 224.76, 'atr_pct': 0.03,
                                    'trend': 'bear', 'buy_trigger': 200.0})
    subject.messages = []
    subject.orders = []
    subject._log = subject.messages.append

    def fake_submit(shares, price, reason):
        subject.orders.append((shares, price, reason))
        subject._execution_price = price
        return 'FILLED', shares

    subject._submit_order = fake_submit
    return subject


@pytest.mark.parametrize('base,expected', [(224.76, 225.10), (100.0, 100.15), (200.0, 200.30)])
def test_confirmation_price_is_upward_cent_aligned(strategy, base, expected):
    assert strategy.short_confirmation_price(base) == expected


def test_repeated_plan_reads_never_compound_the_buffer(runner):
    for _ in range(4):
        assert runner._rev_sell_trigger() == 225.10
    assert runner.st['daily_signal']['sell_trigger'] == 224.76


def test_original_base_and_small_extension_do_not_arm_or_log_a_hit(strategy, runner):
    for price in (224.76, 224.88, 225.09):
        assert runner._log_sell_trigger_hit(price) is False
        runner._handle_idle(price)
    assert runner.st['fstate'] == strategy.STATE_IDLE
    assert runner.st['trade_count_short'] == 0
    assert not runner.orders


def test_one_confirmation_then_reversal_sells_without_another_extension(strategy, runner):
    assert runner._log_sell_trigger_hit(225.10) is True
    runner._handle_idle(225.10)
    assert runner.st['fstate'] == strategy.STATE_SPIKING
    assert runner.st['short_arm_trigger'] == 225.10
    assert runner.st['short_arm_bars'] == 0
    assert not runner.orders
    assert runner._log_sell_trigger_hit(225.10) is False
    runner._handle_spiking(224.64)
    assert not runner.orders  # First check after confirmation is not enough.
    runner._handle_spiking(224.64)
    assert runner.orders == [(-100, 224.64, 'REV-T sell')]
    assert runner.st['fstate'] == strategy.STATE_SOLD
    assert runner.st['buyback_target'] == 223.29
    assert runner.st['sell_fill_price'] == 224.64
    hits = [line for line in runner.messages if '[SELL-TRIG HIT]' in line]
    assert len(hits) == 1
    assert 'confirmation Y225.10' in hits[0] and 'setup=1' in hits[0]
    assert 'no order yet' in hits[0]


def test_small_pullback_does_not_sell_after_two_checks(runner):
    runner._handle_idle(225.10)
    runner._handle_spiking(224.70)
    runner._handle_spiking(224.70)
    assert not runner.orders
    runner._handle_spiking(224.64)
    assert len(runner.orders) == 1


def test_plan_and_formula_use_the_same_confirmation_price(runner):
    runner._log_next_t_entry_plan(224.88, 100000, 200)
    plan = next(line for line in runner.messages if '[NEXT-PLAN] REV-T' in line)
    assert 'confirmation >= Y225.10' in plan
    assert 'extension' not in plan
    assert 'subsequent strategy checks after confirmation' in plan
    runner._log_daily_plan_formulas(runner.st['daily_signal'], 200)
    formula = next(line for line in runner.messages if '[REV-T FORMULA] confirmation' in line)
    assert 'confirmation Y225.10' in formula
    assert 'base Y224.76' in formula


def test_missing_signal_never_arms(strategy, runner):
    runner.st['daily_signal'] = {}
    assert runner._rev_sell_trigger() == 0.0
    assert runner._log_sell_trigger_hit(225.10) is False
    runner._handle_idle(225.10)
    assert runner.st['fstate'] == strategy.STATE_IDLE
    assert not runner.orders


def test_reversal_helper_has_no_hidden_second_rise(strategy):
    assert strategy.confirmed_short_reversal(225.10, 225.10, 224.64, 2)
    assert not strategy.confirmed_short_reversal(225.10, 225.09, 224.64, 2)
