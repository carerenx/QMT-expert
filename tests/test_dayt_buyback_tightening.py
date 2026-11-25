"""Exercise real v0624 state transitions with broker methods replaced offline."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope='module')
def strategy():
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_buyback_regression', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(strategy):
    portfolio = strategy.PortfolioRunner()
    subject = strategy.StrategyRunner(portfolio, '601869.SH')
    subject._init_state()
    subject.st.update(fstate=strategy.STATE_SOLD, sell_fill_price=223.01,
                      buyback_target=215.77,
                      buyback_target_pct=(1 - 215.77 / 223.01) * 100,
                      sell_elapsed_bars=31,
                      buyback_trigger_mult=0.30,
                      short_legs=[(223.01, 200)],
                      daily_signal={'atr_pct': (1 - 215.77 / 223.01) / 0.30})
    subject.messages = []
    subject._log = subject.messages.append
    return subject


def test_tightened_line_survives_high_to_low_price_sequence(strategy, runner):
    runner._handle_sold(222.00)
    assert runner.st['fstate'] == strategy.STATE_SOLD
    assert runner.st['buyback_target'] == 218.67
    runner._handle_sold(219.00)
    assert runner.st['fstate'] == strategy.STATE_SOLD
    runner._handle_sold(218.67)
    assert runner.st['fstate'] == strategy.STATE_DIPPING
    assert runner.st['dip_price'] == 218.67
    assert any('[BUYBACK-TIGHTENED]' in line for line in runner.messages)


def test_counter_boundary_and_high_price_condition_are_both_required(runner):
    runner.st['sell_elapsed_bars'] = 30
    runner._handle_sold(222.00)
    assert runner.st['buyback_target'] == 215.77
    runner.st['sell_elapsed_bars'] = 31
    runner._handle_sold(221.00)
    assert runner.st['buyback_target'] == 215.77
    runner._handle_sold(222.00)
    assert runner.st['buyback_target'] == 218.67


def test_original_low_touch_still_works_before_activation(strategy, runner):
    runner.st['sell_elapsed_bars'] = 5
    runner._handle_sold(215.77)
    assert runner.st['fstate'] == strategy.STATE_DIPPING
    assert runner.st['buyback_target'] == 215.77


def test_tightening_occurs_once_and_uses_sell_cycle_locked_distance(runner):
    runner.st['daily_signal']['atr_pct'] *= 2
    runner._handle_sold(222.00)
    assert runner.st['buyback_target'] == 218.67
    runner.st['daily_signal']['atr_pct'] *= 0.01
    runner._handle_sold(222.50)
    assert runner.st['buyback_target'] == 218.67
    assert len([line for line in runner.messages if '[BUYBACK-TIGHTENED]' in line]) == 1


def test_touch_waits_for_rebound_before_buying(strategy, runner):
    calls = []
    runner._do_buyback = lambda price, reason: calls.append((price, reason)) or 0
    runner._handle_sold(222.00)
    runner._handle_sold(218.60)
    assert runner.st['fstate'] == strategy.STATE_DIPPING
    assert not calls
    runner._handle_dipping(218.40)
    runner._handle_dipping(218.50)
    assert not calls
    runner._handle_dipping(218.63)
    assert calls == [(218.63, 'NORMAL')]


def test_partial_fill_retains_tightened_target_and_logs_active_plan(strategy, runner):
    runner._handle_sold(222.00)
    runner._execution_price = 218.60
    runner._submit_buyback_order = lambda *args: ('PARTIAL', 100)
    runner.execution_book = SimpleNamespace(legs={'SHORT': [(223.01, 100)]})
    runner.messages.clear()
    assert runner._do_buyback(218.60, 'NORMAL') == 100
    assert runner.st['fstate'] == strategy.STATE_SOLD
    assert runner.st['buyback_target'] == 218.67
    assert runner.st['buyback_tightened'] is True
    assert any('218.67' in line and 'active tightened' in line for line in runner.messages)
    assert not any('after 30' in line for line in runner.messages)


def test_cycle_reset_clears_activation_flag(runner):
    runner._handle_sold(222.00)
    assert runner.st['buyback_tightened'] is True
    runner._init_state()
    assert runner.st['buyback_tightened'] is False
    assert runner.st['buyback_target'] == 0


@pytest.mark.parametrize('trend,expected', [
    ('bear', 0.20), ('strong_bull', 0.15), ('weak_bull', 0.15),
    ('sideways', 0.15), ('unknown', 0.15), ('', 0.15),
])
def test_regime_parameters_keep_bear_target_closer_than_old_default(strategy, trend, expected):
    assert strategy.select_buyback_trigger_mult({'trend': trend}) == expected


def test_coefficient_change_does_not_reprice_existing_locked_cycle(runner):
    # The fixture represents a previously filled .30 cycle, not a new order.
    assert runner._buyback_trigger_mult() == 0.30
    assert runner.st['buyback_target'] == 215.77
    assert runner.st['buyback_target_pct'] == pytest.approx((1 - 215.77 / 223.01) * 100)


def test_new_bear_sell_uses_point_two_and_tightens_from_that_locked_distance(strategy, runner):
    runner.portfolio.seed = SimpleNamespace(pending=None)
    runner.st.update(fstate=strategy.STATE_SPIKING,
                     peak_price=223.50, short_arm_trigger=222.82, short_arm_bars=2,
                     short_legs=[], buyback_tightened=True,
                     daily_signal={'trend': 'bear', 'atr_pct': 0.1082})
    runner._execution_price = 223.01
    runner._submit_order = lambda *args: ('FILLED', -100)
    runner._handle_spiking(223.01)
    assert runner.st['fstate'] == strategy.STATE_SOLD
    assert runner.st['buyback_trigger_mult'] == 0.20
    assert runner.st['buyback_target_pct'] == pytest.approx(2.164)
    assert runner.st['buyback_target'] == 218.18
    assert runner.st['buyback_tightened'] is False
    runner.st['sell_elapsed_bars'] = 31
    runner._handle_sold(222.00)
    assert runner.st['buyback_target'] == 220.11
