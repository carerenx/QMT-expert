# -*- coding: utf-8 -*-
"""Structural corruption and account arithmetic; no genuine market sample."""
import unittest
from types import SimpleNamespace

import pandas as pd

from Stragety.RedisQMT.Trend100.core.audit import audit_minute_session
from Stragety.RedisQMT.Trend100.core.metrics import account_metrics
from Stragety.RedisQMT.Trend100.core.replay import session_clock


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.clock = session_clock('2026-01-05')
        self.frame = pd.DataFrame({'open': 10., 'high': 11., 'low': 9., 'close': 10.,
                                   'volume': 1000, 'amount': 10000., 'tradable': True,
                                   'limit_up': 20., 'limit_down': 1.}, index=self.clock)
        self.frame.loc[self.clock[-3:], 'tradable'] = False
        self.raw = {'open': 10., 'high': 11., 'low': 9., 'close': 10.,
                    'volume': 240000, 'amount': 2400000., 'exchange': 'SH', 'asset_kind': 'stock'}

    def audit(self, frame=None, raw=None):
        return audit_minute_session(self.frame if frame is None else frame,
                                     self.raw if raw is None else raw, self.clock)

    def test_reconciles_price_volume_amount_without_claiming_authenticity(self):
        self.assertTrue(self.audit()['passed'])
        for column in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            changed = dict(self.raw)
            changed[column] *= 2
            self.assertIn('daily_mismatch:' + column, self.audit(raw=changed)['issues'])

    def test_removed_and_duplicate_minutes_cannot_pass(self):
        removed = self.frame.drop(self.clock[10])
        duplicate = pd.concat([self.frame, self.frame.iloc[[-1]]])
        self.assertIn('minute_clock_mismatch', self.audit(removed)['issues'])
        self.assertIn('minute_clock_mismatch', self.audit(duplicate)['issues'])

    def test_nan_and_nonpositive_prices_are_rejected(self):
        for value in (float('nan'), 0, -1):
            changed = self.frame.copy()
            changed.loc[self.clock[0], 'open'] = value
            self.assertFalse(self.audit(changed)['passed'])

    def test_volume_in_lots_instead_of_shares_is_detected(self):
        changed = self.frame.copy()
        changed['volume'] = 10
        self.assertTrue(any('amount_share_price_inconsistent' in issue for issue in self.audit(changed)['issues']))

    def test_unmodelled_closing_auction_cannot_silently_fill(self):
        changed = self.frame.copy()
        changed.loc[self.clock[-1], 'tradable'] = True
        self.assertTrue(any('auction_matching_unsupported' in issue for issue in self.audit(changed)['issues']))

    def test_zero_volume_day_requires_explicit_suspension_reference(self):
        changed = self.frame.copy()
        changed[['volume', 'amount']] = 0
        changed['tradable'] = False
        raw = dict(self.raw, volume=0, amount=0)
        self.assertFalse(self.audit(changed, raw)['passed'])
        raw.update(suspended=True, suspension_source='synthetic suspension fixture')
        self.assertTrue(self.audit(changed, raw)['passed'])

    def test_missing_fields_fail_with_named_issue(self):
        self.assertIn('missing_fields:amount', self.audit(self.frame.drop(columns='amount'))['issues'])

    def test_sh_etf_early_continuous_close_is_not_falsely_rejected(self):
        changed = self.frame.copy()
        changed['tradable'] = True
        raw = dict(self.raw, asset_kind='etf')
        self.assertTrue(self.audit(changed, raw)['passed'])

    def test_missing_market_classification_cannot_default_to_stock_rules(self):
        raw = dict(self.raw)
        raw.pop('asset_kind')
        self.assertTrue(self.audit(raw=raw)['issues'][0].startswith('unverified_phase_metadata'))


class MetricsTests(unittest.TestCase):
    def ledger(self, values, clock=None):
        if clock is None:
            clock = pd.date_range('2026-01-05 09:30', periods=len(values), freq='min')
        # Sparse observations here test arithmetic only, not certified data coverage.
        curve = [{'at': at, 'cash': value, 'equity': value, 'cash_receivable': 0}
                 for at, value in zip(clock, values)]
        return SimpleNamespace(clock=pd.DatetimeIndex(clock), curve=curve, fills=[],
                               holdings={}, cash_receivable=0., share_receivable={})

    def test_short_sample_does_not_output_cagr_or_claim_goal(self):
        result = account_metrics(self.ledger([150000, 300000]))
        self.assertEqual(result['total_return'], 1)
        self.assertIsNone(result['cagr'])
        self.assertIsNone(result['net_closed_position_win_rate'])
        self.assertIsNone(result['benchmark_excess_return'])
        self.assertIn('not certified', result['evidence_scope'])

    def test_calendar_day_cagr_includes_weekends_and_no_trade_time(self):
        clock = pd.DatetimeIndex(['2025-10-10 09:30', '2026-10-09 14:59'])
        result = account_metrics(self.ledger([150000, 300000], clock))
        self.assertEqual(result['calendar_days'], 365)
        self.assertAlmostEqual(result['cagr'], 2 ** (365.25 / 365) - 1)

    def test_initial_capital_is_included_in_drawdown_peak(self):
        self.assertAlmostEqual(account_metrics(self.ledger([135000, 150000]))['max_drawdown'], .1)
        self.assertAlmostEqual(account_metrics(self.ledger([200000, 140000, 190000]))['max_drawdown'], .3)

    def test_missing_clock_observation_prevents_metrics(self):
        ledger = self.ledger([150000, 160000, 155000])
        ledger.curve.pop(1)
        with self.assertRaisesRegex(ValueError, 'every clock minute'):
            account_metrics(ledger)

    def test_receivables_are_value_but_not_spendable_or_invested_cash(self):
        ledger = self.ledger([150000])
        ledger.curve[0].update(cash=140000, cash_receivable=1000)
        ledger.cash_receivable = 1000
        result = account_metrics(ledger)
        self.assertEqual(result['cash_receivable'], 1000)
        self.assertAlmostEqual(result['mean_invested_fraction_per_minute'], 9000 / 150000)

    def test_unrealized_position_value_remains_in_account_return(self):
        ledger = self.ledger([150000, 160000])
        ledger.holdings = {'A': object()}
        ledger.curve[-1]['cash'] = 100000
        result = account_metrics(ledger)
        self.assertEqual(result['open_symbols'], 1)
        self.assertEqual(result['net_profit'], 10000)


if __name__ == '__main__':
    unittest.main()
