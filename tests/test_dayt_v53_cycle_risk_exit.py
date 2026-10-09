import unittest
from types import SimpleNamespace
from unittest.mock import patch

from output.analysis.compare_v51_v39_minute import load_strategy


s = load_strategy('v53_nomom')


class CycleRiskExitTests(unittest.TestCase):
    def setUp(self):
        self.portfolio = s.PortfolioRunner(False)
        self.runner = s.StrategyRunner(self.portfolio, '601869.SH', lane=0)
        self.portfolio.runners = {'601869.SH': self.runner}
        self.runner._init_state()
        self.runner.baseline_shares = 200
        self.runner.st.update(
            initialized=True,
            trade_date='20260810',
            daily_signal={'atr_pct': 0.05},
            fstate=s.STATE_SOLD,
            short_legs=[(321.86, 100)],
            sell_fill_price=321.86,
            buyback_target=317.03,
            ladder_sell_target=0.0,
        )
        cycle = s.Cycle('risk', 0, 'SHORT', '20260806',
                        '2026-08-06T10:01:00', 0.05)
        cycle.fill(1, 100, 321.86, True, 5.0)
        cycle.advance_verified(['20260806', '20260807', '20260810'])
        cycle.label = 'REV-T sell'
        self.runner.cycle = cycle
        self.runner.execution_book.record(1, 'REV-T sell', -100, 321.86)

    def test_adverse_move_closes_owned_short_cycle_with_compete_order(self):
        self.runner._submitted_order_id = 2
        self.runner._execution_price = 334.00
        self.portfolio.conn.trader = SimpleNamespace(
            query_stock_order=lambda *args: SimpleNamespace(fee=5.0))
        with patch.object(s.ExecutionRunner, '_submit_order',
                          return_value=('FILLED', 100)) as submit:
            self.runner._handle_sold(334.00)

        submit.assert_called_once_with(
            100, 334.00, 'REV-T risk buyback(ADVERSE_MOVE)', 'COMPETE')
        self.assertIsNone(self.runner.cycle)
        self.assertEqual(self.runner.st['short_legs'], [])

    def test_max_holding_days_closes_even_without_adverse_move(self):
        self.runner._submitted_order_id = 2
        self.runner._execution_price = 322.00
        self.portfolio.conn.trader = SimpleNamespace(
            query_stock_order=lambda *args: SimpleNamespace(fee=5.0))
        with patch.object(s.ExecutionRunner, '_submit_order',
                          return_value=('FILLED', 100)) as submit:
            self.runner._handle_sold(322.00)

        submit.assert_called_once_with(
            100, 322.00, 'REV-T risk buyback(MAX_HOLDING_DAYS)', 'COMPETE')
        self.assertIsNone(self.runner.cycle)

    def test_failed_risk_exit_retains_cycle_and_short_leg(self):
        with patch.object(s.ExecutionRunner, '_submit_order',
                          return_value=('TIMEOUT', 0)):
            self.runner._handle_sold(334.00)

        self.assertIsNotNone(self.runner.cycle)
        self.assertEqual(self.runner.cycle.quantity, 100)
        self.assertEqual(self.runner.st['short_legs'], [(321.86, 100)])
        self.assertEqual(self.runner.st['fstate'], s.STATE_SOLD)


if __name__ == '__main__':
    unittest.main()
