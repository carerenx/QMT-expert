import unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime
import tempfile
from pathlib import Path
from output.analysis.compare_v51_v39_minute import load_strategy

s=load_strategy('v52')


class CycleRunnerTests(unittest.TestCase):
    def setUp(self):
        self.p=s.PortfolioRunner(False)
        self.a=s.StrategyRunner(self.p,'601869.SH',lane=0)
        self.b=s.StrategyRunner(self.p,'601869.SH',lane=1)
        self.p.runners={'601869.SH':self.a,'601869.SH#1':self.b}
        for runner in (self.a,self.b):
            runner._init_state()
            runner.baseline_shares=800
            runner.st.update(initialized=True,trade_date=s.datetime.now().strftime('%Y%m%d'),
                            daily_signal={'atr_pct':.05},buyback_target=68.5)

    def cycle(self,runner,direction='SHORT',quantity=100):
        cycle=s.Cycle(str(runner.lane),runner.lane,direction,'20260910','2026-09-10T10:00:00',.05)
        cycle.fill(runner.lane,quantity,70,True,5)
        label='REV-T sell' if direction=='SHORT' else 'FWD-T buy'
        cycle.label=label
        runner.execution_book.record(runner.lane,label,-quantity if direction=='SHORT' else quantity,70)
        runner.st['short_legs' if direction=='SHORT' else 'long_legs']=[(70,quantity)]
        runner.cycle=cycle
        return cycle

    def test_same_stock_capacity_no_netting(self):
        self.cycle(self.a)
        with patch.object(s.ExecutionRunner,'_new_t_shares',return_value=1000):
            self.assertEqual(self.b._new_t_shares(70,'BUY'),300)
            self.assertEqual(self.a._new_t_shares(70,'SELL'),0)
            self.cycle(self.b,'LONG')
            self.assertEqual(self.b._new_t_shares(70,'BUY'),0)
        self.assertEqual(sum(r.cycle.quantity for r in (self.a,self.b)),200)

    def test_blocked_long_does_not_submit_or_count(self):
        before=dict(self.a.st)
        with patch.object(s.ExecutionRunner,'_submit_order') as submit:
            self.assertEqual(self.a._submit_order(100,70,'FWD-T buy'),('SKIP',0))
            submit.assert_not_called()
        self.assertEqual(self.a.st,before)

    def test_exit_not_blocked_by_direction_gate(self):
        self.cycle(self.a,'LONG')
        self.a._submitted_order_id=12
        self.a._execution_price=71
        self.p.conn.trader=SimpleNamespace(query_stock_order=lambda *args:SimpleNamespace(fee=5))
        with patch.object(s.ExecutionRunner,'_submit_order',return_value=('FILLED',-100)) as submit:
            self.assertEqual(self.a._submit_order(-100,71,'FWD-T sell'),('FILLED',-100))
            submit.assert_called_once()
        self.assertIsNone(self.a.cycle)
        self.assertEqual(self.a.cycle_history[-1]['realized_gross'],100)

    def test_other_cycle_completion_does_not_change_old_target(self):
        self.cycle(self.a)
        self.cycle(self.b,'LONG')
        before=self.a.checkpoint_record()
        self.b._submitted_order_id=12
        self.b._execution_price=71
        self.p.conn.trader=SimpleNamespace(query_stock_order=lambda *args:SimpleNamespace(fee=5))
        with patch.object(s.ExecutionRunner,'_submit_order',return_value=('FILLED',-100)):
            self.b._submit_order(-100,71,'FWD-T sell')
        self.assertEqual(self.a.checkpoint_record(),before)

    def test_serial_exit_priority(self):
        self.cycle(self.b)
        self.p.tasks={'601869.SH':('flat',0),'601869.SH#1':('old',0)}
        with patch.object(self.a,'_rollover_cycle_day'),patch.object(self.b,'_rollover_cycle_day'):
            self.p._prepare_trading_day()
        self.assertEqual(list(self.p.tasks),['601869.SH#1','601869.SH'])

    def test_roundtrip_preserves_cycle_and_clears_observation(self):
        self.cycle(self.a)
        record=self.a.checkpoint_record()
        clone=s.StrategyRunner(self.p,self.a.stock_qmt)
        clone.restore_record(record)
        self.assertEqual(clone.cycle.record(),record['v52']['cycle'])
        self.assertEqual(clone.baseline_shares,800)
        self.assertEqual(clone.st['buyback_target'],68.5)
        self.assertFalse(clone.admission.allowed)

    def test_invalid_owner_blocks_exit(self):
        with self.assertRaisesRegex(RuntimeError,'no unique cycle'):
            self.a._submit_order(100,70,'REV-T buyback')

    def test_ladder_keeps_owner_and_obeys_absolute_cap(self):
        old=self.cycle(self.a)
        old.label='REV-T sell'
        self.a._ladder_context=True
        with patch.object(s.ExecutionRunner,'_new_t_shares',return_value=1000):
            self.assertEqual(self.a._new_t_shares(70,'SELL'),300)
            self.a.baseline_shares=200
            self.assertEqual(self.a._new_t_shares(70,'SELL'),0)
        self.assertIs(self.a.cycle,old)

    def test_invalid_tick_clears_direction_observation(self):
        for n in range(15): self.a.admission.on_minute(100000+n*60,100+n,99+n,.05)
        self.assertTrue(self.a.admission.allowed)
        self.a._minute_pending=(100900,116,115,.05)
        self.a._invalidate_strength('MISSING_TICK')
        self.assertFalse(self.a.admission.allowed)
        self.assertIsNone(self.a._minute_pending)

    def test_overnight_roll_preserves_exit_and_ownership(self):
        cycle=self.cycle(self.a)
        self.a.st.update(trade_date='20260910',fstate=s.STATE_SOLD,short_legs=[(70,100)],
                         trade_count_short=4,mom_trade_count=2)
        class NextDay:
            @staticmethod
            def now(): return datetime(2026,9,14,9,29)
        with patch.object(s,'datetime',NextDay),patch.object(self.a,'_refresh_position'):
            self.a._rollover_cycle_day()
        self.assertEqual(self.a.st['trade_date'],'20260914')
        self.assertEqual(self.a.st['buyback_target'],68.5)
        self.assertIs(self.a.cycle,cycle)
        self.assertEqual(self.a.st['trade_count_short'],0)
        self.assertEqual(self.a.st['mom_trade_count'],0)
        # Weekend dates are not fabricated by rollover.
        cycle.advance_verified(['20260910','20260911','20260914'])
        self.assertEqual(cycle.trading_days,['20260910','20260911','20260914'])

    def test_partial_fill_record_restores_without_duplication(self):
        cycle=self.cycle(self.a,'LONG',200)
        cycle.fill('partial-close',100,71,False,5)
        self.a.execution_book.record('partial-close','FWD-T sell',-100,71)
        self.a.st['long_legs']=[(70,100)]
        record=self.a.checkpoint_record()
        self.b.restore_record(record)
        self.assertEqual(self.b.cycle.quantity,100)
        self.assertFalse(self.b.cycle.fill('partial-close',100,71,False,5))

    def test_pending_checkpoint_is_retained_and_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'v52.json'
            s.write_checkpoint(path,dict(schema=1,account=str(s.ACCOUNT),inflight={'order_id':17},
                                         order_uncertain=False))
            before=path.read_bytes()
            with patch.object(s,'STATE_FILE',str(path)):
                with self.assertRaisesRegex(RuntimeError,'unresolved broker order'):
                    self.p.restore_checkpoint()
            self.assertEqual(path.read_bytes(),before)

    def test_same_stock_other_short_reserve_protected(self):
        self.cycle(self.a,'SHORT',100)
        self.cycle(self.b,'SHORT',100)
        with patch.object(self.a,'_snapshot_account',return_value={'cash':10000.,'price':70.}), \
             patch.object(self.p,'reserved_cash',return_value=14710.):
            # Releasing own reserve cannot release the other lane's reserve.
            self.assertLess(self.a._clamp_buy_shares(100,70.),100)

    def test_verified_suspension_preserves_target(self):
        for status in (16,17,20):
            self.a._update_intraday_average({'stockStatus':status})
            self.assertIn('suspension',self.a.paused_reason)
            self.assertEqual(self.a.st['buyback_target'],68.5)
            self.assertFalse(self.a.admission.allowed)

    def test_live_entry_rejected_before_portfolio_creation(self):
        with patch('sys.argv',['v52','--mode','live']),patch.object(s,'PortfolioRunner') as create:
            with self.assertRaisesRegex(RuntimeError,'live disabled'):
                s.main()
            create.assert_not_called()


if __name__=='__main__': unittest.main()
