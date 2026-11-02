import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.signals import daily_features
from core.signals import minute_entry_signals
from core.signals import ranked_candidates


class SignalTests(unittest.TestCase):
    def test_future_prices_and_actions_do_not_change_past_features(self):
        dates = pd.bdate_range('2024-01-01', periods=180).strftime('%Y%m%d')
        price = np.linspace(10.0, 20.0, len(dates))
        raw = pd.DataFrame({'open': price, 'high': price + .2, 'low': price - .2,
                            'close': price, 'volume': 100_000, 'amount': 100_000_000}, index=dates)
        events = [{'date': dates[150], 'dr': 2.0}]
        before = daily_features(raw.iloc[:140], [])
        after = daily_features(raw, events).iloc[:140]
        pd.testing.assert_frame_equal(before, after)

    def test_membership_uses_trade_date_and_signal_uses_previous_date(self):
        dates = pd.bdate_range('2025-01-01', periods=70).strftime('%Y%m%d')
        benchmark = pd.DataFrame({'close': np.arange(100.0, 170.0)}, index=dates)
        frame = pd.DataFrame({'eligible': True, 'ret20': .2}, index=dates)
        snapshots = {dates[0]: ['000001.SZ'], dates[65]: ['000002.SZ']}
        candidates = ranked_candidates({'000001.SZ': frame, '000002.SZ': frame}, benchmark, snapshots)
        changed = candidates[candidates.trade_date.eq(dates[65])].iloc[0]
        self.assertEqual(changed.code, '000002.SZ')
        self.assertEqual(changed.signal_date, dates[64])
        self.assertFalse((candidates.trade_date == candidates.signal_date).any())

    def test_breakout_excludes_current_high_and_future_minutes(self):
        times = pd.date_range('2025-10-10 09:31', periods=40, freq='min').strftime('%Y%m%d%H%M%S')
        raw = pd.DataFrame({'open': 10.0, 'high': 10.0, 'low': 10.0, 'close': 10.0,
                            'volume': 100, 'amount': 100_000.0}, index=times)
        raw.loc[times[15], ['high', 'close']] = 10.2
        self.assertTrue(minute_entry_signals(raw).loc[times[15]])
        before = minute_entry_signals(raw.iloc[:25])
        raw.loc[times[30]:, ['open', 'high', 'low', 'close']] = 100.0
        pd.testing.assert_series_equal(before, minute_entry_signals(raw).iloc[:25])
        self.assertFalse(minute_entry_signals(raw).iloc[:15].any())


if __name__ == '__main__':
    unittest.main()
