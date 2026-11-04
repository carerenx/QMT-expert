import os
import sys
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_subscription_manager import (
    QuoteSourceAdapter,
    QuoteSubscriptionManager,
)
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession


class FakeQuoteSource(QuoteSourceAdapter):
    def __init__(self):
        self.subscriptions = {}
        self.unsubscribed = []
        self._next = 0

    def subscribe(self, codes, on_push):
        self._next += 1
        handle = self._next
        self.subscriptions[handle] = {"codes": list(codes), "on_push": on_push}
        return handle

    def unsubscribe(self, handle):
        self.unsubscribed.append(handle)
        self.subscriptions.pop(handle, None)

    def fire(self, data_by_combo_codes):
        # Fire every live subscription's on_push with the given data.
        for handle, sub in list(self.subscriptions.items()):
            sub["on_push"](data_by_combo_codes)


class FakeMarketData:
    def get_ticks(self, codes):
        return {}


class FakePositionProvider:
    def get_positions(self, account_id):
        return {}

    def get_asset(self, account_id):
        return None


class InProcPushBus:
    """Stands in for the QuotePushChannel: server publishes, every client channel
    subscribed to the topic receives (topic, data)."""

    def __init__(self):
        self._subscribers = []  # list of _BusSubscriber

    def register(self, subscriber):
        self._subscribers.append(subscriber)

    def publish(self, topic, data):
        for sub in list(self._subscribers):
            sub._deliver(topic, data)


class ClientPushChannel:
    """Client-side push channel wired to the bus. Mirrors QuotePushChannel's
    subscriber surface used by WholeQuoteClientSession."""

    def __init__(self, bus):
        self.bus = bus
        self.topics = []
        self._on_msg = None
        bus.register(self)

    def start_subscriber(self, topics, on_msg):
        self.topics = list(topics)
        self._on_msg = on_msg

    def _deliver(self, topic, data):
        if self._on_msg is not None and topic in self.topics:
            self._on_msg(topic, data)

    def stop(self):
        pass


class WholeQuoteE2ETest(unittest.TestCase):
    def _server(self, heartbeat_timeout=30.0, clock=None):
        source = FakeQuoteSource()
        bus = InProcPushBus()
        manager = QuoteSubscriptionManager(
            source,
            heartbeat_timeout_seconds=heartbeat_timeout,
            time_func=clock,
            on_push_publisher=bus.publish,
        )
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            quote_subscription_manager=manager,
        )
        return source, bus, manager, handlers

    def _client(self, client_id, bus, handlers):
        def rpc_call(method, params):
            return handlers.handle(method, params)

        return WholeQuoteClientSession(
            rpc_call=rpc_call,
            push_channel=ClientPushChannel(bus),
            client_id=client_id,
            heartbeat_interval_seconds=0.05,
        )

    def test_two_clients_share_one_qmt_subscription_and_both_receive(self):
        source, bus, manager, handlers = self._server()
        client_a = self._client("clientA", bus, handlers)
        client_b = self._client("clientB", bus, handlers)
        got_a, got_b = [], []
        client_a.subscribe_whole_quote(["SH", "SZ"], callback=got_a.append)
        client_b.subscribe_whole_quote(["sz", "sh"], callback=got_b.append)

        # One shared big-QMT subscription for the same normalized combo.
        self.assertEqual(len(source.subscriptions), 1)
        source.fire({"000001.SZ": {"lastPrice": 10.5}})
        self.assertEqual(len(got_a), 1)
        self.assertEqual(len(got_b), 1)

    def test_one_unsubscribe_keeps_other_receiving(self):
        source, bus, manager, handlers = self._server()
        client_a = self._client("clientA", bus, handlers)
        client_b = self._client("clientB", bus, handlers)
        got_a, got_b = [], []
        sub_a = client_a.subscribe_whole_quote(["SH"], callback=got_a.append)
        client_b.subscribe_whole_quote(["SH"], callback=got_b.append)

        client_a.unsubscribe_quote(sub_a)
        self.assertEqual(len(source.subscriptions), 1)  # still alive for clientB
        source.fire({"x": 1})
        self.assertEqual(got_a, [])
        self.assertEqual(len(got_b), 1)

    def test_all_unsubscribe_tears_down_qmt_subscription(self):
        source, bus, manager, handlers = self._server()
        client_a = self._client("clientA", bus, handlers)
        client_b = self._client("clientB", bus, handlers)
        sub_a = client_a.subscribe_whole_quote(["SH"], callback=lambda d: None)
        sub_b = client_b.subscribe_whole_quote(["SH"], callback=lambda d: None)
        client_a.unsubscribe_quote(sub_a)
        client_b.unsubscribe_quote(sub_b)
        self.assertEqual(len(source.subscriptions), 0)
        self.assertEqual(len(source.unsubscribed), 1)

    def test_server_restart_client_replay_restores_push(self):
        source, bus, manager, handlers = self._server()
        client = self._client("clientA", bus, handlers)
        received = []
        client.subscribe_whole_quote(["SH"], callback=received.append)
        self.assertEqual(len(source.subscriptions), 1)

        # Server "restarts": a brand-new manager/source/handlers on the same bus.
        source2 = FakeQuoteSource()
        manager2 = QuoteSubscriptionManager(source2, on_push_publisher=bus.publish)
        handlers2 = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            quote_subscription_manager=manager2,
        )
        self.assertEqual(len(source2.subscriptions), 0)

        # Client detects the restart and replays its subscriptions.
        def rpc_call2(method, params):
            return handlers2.handle(method, params)

        client._rpc = rpc_call2
        client.replay_subscriptions()

        self.assertEqual(len(source2.subscriptions), 1)
        source2.fire({"000001.SZ": {"lastPrice": 11.0}})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["000001.SZ"]["lastPrice"], 11.0)

    def test_silent_client_reaped_and_qmt_subscription_torn_down(self):
        clock = [1000.0]
        source, bus, manager, handlers = self._server(clock=lambda: clock[0])
        client = self._client("clientA", bus, handlers)
        client.subscribe_whole_quote(["SH"], callback=lambda d: None)
        self.assertEqual(len(source.subscriptions), 1)

        # Client goes silent (no keepalive); clock advances past the timeout.
        clock[0] += 31.0
        manager.reap_expired()
        self.assertEqual(len(source.subscriptions), 0)
        self.assertEqual(len(source.unsubscribed), 1)


if __name__ == "__main__":
    unittest.main()
