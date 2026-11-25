import numpy as np
import pandas as pd
import pytest

from Stragety.MiniQMT_Stragety.core.dayt_empirical_edge_lab import (
    EmpiricalPolicy, eligible, enrich, fit_model, label_examples,
)
from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import replay
from backtest.research_dayt_empirical_edge import training_gate


def minute_frame():
    pieces = []
    for number, day in enumerate(pd.bdate_range('2026-01-07', periods=5)):
        morning = pd.date_range(day + pd.Timedelta(hours=9, minutes=31), periods=120, freq='min')
        afternoon = pd.date_range(day + pd.Timedelta(hours=13, minutes=1), periods=120, freq='min')
        index = morning.append(afternoon).strftime('%Y%m%d%H%M%S')
        frame = pd.DataFrame(index=index)
        frame['date'] = index.str[:8]
        frame['session'] = index.str[:8] + np.where(index.str[8:12] < '1200', 'AM', 'PM')
        frame['minute'] = index.str[8:12]
        frame['open'] = 100.0 + number
        frame['close'] = frame['open']
        frame['high'] = frame['open'] + 0.1
        frame['low'] = frame['open'] - 0.1
        frame['volume'] = 100_000.0
        frame['prior_close'] = 100.0
        frame['cash_action'] = 0.0
        frame['cum_cash_action'] = 0.0
        frame['atr_pct'] = 0.03
        frame['ret3'] = 0.001
        frame['ret20'] = 0.002
        frame['vwap_gap'] = 0.001
        frame['daily_up'] = True
        frame['regime'] = 'RANGE_HIGH_VOL'
        pieces.append(frame)
    return enrich(pd.concat(pieces))


def test_labels_are_purged_and_same_horizon_intervals_do_not_overlap():
    frame = minute_frame()
    labels = label_examples(frame, 'TEST', 1_000_000, '20260108')
    assert (labels['label_end'].str[:8] <= '20260108').all()
    assert set(labels['horizon']) == {30, 60, 240}
    for horizon, rows in labels.loc[labels['side'] == 1].groupby('horizon'):
        rows = rows.sort_values('entry_time')
        assert (rows['entry_time'].iloc[1:].to_numpy() >= rows['label_end'].iloc[:-1].to_numpy()).all(), horizon
    changed = frame.copy()
    changed.loc[changed['date'] > '20260108', ['open', 'close']] *= 1.5
    pd.testing.assert_frame_equal(labels, label_examples(changed, 'TEST', 1_000_000, '20260108'))


def test_cash_dividend_does_not_become_fake_short_profit():
    frame = minute_frame()
    frame.loc[frame['date'] >= '20260108', 'cum_cash_action'] = 1.0
    frame.loc[frame['date'] >= '20260108', ['open', 'close']] = 99.0
    labels = label_examples(frame, 'TEST', 1_000_000, '20260108')
    pair = labels.loc[(labels['horizon'] == 240) & (labels['date'] == '20260107')]
    assert len(pair) == 2
    np.testing.assert_allclose(pair['gross'], 0.0, atol=1e-12)
    assert (pair['net'] < 0).all()


def training_labels():
    rows = []
    for day in pd.bdate_range('2025-10-01', periods=40).strftime('%Y%m%d'):
        for symbol in ('A', 'B', 'C'):
            rows.append(dict(date=day, symbol=symbol, label_end=day + '110000',
                             regime='UP_TREND', context='UP_TREND|continue|near|dailyup',
                             horizon=30, side=1, gross=0.02, cost_rate=0.006, net=0.014))
    return pd.DataFrame(rows)


def test_fit_rejects_future_labels_and_robust_gate_requires_stock_diversity():
    labels = training_labels()
    assert fit_model(labels, 'EMPIRICAL_CONTEXT', '20251231')['rules']
    assert not fit_model(labels.loc[labels['symbol'] == 'A'], 'EMPIRICAL_CONTEXT', '20251231')['rules']
    poisoned = labels.copy()
    poisoned.loc[0, 'label_end'] = '20260107110000'
    with pytest.raises(AssertionError, match='unsettled'):
        fit_model(poisoned, 'EMPIRICAL_CONTEXT', '20251231')


