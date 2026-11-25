import pandas as pd
import pytest

from Stragety.MiniQMT_Stragety.core.dayt_cost_space_lab import CATALOG, enrich, round_trip_rate
from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import replay


def signal(**changes):
    row = dict(regime='RANGE_HIGH_VOL', atr_pct=0.04, close=100.0, vwap=103.0,
               vwap_gap=-0.03, ret1=0.001, ret3=0.001, ret5=-0.001,
               index_ret3=0.0, past_high30=103.0, past_low30=97.0)
    row.update(changes)
    return row


def test_cost_gate_rejects_small_lots_but_no_gate_ablation_keeps_them():
    row = signal(vwap=101.65)
    assert CATALOG['COST_REVERT'].entry(row) == 1
    assert CATALOG['COST_REVERT'].execution_terms(row, 100, 100.0, 1) is None
    assert CATALOG['RAW_SPACE_REVERT'].execution_terms(row, 100, 100.0, 1) is not None
    assert CATALOG['COST_REVERT'].execution_terms(row, 1000, 100.0, 1) is not None


def test_execution_rechecks_anchor_after_price_gap_and_partial_fill():
    row = signal()
    assert CATALOG['COST_REVERT'].execution_terms(row, 1000, 100.0, 1) is not None
    assert CATALOG['COST_REVERT'].execution_terms(row, 1000, 102.0, 1) is None
    thin = signal(vwap=101.5)
    assert CATALOG['COST_REVERT'].execution_terms(thin, 100, 100.0, 1) is None


def test_family_routes_are_distinct_and_unknown_does_not_trade():
    row = signal()
    assert CATALOG['COST_RANGE'].entry(row) == 1
    assert CATALOG['COST_TREND'].entry(row) == 0
    trend = signal(regime='UP_TREND')
    assert CATALOG['COST_TREND'].entry(trend) == 1
    assert CATALOG['COST_RANGE'].entry(trend) == 0
    shock = signal(regime='SHOCK_DOWN')
    assert CATALOG['COST_SHOCK'].entry(shock) == 1
    assert CATALOG['COST_SHOCK'].entry(signal(regime='UNKNOWN')) == 0


def test_sell_and_buy_anchors_both_require_favorable_known_price_space():
    row = signal(close=100.0, vwap=97.0, vwap_gap=0.03, ret3=-0.001)
    assert CATALOG['COST_REVERT'].entry(row) == -1
    terms = CATALOG['COST_REVERT'].execution_terms(row, 1000, 100.0, -1)
    assert terms['target_pct'] == pytest.approx(0.018)
    assert terms['stop_pct'] == pytest.approx(0.0135)
    assert terms['admission_margin'] > 0


def test_space_enrichment_prefix_invariance():
    index = pd.date_range('2026-01-07 09:31', periods=60, freq='min').strftime('%Y%m%d%H%M%S')
    frame = pd.DataFrame(dict(close=range(100, 160), high=range(101, 161), low=range(99, 159), session='AM'), index=index)
    full = enrich(frame)
    prefix = enrich(frame.iloc[:45])
    pd.testing.assert_frame_equal(full.iloc[:45], prefix)
    changed = frame.copy()
    changed.iloc[45:, :3] *= 2
    pd.testing.assert_frame_equal(full.iloc[:45], enrich(changed).iloc[:45])


def test_locked_exit_uses_admitted_space_not_default_atr_target():
    index = pd.date_range('2026-01-07 09:31', periods=45, freq='min').strftime('%Y%m%d%H%M%S')
    frame = pd.DataFrame([signal() for _ in index], index=index)
    frame['date'] = '20260107'
    frame['session'] = '20260107AM'
    frame['minute'] = index.str[8:12]
    frame['open'] = 100.0
    frame['high'] = 103.0
    frame['low'] = 99.0
    frame['volume'] = 100000.0
    frame['prior_close'] = 100.0
    frame['cash_action'] = 0.0
    frame['regime'] = 'UNKNOWN'
    frame.iloc[14, frame.columns.get_loc('regime')] = 'RANGE_HIGH_VOL'
    # +1.3% would meet the old 1.2% ATR target but not the locked 1.8% space.
    frame.loc[index[20]:, 'close'] = 101.3
    result = replay(frame, 'COST_REVERT', 1_000_000, 10, policy_catalog=CATALOG)
    assert result[0]['fills'] == 1
    assert result[0]['open_leg']['target_pct'] == pytest.approx(0.018)
    assert result[0]['open_leg']['max_minutes'] == 120
    assert result[0]['open_leg']['admission_margin'] > 0


def test_worst_cost_estimate_includes_both_minimum_commissions():
    assert round_trip_rate(100, 10, 10.1) > 0.014
    assert round_trip_rate(100, 10, 10.1) > round_trip_rate(1000, 10, 10.1)
    assert round_trip_rate(0, 10, 10.1) == float('inf')
