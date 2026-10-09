import unittest
from output.analysis.compare_v51_v39_minute import Broker, Clock
import pandas as pd
from datetime import datetime


class MinuteReplayTests(unittest.TestCase):
    def setUp(self):
        Clock.current = datetime(2026, 9, 10, 9, 31)
        self.daily = pd.DataFrame(dict(open=[9.],high=[11.],low=[8.],close=[10.],volume=[100.],amount=[100000.]),index=['20260909'])
        self.bars = pd.DataFrame(dict(open=[10.,999.],high=[11.,1000.],low=[9.,998.],close=[10.,999.],volume=[100.,100.],amount=[100000.,9990000.]),index=['20260910093100','20260910093200'])
        self.broker = Broker(self.daily,self.bars,0)

    def test_no_future_tick_and_previous_complete_day_only(self):
        tick = self.broker.get_full_tick(['601869.SH'])['601869.SH']
        self.assertEqual(tick['high'],11.)
        self.assertEqual(tick['lastPrice'],10.)
        self.assertEqual(tick['lastClose'],10.)
        self.assertEqual(tick['pvolume'],10000.)
        self.assertEqual(self.broker.load_daily_snapshot(80)['last_complete_date'],'20260909')

    def test_t_plus_one_and_cash_conservation(self):
        b = self.broker
        b.label='REV-T sell'
        b.order(b.code,-200,'COMPETE',10.)
        b.label='REV-T buyback(NORMAL)'
        b.order(b.code,200,'FIX',10.)
        self.assertEqual(b.cash,100000.)
        self.assertEqual(b.position,200)
        self.assertEqual(b.sellable,0)
        b.label='REV-T sell'
        oid=b.order(b.code,-100,'COMPETE',10.)
        self.assertEqual(b.orders[oid].traded_volume,0)
        self.assertEqual(len(b.trades),2)
        self.assertEqual(b.closed,[0.])

    def test_limit_price_and_zero_volume(self):
        b=self.broker
        b.label='FWD-T buy'
        oid=b.order(b.code,100,'FIX',9.)
        self.assertEqual(b.orders[oid].traded_volume,0)
        self.bars.loc[self.bars.index[0],'volume']=0
        oid=b.order(b.code,100,'COMPETE',10.)
        self.assertEqual(b.orders[oid].traded_volume,0)


if __name__ == '__main__': unittest.main()
