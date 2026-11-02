# -*- coding: utf-8 -*-
"""Synthetic fill/ledger checks; these inputs are not a market backtest."""
import unittest
from datetime import date

import pandas as pd

from Stragety.RedisQMT.Trend100.core.execution import (
    FeeRow, FeeSchedule, MinuteBar, PaperLedger,
)
from Stragety.RedisQMT.Trend100.core.portfolio import BuyIntent


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.clock = pd.DatetimeIndex(['2026-01-05 09:45', '2026-01-05 09:46',
                                      '2026-01-05 09:47', '2026-01-06 09:30',
                                      '2026-01-06 09:31', '2026-01-06 09:32'])
        # Explicit test tariffs; not assertions about current broker charges.
        self.fees = FeeSchedule([
            FeeRow(date(2026, 1, 1), date(2026, 12, 31), 'stock', .0003, 5, .0005, .00001),
            FeeRow(date(2026, 1, 1), date(2026, 12, 31), 'etf', .0003, 5, 0, 0),
        ])
        self.ledger = PaperLedger(self.clock, self.fees, slippage=0)
        self.ledger.process(self.clock[0], {})

    def bar(self, price=10, volume=1_000_000, amount=10_000_000, tradable=True,
            up=1000, down=1, high=None, low=None):
        return MinuteBar(price, high or price + .1, low or price - .1, price,
                         volume, amount, tradable, up, down)

    def buy(self, shares=300, price=10, kind='stock', code='A'):
        self.ledger.buy(BuyIntent(code, kind, self.clock[0], shares, price, price * .03))

    def test_next_minute_only_with_cash_fee_and_t1_accounting(self):
        self.buy()
        self.assertEqual(self.ledger.cash, 150000)
        self.assertEqual(self.ledger.holdings, {})
        self.ledger.process(self.clock[1], {'A': self.bar()})
        self.assertAlmostEqual(self.ledger.cash, 146994.97)
        self.assertEqual(self.ledger.holdings['A'].shares, 300)
        self.ledger.sell('A', 'stop')
        self.ledger.process(self.clock[2], {'A': self.bar(9)})
        self.assertEqual(self.ledger.holdings['A'].shares, 300)
        self.assertEqual(len(self.ledger.fills), 1)
        self.ledger.process(self.clock[3], {'A': self.bar(8)})
        self.assertNotIn('A', self.ledger.holdings)
        self.assertEqual(len(self.ledger.fills), 2)
        self.assertAlmostEqual(self.ledger.cash, 149388.75)

    def test_buy_expires_after_one_bar_when_limit_not_met(self):
        self.buy(price=10)
        self.ledger.process(self.clock[1], {'A': self.bar(11)})
        self.assertEqual(self.ledger.pending, [])
        self.assertEqual(self.ledger.holdings, {})
        self.assertEqual(self.ledger.cancellations[0]['remaining'], 300)
        self.ledger.process(self.clock[2], {'A': self.bar(9)})
        self.assertEqual(self.ledger.fills, [])

    def test_buy_partial_fill_cancels_remaining_and_respects_amount_cap(self):
        self.buy(shares=500)
        self.ledger.process(self.clock[1], {'A': self.bar(amount=200000)})
        self.assertEqual(self.ledger.holdings['A'].shares, 200)
        self.assertEqual(self.ledger.cancellations[0]['remaining'], 300)
        self.assertEqual(self.ledger.pending, [])

    def test_sell_partial_fills_keep_residual_and_charge_minimum_once_per_day(self):
        self.buy()
        self.ledger.process(self.clock[1], {'A': self.bar()})
        self.ledger.sell('A', 'stop')
        self.ledger.process(self.clock[2], {'A': self.bar()})
        for at in self.clock[3:]:
            self.ledger.process(at, {'A': self.bar(volume=10000)})
        sells = [fill for fill in self.ledger.fills if fill['side'] == 'sell']
        self.assertEqual([fill['shares'] for fill in sells], [100, 100, 100])
        self.assertEqual([fill['fees'] for fill in sells], [5.51, .51, .51])
        self.assertEqual(self.ledger.holdings, {})
        self.assertEqual(self.ledger.pending, [])

    def test_limit_down_and_suspension_preserve_holdings_and_losses(self):
        self.buy()
        self.ledger.process(self.clock[1], {'A': self.bar()})
        self.ledger.sell('A', 'stop')
        self.ledger.process(self.clock[2], {'A': self.bar()})
        self.ledger.process(self.clock[3], {'A': self.bar(9, down=9, low=9)})
        self.assertEqual(self.ledger.holdings['A'].shares, 300)
        self.assertEqual(len(self.ledger.pending), 1)
        self.ledger.process(self.clock[4], {'A': self.bar(8, tradable=False)})
        self.assertEqual(self.ledger.holdings['A'].shares, 300)
        self.assertLess(self.ledger.curve[-1]['equity'], self.ledger.curve[-2]['equity'])

    def test_sells_settle_before_buys_even_if_buys_submitted_first(self):
        self.buy()
        self.ledger.process(self.clock[1], {'A': self.bar()})
        self.ledger.process(self.clock[2], {'A': self.bar()})
        self.ledger.process(self.clock[3], {'A': self.bar()})
        self.ledger.buy(BuyIntent('B', 'etf', self.clock[3], 100, 10, .3))
        self.ledger.sell('A', 'stop')
        self.ledger.process(self.clock[4], {'A': self.bar(), 'B': self.bar()})
        self.assertEqual([fill['side'] for fill in self.ledger.fills[-2:]], ['sell', 'buy'])

    def test_account_halt_does_not_liquidate_at_the_trigger_price(self):
        self.buy(shares=1000, price=100)
        self.ledger.process(self.clock[1], {'A': self.bar(100)})
        self.ledger.process(self.clock[2], {'A': self.bar(69)})
        self.assertEqual(self.ledger.risk.level, 2)
        self.assertIn('A', self.ledger.holdings)
        self.assertEqual(self.ledger.pending[0].reason, 'account_halt')
        self.ledger.process(self.clock[3], {'A': self.bar(65)})
        self.assertEqual(self.ledger.fills[-1]['price'], 65)
        self.assertEqual(self.ledger.holdings, {})

    def test_missing_bars_or_fees_fail_before_advancing_clock(self):
        self.buy()
        with self.assertRaisesRegex(ValueError, 'Missing minute'):
            self.ledger.process(self.clock[1], {})
        self.assertEqual(self.ledger.now, self.clock[0])
        self.ledger.fees = FeeSchedule([])
        with self.assertRaisesRegex(ValueError, 'fee coverage'):
            self.ledger.process(self.clock[1], {'A': self.bar()})
        self.assertEqual(self.ledger.now, self.clock[0])
        self.assertEqual(self.ledger.cash, 150000)

    def test_clock_skips_and_same_minute_reprocessing_rejected(self):
        with self.assertRaisesRegex(ValueError, 'skip, repeat'):
            self.ledger.process(self.clock[2], {})
        with self.assertRaisesRegex(ValueError, 'skip, repeat'):
            self.ledger.process(self.clock[0], {})

    def test_cash_cannot_go_negative_including_stock_transfer_fee(self):
        self.buy(shares=15000, price=10)
        self.ledger.process(self.clock[1], {'A': self.bar(volume=2_000_000, amount=20_000_000)})
        self.assertEqual(self.ledger.holdings['A'].shares, 14900)
        self.assertGreaterEqual(self.ledger.cash, 0)

    def test_etf_does_not_inherit_stock_taxes(self):
        self.buy(kind='etf')
        self.ledger.process(self.clock[1], {'A': self.bar()})
        self.assertEqual(self.ledger.fills[0]['fees'], 5)

    def test_slippage_tick_rounding_and_range_checks(self):
        self.ledger.slippage = .001
        self.buy(price=10.02)
        self.ledger.process(self.clock[1], {'A': self.bar(10)})
        self.assertEqual(self.ledger.fills[0]['price'], 10.01)


if __name__ == '__main__':
    unittest.main()
