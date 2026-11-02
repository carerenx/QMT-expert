# -*- coding: utf-8 -*-
"""Point-in-time ranking and reservation integration; synthetic inputs only."""
import unittest
from dataclasses import replace
from datetime import date

import pandas as pd

from Stragety.RedisQMT.Trend100.core.candidates import (
    Candidate, SecurityState, rank_candidates,
)
from Stragety.RedisQMT.Trend100.core.portfolio import BuyIntent, PortfolioPlanner


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.sessions = pd.bdate_range('2025-01-01', periods=61)
        self.decision = self.sessions[-1] + pd.Timedelta(hours=15)

    def state(self, code, kind='stock', benchmark=''):
        return SecurityState(code, kind, self.decision.date(), self.decision,
                             300, False, False, False, 'mainboard',
                             'domestic_equity', benchmark)

    def history(self, target, amount=200_000_000, breakout=True):
        frame = pd.DataFrame({'open': 100., 'high': 101., 'low': 99.,
                              'close': 100., 'volume': 1_000_000.,
                              'amount': float(amount)}, index=self.sessions)
        frame.loc[self.sessions[-1], ['close', 'high']] = [target, target + 1]
        frame.loc[self.sessions[-1], 'volume'] = 2_000_000 if breakout else 1_000_000
        return frame

    def test_decile_is_taken_before_breakout_not_after(self):
        states = [self.state(str(i)) for i in range(10)]
        daily = {str(i): self.history(104 + i, breakout=i != 9) for i in range(10)}
        self.assertEqual(rank_candidates(states, daily, self.sessions, self.decision), [])

    def test_classes_have_separate_deciles_and_etf_liquidity_dedup(self):
        states = [self.state('S'), self.state('EA', 'etf', 'index1'),
                  self.state('EB', 'etf', 'index1'), self.state('EC', 'etf', 'index2')]
        daily = {'S': self.history(120), 'EA': self.history(110, 400_000_000),
                 'EB': self.history(130, 200_000_000), 'EC': self.history(105)}
        result = rank_candidates(states, daily, self.sessions, self.decision)
        self.assertEqual([item.code for item in result], ['S', 'EA'])

    def test_missing_eligible_history_stops_instead_of_reducing_universe(self):
        state = self.state('A')
        with self.assertRaisesRegex(ValueError, 'Missing eligible'):
            rank_candidates([state], {}, self.sessions, self.decision)
        daily = {'A': self.history(110).drop(self.sessions[3])}
        with self.assertRaisesRegex(ValueError, 'Incomplete daily'):
            rank_candidates([state], daily, self.sessions, self.decision)
        self.assertEqual(rank_candidates([replace(state, st=True)], {},
                                         self.sessions, self.decision), [])

    def test_future_or_stale_metadata_and_intraday_decisions_rejected(self):
        state = self.state('A')
        for changed in (replace(state, known_at=self.decision + pd.Timedelta(days=1)),
                        replace(state, session=date(2024, 1, 1))):
            with self.assertRaisesRegex(ValueError, 'metadata'):
                rank_candidates([changed], {}, self.sessions, self.decision)
        with self.assertRaisesRegex(ValueError, 'completed close'):
            rank_candidates([], {}, self.sessions, self.decision - pd.Timedelta(hours=1))

    def test_future_daily_data_is_excluded_from_ranking(self):
        frame = self.history(110)
        before = rank_candidates([self.state('A')], {'A': frame}, self.sessions, self.decision)
        frame.loc[self.sessions[-1] + pd.Timedelta(days=1)] = [9000] * 6
        after = rank_candidates([self.state('A')], {'A': frame}, self.sessions, self.decision)
        self.assertEqual(before, after)


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.at = pd.Timestamp('2026-01-06 09:45')
        self.previous = date(2026, 1, 5)
        self.frame = pd.DataFrame({'high': 101., 'close': 100., 'amount': 10_000_000.},
                                  index=pd.date_range('2026-01-06 09:30', periods=16, freq='min'))
        self.frame.loc[self.at, 'close'] = 102.
        self.planner = PortfolioPlanner()

    def candidate(self, code, kind='stock', momentum=0.2):
        return Candidate(code, kind, self.previous, momentum, 3., 100.)

    def plan(self, candidates, holdings=None, pending=None, cash=150000, equity=150000,
             allow=True):
        return self.planner.plan_buys(self.at, self.previous, candidates,
                                     {item.code: self.frame for item in candidates},
                                     holdings or {}, pending or [], equity, cash, allow)

    def test_four_slots_and_two_etf_cap_include_pending_orders(self):
        pending = [BuyIntent('E1', 'etf', self.at, 100, 10, 0.3)]
        holdings = {'E2': 'etf', 'S1': 'stock'}
        result = self.plan([self.candidate('E3', 'etf', 0.9), self.candidate('S2')],
                           holdings, pending)
        self.assertEqual([item.code for item in result], ['S2'])
        self.assertEqual(self.plan([self.candidate('S3')], holdings, pending + result), [])

    def test_rank_order_allocates_last_slot_and_ties_use_code(self):
        holdings = {'H1': 'stock', 'H2': 'stock', 'H3': 'stock'}
        result = self.plan([self.candidate('Z', momentum=0.1),
                            self.candidate('B'), self.candidate('A')], holdings)
        self.assertEqual([item.code for item in result], ['A'])

    def test_pending_reservations_prevent_double_spending(self):
        pending = [BuyIntent('P', 'stock', self.at, 100, 100, 3)]
        self.assertEqual(self.plan([self.candidate('A')], pending=pending, cash=10005), [])
        self.planner = PortfolioPlanner()
        result = self.plan([self.candidate('A'), self.candidate('B')], cash=15000)
        self.assertEqual(len(result), 1)
        self.assertLessEqual(sum(item.reserved_cash for item in result), 15000)

    def test_failed_attempt_is_not_retried_and_existing_position_not_added(self):
        self.assertEqual(len(self.plan([self.candidate('A')])), 1)
        self.assertEqual(self.plan([self.candidate('A')]), [])
        self.assertEqual(self.plan([self.candidate('B')], {'B': 'stock'}), [])

    def test_unfilled_exits_keep_slots_until_confirmed_fill(self):
        holdings = {str(i): 'stock' for i in range(4)}
        self.assertEqual(self.plan([self.candidate('A')], holdings), [])
        del holdings['0']
        self.assertEqual(len(self.plan([self.candidate('A')], holdings)), 1)

    def test_market_gate_and_drawdown_halt_block_entries(self):
        self.assertEqual(self.plan([self.candidate('A')], allow=False), [])
        self.assertEqual(self.plan([self.candidate('A')], equity=120000), [])
        self.assertEqual(self.plan([self.candidate('A')], equity=180000), [])

    def test_stale_candidates_rejected(self):
        stale = replace(self.candidate('A'), session=date(2026, 1, 2))
        with self.assertRaisesRegex(ValueError, 'preceding exchange session'):
            self.plan([stale])


if __name__ == '__main__':
    unittest.main()
