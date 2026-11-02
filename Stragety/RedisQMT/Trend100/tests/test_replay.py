# -*- coding: utf-8 -*-
"""Full synthetic sessions exercise ranking -> signals -> ledger continuity."""
import unittest
from dataclasses import replace
from datetime import date

import pandas as pd

from Stragety.RedisQMT.Trend100.core.candidates import SecurityState
from Stragety.RedisQMT.Trend100.core.corporate import Distribution
from Stragety.RedisQMT.Trend100.core.execution import FeeRow, FeeSchedule
from Stragety.RedisQMT.Trend100.core.replay import ReplaySession, run_replay, session_clock


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.calendar = pd.bdate_range(end='2026-01-07', periods=64)
        self.daily = pd.DataFrame({'open': 100., 'high': 102., 'low': 98., 'close': 100.,
                                   'volume': 1_000_000., 'amount': 200_000_000.}, index=self.calendar)
        self.daily.loc[self.calendar[60], ['close', 'high', 'volume']] = [104, 105, 2_000_000]
        self.daily.loc[self.calendar[61], ['open', 'high', 'low', 'close', 'volume']] = [104, 107, 103, 106, 240_000_000]
        self.daily.loc[self.calendar[62], ['open', 'high', 'low', 'close', 'volume']] = [90, 91, 89, 90, 240_000_000]
        self.index = pd.Series(range(100, 164), index=self.calendar, dtype=float)
        self.fees = FeeSchedule([FeeRow(date(2026, 1, 1), date(2026, 12, 31),
                                        'stock', .0003, 5, .0005, .00001)])

    def session(self, offset, price):
        current = self.calendar[offset]
        previous = self.calendar[offset - 1]
        state = SecurityState('A', 'stock', previous.date(), previous + pd.Timedelta(hours=15),
                              300, False, False, False, 'mainboard', '', '')
        index = session_clock(current.date())
        frame = pd.DataFrame({'open': float(price), 'high': float(price + 1),
                              'low': float(price - 1), 'close': float(price),
                              'volume': 1_000_000, 'amount': 100_000_000.,
                              'tradable': True, 'limit_up': 1000., 'limit_down': 1.}, index=index)
        if offset == 61:
            opening = index[:15]
            frame.loc[opening, ['open', 'high', 'low', 'close']] = [104., 105., 103., 104.]
        frame.loc[index[-3:], 'tradable'] = False
        frame['amount'] = frame.close * frame.volume
        raw_daily = {'open': frame.open.iloc[0], 'high': frame.high.max(),
                     'low': frame.low.min(), 'close': frame.close.iloc[-1],
                     'volume': frame.volume.sum(), 'amount': frame.amount.sum(),
                     'exchange': 'SH', 'asset_kind': 'stock'}
        return ReplaySession(current.date(), self.calendar, [state], {'A': self.daily},
                             self.index, {'A': frame}, {'A': raw_daily})

    def test_continuous_two_sessions_generate_next_minute_entry_and_t1_exit(self):
        result = run_replay([self.session(61, 106), self.session(62, 90)], self.fees, slippage=0)
        ledger = result.ledger
        self.assertEqual(len(ledger.curve), 480)
        self.assertEqual([(fill['at'], fill['side']) for fill in ledger.fills],
                         [(pd.Timestamp('2026-01-05 09:46'), 'buy'),
                          (pd.Timestamp('2026-01-06 09:31'), 'sell')])
        self.assertEqual(ledger.holdings, {})
        buy = ledger.fills[0]
        sell = ledger.fills[1]
        self.assertAlmostEqual(ledger.cash, 150000 - buy['gross'] - buy['fees'] + sell['gross'] - sell['fees'])
        self.assertEqual(ledger.curve[15]['cash'], 150000)
        self.assertLess(ledger.curve[16]['cash'], 150000)

    def test_market_gate_ablation_uses_same_inputs_and_actual_cash_ledger(self):
        day = self.session(61, 106)
        flat_index = pd.Series(100., index=self.calendar)
        day = replace(day, index_closes=flat_index)
        gated = run_replay([day], self.fees, slippage=0)
        baseline = run_replay([day], self.fees, slippage=0, use_market_gate=False)
        self.assertEqual(gated.ledger.fills, [])
        self.assertEqual(gated.ledger.curve[-1]['equity'], 150000)
        self.assertEqual(len(baseline.ledger.fills), 1)

    def test_missing_held_minute_session_fails_instead_of_skipping_loss(self):
        second = self.session(62, 90)
        broken = replace(second, minutes={'A': second.minutes['A'].drop(second.minutes['A'].index[3])})
        with self.assertRaisesRegex(ValueError, 'Minute audit failed'):
            run_replay([self.session(61, 106), broken], self.fees)

    def test_exchange_session_cannot_be_omitted(self):
        with self.assertRaisesRegex(ValueError, 'omit intervening'):
            run_replay([self.session(61, 106), self.session(63, 90)], self.fees)

    def test_stock_cannot_be_mislabelled_etf_to_bypass_phase_validation(self):
        day = self.session(61, 106)
        day.raw_daily_for_session['A']['asset_kind'] = 'etf'
        with self.assertRaisesRegex(ValueError, 'classification differs'):
            run_replay([day], self.fees)

    def test_cash_distribution_integrates_without_false_stop_or_equity_jump(self):
        event = Distribution('fixture', 'A', pd.Timestamp('2026-01-02 12:00'),
            date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), 1., 0.,
            date(2026, 1, 6), date(2026, 1, 6), 105 / 106,
            'synthetic fixture', 'synthetic net-cash model')
        result = run_replay([self.session(61, 106), self.session(62, 105), self.session(63, 105)],
                            self.fees, [event], slippage=0)
        ledger = result.ledger
        self.assertEqual(len(ledger.fills), 1)
        self.assertEqual(len(ledger.curve), 720)
        self.assertAlmostEqual(ledger.curve[239]['equity'], ledger.curve[240]['equity'])
        self.assertAlmostEqual(ledger.curve[479]['equity'], ledger.curve[480]['equity'])
        self.assertEqual(ledger.cash_receivable, 0)
        self.assertGreater(ledger.curve[480]['cash'], ledger.curve[479]['cash'])


if __name__ == '__main__':
    unittest.main()
