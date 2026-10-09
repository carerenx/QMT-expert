import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock

from Stragety.RedisQMT.Common.redis_qmt import RedisQmtAdapter
from Stragety.RedisQMT.Common.signals import compute_signal
from Stragety.RedisQMT.Common.turning_points import TurningPointTracker
from Stragety.RedisQMT.DT.DT_v1 import StrategyRunner


class TurningPointTrackerTests(unittest.TestCase):
    def test_spike_pullback_emits_sell_at_threshold(self):
        tracker = TurningPointTracker("peak", 0.01, "SELL")
        tracker.arm(100.0)
        self.assertIsNone(tracker.update(105.0))
        self.assertIsNone(tracker.update(104.0))
        self.assertEqual(tracker.update(103.95), "SELL")

    def test_dip_rebound_emits_buy_and_rearm_resets_extreme(self):
        tracker = TurningPointTracker("dip", 0.01, "BUY")
        tracker.arm(100.0)
        self.assertIsNone(tracker.update(95.0))
        self.assertEqual(tracker.update(95.95), "BUY")
        tracker.arm(80.0)
        self.assertEqual(tracker.extreme_price, 80.0)
        self.assertIsNone(tracker.update(80.79))

    def test_invalid_price_is_ignored(self):
        tracker = TurningPointTracker("peak", 0.01, "SELL")
        tracker.arm(100.0)
        self.assertIsNone(tracker.update(0))
        self.assertEqual(tracker.extreme_price, 100.0)


class FakeObject(object):
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeClient(object):
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None, account_id=None):
        self.calls.append((method, payload, account_id))
        return {"pong": True, "account_id": account_id}


class FakeTrader(object):
    def __init__(self):
        self.client = FakeClient()
        self.orders = []
        self.trades = []
        self.raise_order_timeout = False

    def query_stock_asset(self, account):
        return FakeObject(cash=50000.0)

    def query_stock_positions(self, account):
        return [FakeObject(stock_code="601869.SH", volume=300,
                           can_use_volume=200, avg_price=80.0)]

    def order_stock(self, account, symbol, order_type, shares, price_type,
                    price, strategy_name, remark):
        self.orders.append((symbol, order_type, shares, price_type, price, remark))
        if self.raise_order_timeout:
            raise TimeoutError("unknown submission result")
        return "OID-1"

    def query_stock_orders(self, account, cancelable_only=False,
                           strategy_name=""):
        return [FakeObject(order_id="OID-1", order_volume=100,
                           traded_volume=100, price=100.0, order_status=56,
                           order_remark="case-1")]

    def query_stock_trades(self, account, strategy_name=""):
        return list(self.trades)

    def cancel_order_stock(self, account, order_id):
        self.cancelled = order_id
        return 0


class FakeXtData(object):
    def get_full_tick(self, symbols):
        return {symbols[0]: {"lastPrice": 101.0, "askPrice": [101.1],
                             "bidPrice": [100.9], "lastClose": 100.0,
                             "open": 100.5}}


