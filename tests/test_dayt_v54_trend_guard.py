import unittest

from output.analysis.compare_v51_v39_minute import load_strategy


s = load_strategy('v54_nomom')


class TrendGuardTests(unittest.TestCase):
    def test_positive_three_session_return_blocks_new_short(self):
        result = s.short_trend_guard([100.0, 101.0, 102.0, 103.0])

        self.assertFalse(result['allowed'])
        self.assertAlmostEqual(result['return'], 0.03)

    def test_nonpositive_three_session_return_allows_new_short(self):
        result = s.short_trend_guard([100.0, 101.0, 99.0, 100.0])

        self.assertTrue(result['allowed'])
        self.assertAlmostEqual(result['return'], 0.0)

    def test_missing_or_invalid_history_fails_closed(self):
        missing = s.short_trend_guard([100.0, 101.0, 102.0])
        invalid = s.short_trend_guard([0.0, 101.0, 102.0, 103.0])

        self.assertFalse(missing['allowed'])
        self.assertFalse(invalid['allowed'])


if __name__ == '__main__':
    unittest.main()
