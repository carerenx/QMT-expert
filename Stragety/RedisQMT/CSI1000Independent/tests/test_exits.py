import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.exits import Position
from core.exits import completed_minute_exit


class ExitTests(unittest.TestCase):
    def test_entry_day_cannot_sell_even_after_stop_or_daily_exit(self):
        position = Position('20251010', 100.0, 100.0)
        decision = completed_minute_exit(position, '20251010', 90.0, 109.0, 1, True)
        self.assertEqual(decision.reason, '')
        self.assertEqual(decision.position.peak_price, 109.0)
        self.assertEqual(position.peak_price, 100.0)

    def test_entry_day_peak_can_arm_a_next_session_profit_exit(self):
        position = Position('20251010', 100.0, 100.0)
        first = completed_minute_exit(position, '20251010', 108.0, 110.0, 1)
        second = completed_minute_exit(first.position, '20251013', 106.0, 107.0, 2)
        self.assertEqual(first.reason, '')
        self.assertEqual(second.reason, 'trailing_profit')

    def test_stop_signal_does_not_claim_an_execution_price(self):
        position = Position('20251010', 100.0, 100.0)
        decision = completed_minute_exit(position, '20251013', 80.0, 90.0, 2)
        self.assertEqual(decision.reason, 'stop_loss')
        self.assertFalse(hasattr(decision, 'fill_price'))

    def test_ten_sessions_include_entry_and_ignore_calendar_weekends(self):
        position = Position('20251010', 100.0, 100.0)
        before = completed_minute_exit(position, '20251022', 101.0, 102.0, 9)
        last = completed_minute_exit(before.position, '20251023', 101.0, 102.0, 10)
        self.assertEqual(before.reason, '')
        self.assertEqual(last.reason, 'holding_limit')

    def test_prior_session_ma20_break_exits_without_waiting_for_stop(self):
        position = Position('20251010', 100.0, 100.0)
        decision = completed_minute_exit(position, '20251013', 99.0, 100.0, 2, True)
        self.assertEqual(decision.reason, 'ma20_break')

    def test_placeholder_minute_cannot_turn_into_a_loss_exit(self):
        position = Position('20251010', 100.0, 100.0)
        for invalid in (0.0, float('nan'), float('inf')):
            with self.subTest(close=invalid):
                with self.assertRaises(ValueError):
                    completed_minute_exit(position, '20251013', invalid, 100.0, 2)


if __name__ == '__main__':
    unittest.main()
