# -*- coding: utf-8 -*-
"""Historical rule boundaries, independent of the downloaded bar timestamps."""
import unittest
from datetime import date

from Stragety.RedisQMT.Trend100.core.phases import has_closing_auction


class PhaseTests(unittest.TestCase):
    def test_sh_etf_rule_changes_on_effective_date(self):
        self.assertFalse(has_closing_auction('SH', 'etf', date(2026, 7, 3)))
        self.assertTrue(has_closing_auction('SH', 'etf', date(2026, 7, 6)))

    def test_stock_and_sz_etf_are_not_given_sh_fund_exception(self):
        self.assertTrue(has_closing_auction('SH', 'stock', date(2025, 10, 10)))
        self.assertTrue(has_closing_auction('SZ', 'etf', date(2025, 10, 10)))

    def test_unknown_markets_or_unverified_dates_stop(self):
        for exchange, kind, day in (('BJ', 'stock', date(2026, 1, 5)),
                                     ('SH', 'bond', date(2026, 1, 5)),
                                     ('SH', 'stock', date(2020, 1, 5))):
            with self.assertRaises(ValueError):
                has_closing_auction(exchange, kind, day)


if __name__ == '__main__':
    unittest.main()