class RedisQmtAdapterTests(unittest.TestCase):
    def test_snapshot_normalizes_account_position_and_tick(self):
        adapter = RedisQmtAdapter(False, trader=FakeTrader(),
                                  xtdata=FakeXtData(), account_id="acct")
        snapshot = adapter.snapshot("601869.SH")
        self.assertEqual(snapshot["cash"], 50000.0)
        self.assertEqual(snapshot["shares"], 300)
        self.assertEqual(snapshot["sellable_shares"], 200)
        self.assertEqual(snapshot["price"], 101.0)

    def test_signal_mode_returns_virtual_fill_without_order(self):
        trader = FakeTrader()
        adapter = RedisQmtAdapter(False, trader=trader,
                                  xtdata=FakeXtData(), account_id="acct")
        result = adapter.submit("BUY", "601869.SH", 100, 101.1, "case-1")
        self.assertEqual(result["status"], "FILLED")
        self.assertTrue(result["virtual"])
        self.assertEqual(trader.orders, [])

    def test_live_mode_submits_whole_lot_and_confirms_fill(self):
        trader = FakeTrader()
        adapter = RedisQmtAdapter(True, trader=trader,
                                  xtdata=FakeXtData(), account_id="acct",
                                  poll_interval=0, fill_timeout=0.01)
        result = adapter.submit("SELL", "601869.SH", 100, 100.9, "case-1")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["filled_shares"], 100)
        self.assertEqual(len(trader.orders), 1)
        with self.assertRaises(ValueError):
            adapter.submit("BUY", "601869.SH", 50, 100.0, "bad-lot")
        with self.assertRaises(ValueError):
            adapter.submit("BUY", "601869.SH", 50, 100.0, "bad-lot",
                           allow_odd_lot=True)

    def test_custom_strategy_name_is_kept_by_adapter(self):
        adapter = RedisQmtAdapter(
            False, trader=FakeTrader(), xtdata=FakeXtData(),
            account_id="acct", strategy_name="RedisQMT_A144_v1")

        self.assertEqual(adapter.strategy_name, "RedisQMT_A144_v1")

    def test_timeout_cancels_before_returning_and_never_resubmits(self):
        trader = FakeTrader()
        trader.query_stock_orders = lambda *args, **kwargs: []
        adapter = RedisQmtAdapter(True, trader=trader,
                                  xtdata=FakeXtData(), account_id="acct",
                                  poll_interval=0, fill_timeout=0)
        result = adapter.submit("BUY", "601869.SH", 100, 101.1, "case-2")
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertEqual(trader.cancelled, "OID-1")
        self.assertEqual(len(trader.orders), 1)

    def test_rpc_timeout_recovers_existing_order_by_remark(self):
        trader = FakeTrader()
        trader.raise_order_timeout = True
        adapter = RedisQmtAdapter(True, trader=trader,
                                  xtdata=FakeXtData(), account_id="acct",
                                  poll_interval=0, fill_timeout=0.01)
        result = adapter.submit("BUY", "601869.SH", 100, 101.1, "case-1")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(len(trader.orders), 1)


class SignalTests(unittest.TestCase):
    def test_daily_signal_has_non_mom_reverse_and_forward_triggers(self):
        closes = [80.0 + index * 0.1 for index in range(60)]
        opens = [value - 0.2 for value in closes]
        highs = [value + 1.0 for value in closes]
        lows = [value - 1.0 for value in closes]
        volumes = [1000.0] * 60
        result = compute_signal(opens, highs, lows, closes, volumes,
                                today_open=90.0)
        self.assertIsNotNone(result)
        self.assertGreater(result["sell_trigger"], 90.0)
        self.assertEqual(result["buy_trigger"], 87.3)
        self.assertNotIn("mom", " ".join(result).lower())


class FakeAdapter(object):
    def __init__(self):
        self.submissions = []

    def submit(self, side, symbol, shares, price, remark, allow_odd_lot=False):
        self.submissions.append((side, symbol, shares, price, remark))
        return {"status": "FILLED", "filled_shares": shares,
                "average_price": price, "order_id": "virtual", "virtual": True}


class ResultAdapter(FakeAdapter):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def submit(self, side, symbol, shares, price, remark, allow_odd_lot=False):
        self.submissions.append((side, symbol, shares, price, remark))
        return self.results.pop(0)


