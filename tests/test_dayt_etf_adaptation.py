"""Offline ETF price/lot/availability tests, including real v0624 transitions."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from Stragety.MiniQMT_Stragety.core.instrument_rules import round_price, rules_for_symbol
from Stragety.MiniQMT_Stragety.core.atr_reentry import calculate_atr_reentry
from Stragety.MiniQMT_Stragety.core.signals import compute_signal


@pytest.fixture(scope='module')
def strategy():
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0624_SeedPositionRegimeDayT.py'
    spec = importlib.util.spec_from_file_location('dayt_etf_regression', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(strategy, monkeypatch):
    monkeypatch.setattr(strategy.cfg, 'now_hms', lambda: '10:00:00')
    portfolio = strategy.PortfolioRunner(dry_run=False)
    portfolio.seed = SimpleNamespace(pending=None)
    subject = strategy.StrategyRunner(portfolio, '159981.SZ')
    subject._init_state()
    subject.st.update(base_shares=16200, base_can_use=16200, daily_signal={
        'sell_trigger': 1.762, 'atr_pct': 0.0289, 'trend': 'bear', 'buy_trigger': 1.693,
    })
    subject.messages = []
    subject._log = subject.messages.append
    return subject


def daily_arrays():
    opens = 1.72 + np.sin(np.arange(80) * 0.7) * 0.025
    highs = opens + 0.021
    lows = opens - 0.018
    closes = opens + np.cos(np.arange(80) * 0.4) * 0.005
    volumes = np.full(80, 100000.0)
    return [values.tolist() for values in (opens, highs, lows, closes, volumes)]


def test_stock_and_fund_rules_do_not_assume_all_funds_are_t0():
    assert rules_for_symbol('159981.SZ').same_day_auction_sell is True
    assert rules_for_symbol('159915.SZ').same_day_auction_sell is None
    assert rules_for_symbol('510300.SH').same_day_auction_sell is None
    assert rules_for_symbol('159981.SZ').price_tick == 0.001
    assert rules_for_symbol('159981.SZ').lot_size == 100
    assert rules_for_symbol('601869.SH').price_tick == 0.01
    assert rules_for_symbol('688001.SH').lot_size == 200


@pytest.mark.parametrize('mode,expected', [('nearest', 1.712), ('floor', 1.712), ('ceil', 1.713)])
def test_thousandth_price_grid(mode, expected):
    assert round_price(1.7124, 0.001, mode) == expected


def test_confirmation_uses_the_fund_grid_once(strategy, runner):
    assert strategy.short_confirmation_price(1.762, price_tick=0.001) == 1.765
    assert runner._rev_sell_trigger() == 1.765
    assert runner._rev_sell_trigger() == 1.765
    assert runner.st['daily_signal']['sell_trigger'] == 1.762
    assert strategy.short_confirmation_price(224.76) == 225.10


def test_initial_signal_keeps_precision_before_final_confirmation():
    arrays = daily_arrays()
    stock = compute_signal(*arrays, today_open=1.737)
    explicit_stock = compute_signal(*arrays, today_open=1.737, price_tick=0.01)
    fund = compute_signal(*arrays, today_open=1.737, price_tick=0.001)
    assert stock == explicit_stock
    raw = fund['open_price'] * (1 + fund['atr_pct'] * fund['sell_mult'] * 0.68)
    assert fund['sell_trigger_raw'] == round(raw, 3)
    assert fund['sell_trigger_raw'] != stock['sell_trigger_raw']


def test_reentry_minimum_distance_is_one_fund_tick_and_scaling_retains_it(strategy):
    opens = [1.712] * 80
    highs = [1.712] * 80
    lows = [1.702] * 80
    closes = [1.707] * 80
    fund = calculate_atr_reentry(1.712, opens, highs, lows, closes, price_tick=0.001)
    assert fund['sell_trigger'] == 1.713  # Zero historical up-units, one-tick floor.
    signal = {'trigger_base': 'CLOSE_FILL_ATR', 'reentry': copy.deepcopy(fund)}
    strategy.scale_reentry_signal(signal, price_tick=0.001)
    assert signal['sell_trigger'] == 1.713
    assert calculate_atr_reentry(1.712, opens, highs, lows, closes) == calculate_atr_reentry(
        1.712, opens, highs, lows, closes, price_tick=0.01)


def test_fixed_buyback_keeps_ask_thousandth(runner):
    runner.ctx = SimpleNamespace(get_full_tick=lambda codes: {'159981.SZ': {'askPrice': [1.713]}})
    assert runner._buyback_limit_price(1.71) == 1.713
    runner.ctx = SimpleNamespace(get_full_tick=lambda codes: {'159981.SZ': {'askPrice': []}})
    assert runner._buyback_limit_price(1.712) == 1.712


def test_sold_cycle_targets_and_tightening_keep_three_decimals(strategy, runner):
    runner.st.update(fstate=strategy.STATE_SPIKING, peak_price=1.770,
                     short_arm_trigger=1.765, short_arm_bars=1, short_legs=[])
    runner._execution_price = 1.763
    runner._submit_order = lambda *args: ('FILLED', -100)
    runner._handle_spiking(1.763)
    assert runner.st['buyback_target'] == 1.753
    assert runner.st['buyback_trigger_mult'] == 0.20
    assert any('sold Y1.763' in line and 'Y1.753' in line for line in runner.messages)
    runner.st['sell_elapsed_bars'] = 31
    runner._handle_sold(1.763)
    assert runner.st['buyback_target'] == 1.757
    assert runner.st['buyback_tightened'] is True


def test_fwd_trailing_and_sellback_hint_keep_three_decimals(runner):
    runner.st['daily_signal'].update(buy_trigger_floor=1.690, buy_trigger_max_trail=0)
    runner._update_fwd_buy_trigger(1.731)
    assert runner.st['daily_signal']['buy_trigger'] == 1.696
    assert runner.st['daily_signal']['sellback_target_hint'] == 1.716


def test_closing_fund_buy_is_a_whole_lot_and_fix_price_is_preserved(strategy, runner, monkeypatch):
    submits = []
    monkeypatch.setattr(strategy, 'order_shares', lambda *args: submits.append(args) or 12345)
    runner._clamp_buy_shares = lambda planned, price: 150
    runner._wait_for_fill = lambda signed, label, price: ('FILLED', signed)
    assert runner._submit_order(200, 1.713, 'REV-T buyback', style='FIX') == ('FILLED', 100)
    assert submits[0][0:4] == ('159981.SZ', 100, 'FIX', 1.713)
    assert any('Y1.713' in line for line in runner.messages)


def test_t0_profile_never_overrides_zero_broker_sellable(strategy, runner, monkeypatch):
    position = SimpleNamespace(m_strInstrumentID='159981', m_nVolume=16200,
                               m_nCanUseVolume=0, m_dOpenPrice=1.7138)
    monkeypatch.setattr(strategy, 'get_trade_detail_data', lambda *args: [position])
    assert runner._clamp_sell_shares(9700) == 0
    assert runner.st['base_can_use'] == 0


def test_position_adapter_uses_available_alias_and_does_not_guess_total(strategy):
    from Stragety.MiniQMT_Stragety.DayT.infra.connector import MockPosition
    assert MockPosition(SimpleNamespace(stock_code='159981.SZ', volume=16200, available=0)).m_nCanUseVolume == 0
    assert MockPosition(SimpleNamespace(stock_code='159981.SZ', volume=16200, available=16000)).m_nCanUseVolume == 16000
    assert MockPosition(SimpleNamespace(stock_code='159981.SZ', volume=16200)).m_nCanUseVolume == 0


def test_plan_logs_and_instrument_profile_are_fund_specific(runner):
    runner.st['do_short'] = True
    runner._log_instrument_rules()
    runner._log_next_t_entry_plan(1.712, 129.01, 16200)
    assert any('tick=0.001 lot=100' in line and 'CONFIRMED-AUCTION-T0' in line for line in runner.messages)
    assert any('confirmation >= Y1.765' in line for line in runner.messages)
    assert runner._fmt('price Y{:.2f}; percent {:.2f}%', 1.712, 0.15) == 'price Y1.712; percent 0.15%'
