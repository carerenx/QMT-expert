# -*- coding: utf-8 -*-
"""Synthetic mechanics checks only; no historical performance inference."""
import unittest
from datetime import date

import pandas as pd

from Stragety.RedisQMT.Trend100.core.signals import (
    AccountRisk, PositionExit, daily_setup, entry_signal, market_open,
)


class SignalTests(unittest.TestCase):
    def test_daily_setup_ignores_future_winner(self):
        index = pd.bdate_range('2025-01-01', periods=62)
        frame = pd.DataFrame({'open': 100., 'high': 102., 'low': 98.,
                              'close': 100., 'volume': 1_000_000.,
                              'amount': 200_000_000.}, index=index)
        asof = index[60]
        frame.loc[asof, ['close', 'high', 'volume']] = [104., 105., 2_000_000.]
        before = daily_setup(frame.iloc[:61], asof)
        frame.loc[index[61], ['close', 'high', 'volume']] = [9999., 9999., 9e9]
        self.assertEqual(before, daily_setup(frame, asof))
        self.assertAlmostEqual(before['momentum'], 0.04)
        self.assertIsNone(daily_setup(frame.iloc[:60], index[59]))
        self.assertIsNone(daily_setup(frame.iloc[:60], asof))

    def test_market_gate_uses_only_completed_daily_data(self):
        index = pd.bdate_range('2025-01-01', periods=61)
        series = pd.Series(range(100, 161), index=index)
        self.assertTrue(market_open(series, index[59]))
        series.iloc[-1] = 0
        self.assertTrue(market_open(series, index[59]))
        self.assertFalse(market_open(series, index[58]))
        self.assertFalse(market_open(series.iloc[:60], index[60]))

    def test_entry_requires_complete_opening_range_and_respects_chase_cap(self):
        index = pd.date_range('2026-01-05 09:30', periods=17, freq='min')
        frame = pd.DataFrame({'high': 101., 'close': 100.}, index=index)
        frame.loc[index[15], 'close'] = 102.
        self.assertTrue(entry_signal(frame, index[15], 100.))
        frame.loc[index[16], 'high'] = 9999.
        self.assertTrue(entry_signal(frame, index[15], 100.))
        self.assertFalse(entry_signal(frame.drop(index[2]), index[15], 100.))
        frame.loc[index[15], 'close'] = 104.
        self.assertFalse(entry_signal(frame, index[15], 100.))

    def test_sizing_risk_cash_liquidity_and_whole_lots(self):
        risk = AccountRisk()
        self.assertEqual(risk.size(150000, 150000, 100, 5, 10_000_000), 200)
        self.assertEqual(risk.size(150000, 10000, 100, 5, 10_000_000), 0)
        self.assertEqual(risk.size(150000, 10005, 100, 5, 10_000_000), 100)
        self.assertEqual(risk.size(150000, 150000, 100, 5, 500000), 0)

    def test_drawdown_thresholds_latch_even_after_recovery(self):
        risk = AccountRisk()
        risk.update(135000)
        self.assertEqual(risk.level, 1)
        risk.update(150000)
        self.assertEqual(risk.level, 1)
        self.assertEqual(risk.size(150000, 150000, 100, 5, 10_000_000), 100)
        risk.update(120000)
        risk.update(180000)
        self.assertEqual(risk.size(180000, 180000, 100, 5, 10_000_000), 0)

    def test_t1_prevents_same_day_stop_and_no_sellable_exit(self):
        position = PositionExit(100, 3, date(2026, 1, 5))
        self.assertIsNone(position.observe(pd.Timestamp('2026-01-05 14:00'), 80, 1, 100))
        self.assertIsNone(position.observe(pd.Timestamp('2026-01-06 09:30'), 80, 2, 0))
        self.assertEqual(position.observe(pd.Timestamp('2026-01-06 09:31'), 80, 2, 100), 'stop')

    def test_trailing_exit_and_twentieth_session_limit(self):
        position = PositionExit(100, 3, date(2026, 1, 5))
        self.assertIsNone(position.observe(pd.Timestamp('2026-01-06 10:00'), 113, 2, 100))
        self.assertEqual(position.observe(pd.Timestamp('2026-01-06 10:01'), 106, 2, 100), 'stop')
        position = PositionExit(100, 3, date(2026, 1, 5))
        self.assertIsNone(position.observe(pd.Timestamp('2026-02-02 14:54'), 101, 20, 100))
        self.assertEqual(position.observe(pd.Timestamp('2026-02-02 14:55'), 101, 20, 100), 'holding_limit')


if __name__ == '__main__':
    unittest.main()
