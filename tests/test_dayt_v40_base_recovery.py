import unittest
from unittest.mock import patch

from output.analysis.compare_v51_v39_minute import load_strategy


s = load_strategy('v39_v40_nomom')


class BaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.runner = s.StrategyRunner.__new__(s.StrategyRunner)
        self.runner.st = {
            'daily_signal': {'buy_trigger': 300.0},
            'fstate': s.STATE_IDLE,
            'recovery_active': True,
            'recovery_needed': 200,
            'recovery_dip_price': 0.0,
            'recovery_filled': 0,
            'recovery_order_count': 0,
            'recovery_done_today': False,
            'base_shares': 0,
            'base_can_use': 0,
            'do_short': False,
            'do_long': True,
        }

    def test_recovery_waits_for_fwd_t_buy_trigger(self):
        self.runner._handle_idle(301.0)

        self.assertEqual(self.runner.st['fstate'], s.STATE_IDLE)
        self.assertEqual(self.runner.st['recovery_dip_price'], 0.0)

        self.runner._handle_idle(300.0)

        self.assertEqual(
            self.runner.st['fstate'], s.STATE_RECOVERY_DIPPING)
        self.assertEqual(self.runner.st['recovery_dip_price'], 300.0)

    def test_confirmed_bounce_restores_base_without_long_leg(self):
        self.runner.st['fstate'] = s.STATE_RECOVERY_DIPPING
        self.runner.st['recovery_dip_price'] = 290.0
        self.runner.st['long_legs'] = []

        def refresh_position():
            if self.runner.st['recovery_filled']:
                self.runner.st['base_shares'] = 200

        with patch.object(self.runner, '_refresh_position', refresh_position):
            with patch.object(
                    self.runner, '_submit_order', return_value=('FILLED', 200)) as submit:
                self.runner._handle_recovery_dipping(
                    290.0 * (1.0 + s.cfg.BOUNCE_PCT + 0.000001))

        submit.assert_called_once()
        args = submit.call_args.args
        self.assertEqual(args[0], 200)
        self.assertEqual(args[2], 'FWD-T buy')
        self.assertEqual(self.runner.st['long_legs'], [])
        self.assertFalse(self.runner.st['recovery_active'])
        self.assertTrue(self.runner.st['recovery_done_today'])
        self.assertEqual(self.runner.st['fstate'], s.STATE_RECOVERY_DONE)
        self.assertFalse(self.runner.st['do_short'])
        self.assertFalse(self.runner.st['do_long'])

    def test_recovery_does_not_buy_before_bounce_confirmation(self):
        self.runner.st['fstate'] = s.STATE_RECOVERY_DIPPING
        self.runner.st['recovery_dip_price'] = 290.0

        with patch.object(self.runner, '_submit_order') as submit:
            self.runner._handle_recovery_dipping(
                290.0 * (1.0 + s.cfg.BOUNCE_PCT * 0.5))

        submit.assert_not_called()
        self.assertEqual(
            self.runner.st['fstate'], s.STATE_RECOVERY_DIPPING)


if __name__ == '__main__':
    unittest.main()