class StrategyRunnerTests(unittest.TestCase):
    def test_reverse_t_completes_spike_pullback_and_dip_rebound(self):
        adapter = FakeAdapter()
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": True, "sell_trigger": 100.0,
                          "atr_pct": 0.04}, cash=50000.0,
                         sellable_shares=200)
        for price in (100.0, 102.0, 101.8, 99.0, 98.0, 98.2):
            runner.process_tick(price, "10:00:00", 100.0)
        self.assertEqual([item[0] for item in adapter.submissions], ["SELL", "BUY"])
        self.assertEqual(runner.state, "DONE")

    def test_forward_t_completes_dip_rebound_and_spike_pullback(self):
        adapter = FakeAdapter()
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": False, "do_long": True,
                          "buy_trigger": 97.0, "atr_pct": 0.04},
                         cash=50000.0, sellable_shares=200)
        for price in (97.0, 96.0, 96.2, 97.4, 98.0, 97.8):
            runner.process_tick(price, "10:00:00", 100.0)
        self.assertEqual([item[0] for item in adapter.submissions], ["BUY", "SELL"])
        self.assertEqual(runner.state, "DONE")

    def test_limit_guard_blocks_new_legs(self):
        adapter = FakeAdapter()
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": True, "sell_trigger": 100.0,
                          "atr_pct": 0.04}, cash=50000.0,
                         sellable_shares=200)
        runner.process_tick(109.5, "10:00:00", 100.0)
        runner.process_tick(109.0, "10:00:01", 100.0)
        self.assertEqual(adapter.submissions, [])
        self.assertEqual(runner.state, "IDLE")

    def test_partial_buyback_keeps_odd_remainder_for_manual_recovery(self):
        adapter = ResultAdapter([
            {"status": "PARTIAL", "filled_shares": 40,
             "average_price": 98.2, "order_id": "one", "virtual": False},
        ])
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": True, "sell_trigger": 100.0,
                          "atr_pct": 0.04}, cash=50000.0,
                         sellable_shares=200)
        runner.short_legs = [(101.8, 100)]
        runner.sell_fill_price = 101.8
        runner.state = "REV_DIPPING"
        runner.turning = TurningPointTracker("dip", 0.001, "BUY")
        runner.turning.arm(98.0)
        runner.process_tick(98.2, "10:00:00", 100.0)
        self.assertEqual(runner.short_legs, [(101.8, 60)])
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(runner.state, "RECOVERY_REQUIRED")

    def test_failed_force_close_does_not_enter_terminal_state(self):
        adapter = ResultAdapter([
            {"status": "TIMEOUT", "filled_shares": 0,
             "average_price": 0.0, "order_id": "one", "virtual": False},
        ])
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": True, "atr_pct": 0.04},
                         cash=50000.0, sellable_shares=100)
        runner.short_legs = [(101.0, 100)]
        runner.state = "REV_SOLD"
        with mock.patch("Stragety.RedisQMT.DT.DT_v1.config.ENABLE_FORCE_CLOSE", True):
            runner.process_tick(102.0, "14:57:00", 100.0)
        self.assertEqual(runner.state, "REV_SOLD")
        self.assertEqual(runner.short_legs, [(101.0, 100)])

    def test_cash_and_t1_capacity_block_forward_t(self):
        runner = StrategyRunner(adapter=FakeAdapter())
        signal = {"do_short": False, "do_long": True,
                  "buy_trigger": 100.0, "atr_pct": 0.04}
        runner.start_day(signal, cash=50.0, sellable_shares=200)
        runner.process_tick(99.0, "10:00:00", 100.0)
        self.assertEqual(runner.state, "IDLE")
        runner.start_day(signal, cash=50000.0, sellable_shares=0)
        runner.process_tick(99.0, "10:00:00", 100.0)
        self.assertEqual(runner.state, "IDLE")

    def test_forward_stop_loss_sells_open_leg(self):
        adapter = FakeAdapter()
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": False, "atr_pct": 0.04},
                         cash=40000.0, sellable_shares=100)
        runner.long_legs = [(100.0, 100)]
        runner.sellback_target = 101.2
        runner.state = "FWD_BOUGHT"
        runner.process_tick(98.5, "10:00:00", 100.0)
        self.assertEqual(adapter.submissions[0][0], "SELL")
        self.assertEqual(runner.state, "DONE")

    def test_reverse_ladder_can_open_second_confirmed_leg(self):
        adapter = FakeAdapter()
        runner = StrategyRunner(adapter=adapter)
        runner.start_day({"do_short": True, "sell_trigger": 100.0,
                          "atr_pct": 0.04}, cash=50000.0,
                         sellable_shares=200)
        for price in (100.0, 102.0, 101.8, 103.4, 104.0, 103.8):
            runner.process_tick(price, "10:00:00", 100.0)
        sells = [item for item in adapter.submissions if item[0] == "SELL"]
        self.assertEqual(len(sells), 2)
        self.assertEqual(sum(volume for _, volume in runner.short_legs), 200)


class CommandLineTests(unittest.TestCase):
    def test_strategy_can_be_started_by_file_path(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "Stragety" / "RedisQMT" / "DT" / "DT_v1.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"], cwd=root,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--mode", result.stdout)
        self.assertIn("--probe", result.stdout)

    def test_probe_is_read_only_and_calls_verify(self):
        adapter = mock.Mock()
        with mock.patch("Stragety.RedisQMT.DT.DT_v1.RedisQmtAdapter",
                        return_value=adapter):
            from Stragety.RedisQMT.DT.DT_v1 import main
            self.assertEqual(main(["--probe"]), 0)
        adapter.verify.assert_called_once_with()

    def test_registry_loads_strategy_only_with_supplied_adapter(self):
        from backtest.dayt_registry import load_redis_strategy
        adapter = FakeAdapter()
        runner = load_redis_strategy("dt_v1", adapter)
        self.assertIs(runner.adapter, adapter)


if __name__ == "__main__":
    unittest.main()
