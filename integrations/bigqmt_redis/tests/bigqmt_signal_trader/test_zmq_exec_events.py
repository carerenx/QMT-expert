"""Exec events must reach a zmq deployment, not just a Redis one (issue #76).

They used to be Redis-only on both ends: the server's publish path returned
early when it could not build a Redis client, and the client's listener only
ever subscribed to Redis channels. A zmq deployment therefore received no
on_stock_order / on_stock_trade / on_order_error at all -- silently, since the
listener just failed to connect and retried forever.

zmq deployments already run a push channel for whole-quote data, so exec events
ride that rather than opening a second socket.
"""

import os
import sys
import threading
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader import exec_events
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, XtQuantTraderCallback


class _Recorder(XtQuantTraderCallback):
    def __init__(self):
        self.seen = []
        self.lock = threading.Lock()

    def _add(self, *item):
        with self.lock:
            self.seen.append(item)

    def on_stock_order(self, order):
        self._add("order", order.stock_code)

    def on_stock_trade(self, trade):
        self._add("trade", trade.stock_code)

    def on_order_error(self, err):
        self._add("error", err.stock_code)

    def names(self):
        with self.lock:
            return [item[0] for item in self.seen]


class _FakeChannel(object):
    """Stands in for a QuotePushChannel: records publish(topic, data)."""

    def __init__(self):
        self.published = []

    def publish(self, topic, data):
        self.published.append((topic, data))


class _FakeRedis(object):
    """Stands in for a Redis client: exposes xadd, which is how the sink
    distinguishes the two."""

    def __init__(self):
        self.published = []

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.published.append(("xadd", key))
        return b"1-0"

    def publish(self, key, value):
        self.published.append(("publish", key))
        return 1


def _order_row(**overrides):
    attrs = dict(
        m_strInstrumentID="601398", m_strExchangeID="SH", m_nOffsetFlag=48,
        m_nVolumeTotalOriginal=100, m_nVolumeTraded=0, m_strOrderSysID="sys-1",
        m_strRemark="TAG-1", m_nOrderStatus=50, m_strTradeID="t-1",
        m_nVolume=100, m_dPrice=7.5,
    )
    attrs.update(overrides)
    return type("Row", (), attrs)()


class SinkRoutingTest(unittest.TestCase):
    """publish_exec_event picks the path from what the sink can do."""

    def test_push_channel_gets_a_topic_per_event_type(self):
        channel = _FakeChannel()
        for event in (exec_events.normalize_order_event(_order_row(), "acct"),
                      exec_events.normalize_trade_event(_order_row(), "acct"),
                      exec_events.normalize_order_error_event(_order_row(), "acct")):
            exec_events.publish_exec_event(channel, "acct", event)

        self.assertEqual([topic for topic, _ in channel.published],
                         ["exec:order", "exec:trade", "exec:order_error"])

    def test_redis_still_uses_the_per_account_channels(self):
        """Redis keeps streams for short replay; the push channel has none, so
        the Redis path must not be rerouted."""
        redis = _FakeRedis()
        exec_events.publish_exec_event(
            redis, "acct", exec_events.normalize_order_event(_order_row(), "acct"))

        self.assertIn(("xadd", exec_events.order_channel("acct")), redis.published)
        self.assertIn(("publish", exec_events.order_channel("acct")), redis.published)

    def test_every_event_type_has_a_topic(self):
        for event_type in ("order", "trade", "order_error", "cancel_error"):
            self.assertTrue(exec_events.exec_topic(event_type).startswith("exec:"))

    def test_unknown_event_type_falls_back_rather_than_dropping(self):
        self.assertEqual(exec_events.exec_topic("something-new"), "exec:order")


class DispatchInputTest(unittest.TestCase):
    """The push channel decodes msgpack/json itself and hands over a dict; the
    Redis path hands over raw bytes. Both must dispatch."""

    def _trader(self):
        trader = BigQmtXtTrader(account_id="acct")
        recorder = _Recorder()
        trader.register_callback(recorder)
        return trader, recorder

    def test_decoded_dict_is_dispatched(self):
        trader, rec = self._trader()
        trader._dispatch_event(
            {"event_type": "order", "account_id": "acct", "stock_code": "601398.SH",
             "action": "BUY", "order_sys_id": "sys-1"})

        self.assertEqual(rec.names(), ["order"])

    def test_raw_bytes_still_dispatched(self):
        import json

        trader, rec = self._trader()
        trader._dispatch_event(json.dumps(
            {"event_type": "trade", "account_id": "acct", "stock_code": "601398.SH",
             "action": "BUY", "trade_id": "t-1"}).encode("utf-8"))

        self.assertEqual(rec.names(), ["trade"])

    def test_push_callback_signature_matches_the_channel(self):
        """The channel calls on_msg(topic, data)."""
        trader, rec = self._trader()
        trader._on_push_exec_event(
            "exec:order",
            {"event_type": "order", "account_id": "acct", "stock_code": "601398.SH",
             "action": "BUY", "order_sys_id": "sys-1"})

        self.assertEqual(rec.names(), ["order"])


class ZmqRoundTripTest(unittest.TestCase):
    """A real PUB/SUB pair, since the point is that these actually travel."""

    ADDRESS = "tcp://127.0.0.1:15997"

    @classmethod
    def setUpClass(cls):
        try:
            import zmq  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pyzmq not installed")

    def test_all_three_event_types_reach_the_callback(self):
        from bigqmt_signal_trader.quote_push_channel import ZmqQuotePushChannel

        server = ZmqQuotePushChannel(bind_address=self.ADDRESS)
        server.start_publisher()
        trader = BigQmtXtTrader(account_id="acct")
        recorder = _Recorder()
        trader.register_callback(recorder)
        client = ZmqQuotePushChannel(connect_address=self.ADDRESS)
        client.start_subscriber(sorted(set(exec_events.EXEC_TOPICS.values())),
                                trader._on_push_exec_event)
        try:
            time.sleep(0.6)          # PUB/SUB needs a moment to connect
            for event in (exec_events.normalize_order_event(_order_row(), "acct"),
                          exec_events.normalize_trade_event(_order_row(), "acct"),
                          exec_events.normalize_order_error_event(_order_row(), "acct")):
                exec_events.publish_exec_event(server, "acct", event)

            deadline = time.time() + 5.0
            while time.time() < deadline and len(recorder.names()) < 3:
                time.sleep(0.05)

            self.assertEqual(sorted(recorder.names()), ["error", "order", "trade"])
        finally:
            client.stop()
            server.stop()


if __name__ == "__main__":
    unittest.main()
