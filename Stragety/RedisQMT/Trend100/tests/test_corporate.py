# -*- coding: utf-8 -*-
"""Synthetic unilateral distributions; not verified corporate-action records."""
import unittest
from dataclasses import replace
from datetime import date

import pandas as pd

from Stragety.RedisQMT.Trend100.core.corporate import CorporateBook, Distribution
from Stragety.RedisQMT.Trend100.core.execution import FeeRow, FeeSchedule, MinuteBar, PaperLedger
from Stragety.RedisQMT.Trend100.core.portfolio import BuyIntent


class CorporateTests(unittest.TestCase):
    def setUp(self):
        self.clock = pd.DatetimeIndex(['2026-01-05 14:58', '2026-01-05 14:59',
                                      '2026-01-06 09:30', '2026-01-07 09:30',
                                      '2026-01-08 09:30', '2026-01-09 09:30'])
        fees = FeeSchedule([FeeRow(date(2026, 1, 1), date(2026, 12, 31),
                                  'stock', .0003, 5, .0005, .00001)])
        self.ledger = PaperLedger(self.clock, fees, slippage=0)
        self.ledger.process(self.clock[0], {})
        self.ledger.buy(BuyIntent('A', 'stock', self.clock[0], 100, 100, 3))
        self.ledger.process(self.clock[1], {'A': self.bar(100)})
        self.event = Distribution('fixture', 'A', pd.Timestamp('2026-01-02 12:00'),
            date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), 1., .1,
            date(2026, 1, 8), date(2026, 1, 9), .9, 'synthetic fixture', 'synthetic net-cash model')
        self.book = CorporateBook([self.event])
        self.book.record_close(self.clock[1], self.ledger)

    def bar(self, price):
        return MinuteBar(price, price + 1, price - 1, price, 1_000_000,
                         100_000_000, True, 1000, 1)

    def test_ex_date_preserves_entitlement_value_without_spendable_cash(self):
        before = self.ledger.curve[-1]['equity']
        cash_before = self.ledger.cash
        self.book.open_session(date(2026, 1, 6), self.ledger)
        after = self.ledger.process(self.clock[2], {'A': self.bar(90)})
        self.assertEqual(self.ledger.cash, cash_before)
        self.assertEqual(self.ledger.cash_receivable, 100)
        self.assertEqual(self.ledger.share_receivable, {'A': 10})
        self.assertAlmostEqual(before, after)
        state = self.ledger.holdings['A'].exit_state
        self.assertEqual(state.entry_price, 90)
        self.assertAlmostEqual(state.entry_atr, 2.7)
        self.assertEqual(self.book.reference_factors(date(2026, 1, 6)), {'A': .9})

    def test_payment_delivery_and_sellability_have_separate_dates(self):
        self.book.open_session(date(2026, 1, 6), self.ledger)
        self.ledger.process(self.clock[2], {'A': self.bar(90)})
        cash = self.ledger.cash
        self.book.open_session(date(2026, 1, 7), self.ledger)
        self.ledger.process(self.clock[3], {'A': self.bar(90)})
        self.assertEqual(self.ledger.cash, cash + 100)
        self.assertEqual(self.ledger.cash_receivable, 0)
        self.book.open_session(date(2026, 1, 8), self.ledger)
        self.ledger.process(self.clock[4], {'A': self.bar(90)})
        holding = self.ledger.holdings['A']
        self.assertEqual(holding.shares, 110)
        self.assertEqual(holding.sellable(date(2026, 1, 8)), 100)
        self.assertEqual(holding.sellable(date(2026, 1, 9)), 110)
        self.assertEqual(self.ledger.share_receivable, {})

    def test_rights_survive_sale_and_missing_receivable_valuation_stops(self):
        self.book.open_session(date(2026, 1, 6), self.ledger)
        self.ledger.process(self.clock[2], {'A': self.bar(90)})
        self.ledger.sell('A', 'stop')
        self.book.open_session(date(2026, 1, 7), self.ledger)
        self.ledger.process(self.clock[3], {'A': self.bar(90)})
        self.assertNotIn('A', self.ledger.holdings)
        self.assertEqual(self.ledger.share_receivable, {'A': 10})
        with self.assertRaisesRegex(ValueError, 'Missing minute'):
            self.ledger.process(self.clock[4], {})
        self.book.open_session(date(2026, 1, 8), self.ledger)
        self.ledger.process(self.clock[4], {'A': self.bar(90)})
        self.assertEqual(self.ledger.holdings['A'].shares, 10)
        self.assertEqual(self.ledger.holdings['A'].sellable(date(2026, 1, 8)), 0)

    def test_pending_exit_does_not_sell_locked_bonus_then_sells_odd_remainder(self):
        self.book.open_session(date(2026, 1, 6), self.ledger)
        self.ledger.process(self.clock[2], {'A': self.bar(90)})
        self.book.open_session(date(2026, 1, 7), self.ledger)
        self.ledger.process(self.clock[3], {'A': self.bar(90)})
        self.ledger.sell('A', 'stop')
        self.book.open_session(date(2026, 1, 8), self.ledger)
        self.ledger.process(self.clock[4], {'A': self.bar(90)})
        self.assertEqual(self.ledger.holdings['A'].shares, 10)
        self.assertEqual(self.ledger.pending[0].shares, 10)
        self.book.open_session(date(2026, 1, 9), self.ledger)
        self.ledger.process(self.clock[5], {'A': self.bar(90)})
        self.assertEqual(self.ledger.holdings, {})
        self.assertEqual(self.ledger.fills[-1]['shares'], 10)

    def test_missing_record_snapshot_is_not_assumed_zero(self):
        book = CorporateBook([self.event])
        with self.assertRaisesRegex(ValueError, 'Missing record-date'):
            book.open_session(date(2026, 1, 6), self.ledger)

    def test_fractional_bonus_requires_explicit_resolution(self):
        book = CorporateBook([replace(self.event, bonus_per_share=.003)])
        book.record_close(self.clock[1], self.ledger)
        with self.assertRaisesRegex(ValueError, 'Fractional allocation'):
            book.open_session(date(2026, 1, 6), self.ledger)

    def test_corporate_session_not_applied_twice(self):
        self.book.open_session(date(2026, 1, 6), self.ledger)
        with self.assertRaisesRegex(ValueError, 'once per day'):
            self.book.open_session(date(2026, 1, 6), self.ledger)


if __name__ == '__main__':
    unittest.main()