def test_robust_half_period_check_rejects_a_late_only_gain():
    labels = training_labels()
    dates = sorted(labels['date'].unique())
    labels.loc[labels['date'] < dates[20], 'net'] = -0.001
    assert not fit_model(labels, 'EMPIRICAL_SCENE', '20251231')['rules']


def test_context_features_are_prefix_invariant():
    frame = minute_frame()
    pd.testing.assert_frame_equal(enrich(frame).iloc[:350], enrich(frame.iloc[:350]))


def test_three_trading_days_are_not_three_natural_days_or_one_overnight():
    frame = minute_frame()
    key = frame.iloc[0]['context']
    rule = dict(horizon=720, side=1, state=key, gross_lower=0.03,
                net_lower=0.02, positive_example_fraction=0.8, last_label_end='20260105150000')
    model = dict(cutoff='20260106', rules={key: [rule]})
    policy = EmpiricalPolicy('EMPIRICAL_CONTEXT', model)
    result = replay(frame, 'EMPIRICAL_CONTEXT', 1_000_000, 10,
                    policy_catalog={'EMPIRICAL_CONTEXT': policy})
    assert result[1][0]['time'] == '20260107095400'
    assert result[2][0]['closed'] == '20260112095400'
    assert result[2][0]['wait_minutes'] > 720


def test_actual_small_lot_cost_can_reject_an_accepted_reference_forecast():
    frame = minute_frame()
    row = frame.loc['20260107095100'].to_dict()
    key = row['context']
    rule = dict(horizon=30, side=1, state=key, gross_lower=0.008,
                net_lower=0.002, positive_example_fraction=0.7, last_label_end='20260105150000')
    policy = EmpiricalPolicy('EMPIRICAL_CONTEXT', dict(cutoff='20260106', rules={key: [rule]}))
    assert policy.entry(row) == 1
    assert policy.execution_terms(row, 100, 10.0, 1) is None
    assert policy.execution_terms(row, 1000, 100.0, 1) is not None


def test_training_scene_gate_is_monotone_and_does_not_double_cycle_counts():
    rows = []
    for candidate in ('EMPIRICAL_SCENE', 'EMPIRICAL_CONTEXT'):
        for capital in (100_000, 1_000_000):
            for slip in (10, 20):
                for symbol in range(12):
                    rows.append(dict(candidate=candidate, capital=capital, slip_bps=slip,
                                     scenario_net=dict.fromkeys(('UP_TREND', 'SHOCK_UP', 'SHOCK_DOWN',
                                                                'DOWN_TREND', 'RANGE_HIGH_VOL', 'RANGE_LOW_VOL'), 100.0),
                                     scenario_cycles=dict.fromkeys(('UP_TREND', 'SHOCK_UP', 'SHOCK_DOWN',
                                                                   'DOWN_TREND', 'RANGE_HIGH_VOL', 'RANGE_LOW_VOL'), 2)))
    blocked = {'EMPIRICAL_SCENE': ['UP_TREND'], 'EMPIRICAL_CONTEXT': []}
    updated, diagnostics = training_gate(rows, blocked)
    assert 'UP_TREND' in updated['EMPIRICAL_SCENE']
    assert diagnostics['EMPIRICAL_CONTEXT']['UP_TREND']['canonical_cycles'] == 24
    assert 'UP_TREND' in updated['EMPIRICAL_CONTEXT']


def test_intraday_eligibility_does_not_request_a_lunch_or_close_crossing_exit():
    row = dict(regime='RANGE_HIGH_VOL', minute='0951')
    assert eligible(row, 60)
    row['minute'] = '1054'
    assert not eligible(row, 60)
    row['minute'] = '1427'
    assert not eligible(row, 30)
