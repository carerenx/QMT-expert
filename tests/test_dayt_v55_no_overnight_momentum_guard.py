import unittest

from output.analysis.compare_v51_v39_minute import load_strategy


s = load_strategy('v55_nomom')


class NoOvernightMomentumGuardTests(unittest.TestCase):
    def test_five_day_return_above_three_percent_blocks_short(self):
        result = s.short_five_day_momentum_guard(
            [100.0, 100.5, 101.0, 101.5, 102.0, 103.1])

        self.assertFalse(result['allowed'])
        self.assertAlmostEqual(result['return'], 0.031)

    def test_five_day_return_at_threshold_allows_short(self):
        result = s.short_five_day_momentum_guard(
            [100.0, 100.5, 101.0, 101.5, 102.0, 103.0])

        self.assertTrue(result['allowed'])
        self.assertAlmostEqual(result['return'], 0.03)

    def test_research_threshold_can_be_overridden(self):
        closes = [100.0, 100.5, 101.0, 101.5, 102.0, 104.0]

        blocked = s.short_five_day_momentum_guard(closes, 0.03)
        allowed = s.short_five_day_momentum_guard(closes, 0.05)

        self.assertFalse(blocked['allowed'])
        self.assertTrue(allowed['allowed'])

    def test_missing_history_fails_closed(self):
        result = s.short_five_day_momentum_guard([100.0, 101.0])

        self.assertFalse(result['allowed'])
        self.assertIsNone(result['return'])

    def test_session_exit_starts_at_1450(self):
        self.assertEqual(s.short_session_exit_reason('14:49:59'), '')
        self.assertEqual(
            s.short_session_exit_reason('14:50:00'), 'SESSION_END')


if __name__ == '__main__':
    unittest.main()
