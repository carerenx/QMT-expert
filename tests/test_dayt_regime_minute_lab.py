import numpy as np
import pandas as pd
import pytest

from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import fee, features, replay
from backtest.research_dayt_regime_lab import CAPITALS, SLIPS, learn_mapping


def account_frame():
    index = pd.date_range('2026-01-07 09:31', periods=45, freq='min').strftime('%Y%m%d%H%M%S')
    frame = pd.DataFrame(index=index)
    frame['date'] = index.str[:8]
    frame['session'] = '20260107AM'
    frame['minute'] = index.str[8:12]
    frame['open'] = 100.0
    frame['close'] = 100.0
    frame['high'] = 102.0
    frame['low'] = 98.0
    frame['volume'] = 100_000.0
    frame['amount'] = 1_000_000_000.0
    frame['prior_close'] = 100.0
    frame['cash_action'] = 0.0
    frame['atr_pct'] = 0.02
    frame['vwap_gap'] = 0.01
    frame['ret3'] = -0.001
    frame['daily_up'] = False
    frame['high10'] = 101.0
    frame['low10'] = 99.0
    frame['regime'] = 'UNKNOWN'
    frame.iloc[14, frame.columns.get_loc('regime')] = 'RANGE_HIGH_VOL'
    return frame


def test_signal_is_delayed_and_completed_cycle_reconciles_to_hold():
    frame = account_frame()
    frame.loc[frame.index[20]:, ['open', 'close']] = 99.0
    summary, fills, cycles, _, _ = replay(frame, 'REV20', 1_000_000, 10)
    assert len(fills) == 2
    assert fills[0]['signal_time'] == '20260107094500'
    assert fills[0]['time'] == '20260107094800'
    assert all(fill['event_epoch'] > fill['available_epoch'] for fill in fills)
    assert fills[0]['quantity'] == -1000
    assert summary['final_shares'] == summary['initial_shares']
    assert summary['excess_pct'] * 10000 == pytest.approx(cycles[0]['net'])
    assert cycles[0]['net'] > 0


def test_open_leg_loss_is_counted_even_with_no_closed_cycles():
    frame = account_frame()
    frame.loc[frame.index[30]:, 'close'] = 100.2
    summary, fills, cycles, _, _ = replay(frame, 'REV20', 1_000_000, 10)
    assert len(fills) == 1
    assert cycles == []
    assert summary['open_contribution'] < 0
    assert summary['excess_pct'] * 10000 == pytest.approx(summary['open_contribution'])


def test_dividend_is_entitlement_difference_and_ex_drop_is_not_profit():
    frame = account_frame().iloc[:30].copy()
    later = account_frame().iloc[:12].copy()
    later.index = later.index.str.replace('20260107', '20260108')
    later['date'] = '20260108'
    later['session'] = '20260108AM'
    later['cash_action'] = 1.0
    later['prior_close'] = 99.0
    later['open'] = 99.0
    later['close'] = 99.0
    later['regime'] = 'UNKNOWN'
    frame = pd.concat([frame, later])
    summary, fills, cycles, _, _ = replay(frame, 'REV20', 1_000_000, 10)
    assert len(cycles) == 1
    assert fills[-1]['reason'] == 'timeout'
    assert cycles[0]['net'] < 0
    assert summary['dividend_receivable'] == 4000


def test_no_volume_and_unknown_regime_never_generate_fills():
    frame = account_frame()
    frame['volume'] = 0.0
    assert replay(frame, 'REV20', 1_000_000, 10)[0]['fills'] == 0
    frame['regime'] = 'UNKNOWN'
    frame['volume'] = 100_000.0
    assert replay(frame, 'MOMENTUM', 1_000_000, 10)[0]['fills'] == 0


def test_t1_caps_reverse_sales_after_forward_inventory_is_added():
    frame = account_frame()
    frame['regime'] = 'RANGE_HIGH_VOL'
    frame['vwap_gap'] = -0.01
    frame['ret3'] = 0.001
    # Repeated next-minute profit taking consumes old shares; new buys remain locked.
    frame['atr_pct'] = 0.01
    frame['close'] = 101.0
    frame['open'] = 100.0
    summary, fills, _, _, _ = replay(frame, 'FWD15', 1_000_000, 10)
    assert summary['rejections']['t1'] > 0
    assert all(0 <= fill['sellable'] <= fill['shares'] and fill['cash'] >= 0 for fill in fills)
    assert sum(-fill['quantity'] for fill in fills if fill['quantity'] < 0) <= 5000


def test_features_do_not_change_when_future_prices_are_replaced():
    indices = []
    for day in pd.bdate_range('2025-11-03', periods=35):
        indices.extend(pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=120, freq='min').strftime('%Y%m%d%H%M%S'))
    values = 100 + np.arange(len(indices)) * 0.001 + np.sin(np.arange(len(indices))) * 0.02
    frame = pd.DataFrame(dict(open=values, high=values + 0.1, low=values - 0.1,
                              close=values, volume=10000.0, amount=100000000.0), index=indices)
    market = frame.copy()
    original = features(frame, market, [])
    cut = len(frame) - 60
    altered = frame.copy()
    altered.iloc[cut:, :4] *= 1.1
    changed = features(altered, market, [])
    pd.testing.assert_frame_equal(original.iloc[:cut], changed.iloc[:cut])
    prefix = features(frame.iloc[:cut], market.iloc[:cut], [])
    pd.testing.assert_frame_equal(original.iloc[:cut], prefix)


def test_router_sample_count_does_not_duplicate_cost_and_capital_scenarios():
    training = []
    for candidate in ('REV15', 'REV20', 'REV30', 'FWD15', 'SYMMETRIC', 'PULLBACK', 'MOMENTUM'):
        for capital in CAPITALS:
            for slip in SLIPS:
                for symbol in range(7):
                    training.append(dict(candidate=candidate, capital=capital, slip_bps=slip, symbol=symbol,
                                         scenario_net={'UP_TREND': 100.0},
                                         scenario_cycles={'UP_TREND': 2}))
    # Fill other regime keys so all candidate gates are exercised.
    from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import REGIMES
    for row in training:
        row['scenario_net'].update({key: 0.0 for key in REGIMES if key != 'UP_TREND'})
        row['scenario_cycles'].update({key: 0 for key in REGIMES if key != 'UP_TREND'})
    mapping, diagnostics = learn_mapping(training)
    assert mapping['UP_TREND'] == 'HOLD'
    assert diagnostics['UP_TREND'][0]['canonical_cycles'] == 14


def test_minimum_fee_and_sell_stamp_are_included():
    assert fee(100, 10) == pytest.approx(5.01)
    assert fee(-100, 10) == pytest.approx(5.51)


def test_impact_cost_does_not_remove_a_narrow_minute_fill():
    frame = account_frame()
    frame['high'] = 100.01
    frame['low'] = 99.99
    cheap, cheap_fills, _, _, _ = replay(frame, 'REV20', 1_000_000, 10)
    costly, costly_fills, _, _, _ = replay(frame, 'REV20', 1_000_000, 20)
    assert len(cheap_fills) == len(costly_fills) == 1
    assert cheap_fills[0]['price'] == costly_fills[0]['price'] == 100.0
    assert cheap['excess_pct'] > costly['excess_pct']
    assert costly_fills[0]['slippage_cost'] == cheap_fills[0]['slippage_cost'] * 2
