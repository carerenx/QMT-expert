import os
import sys
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel
from bigqmt_signal_trader.quote_subscription_manager import (
    QuoteSourceAdapter,
    QuoteSubscriptionManager,
)


class FakeQuoteSource(QuoteSourceAdapter):
    """Captures the on_push callback so tests can fire big-QMT quote events."""

    def __init__(self):
        self.subscriptions = {}
        self.unsubscribed = []
        self._next_handle = 0

    def subscribe(self, codes, on_push):
        self._next_handle += 1
        handle = self._next_handle
        self.subscriptions[handle] = {"codes": list(codes), "on_push": on_push}
        return handle

    def unsubscribe(self, handle):
        self.unsubscribed.append(handle)
        self.subscriptions.pop(handle, None)

    def fire(self, handle, data):
        self.subscriptions[handle]["on_push"](data)


class RecordingPublisher:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, topic, data):
        with self._lock:
            self.calls.append((topic, data))


class OnPushWiringTest(unittest.TestCase):
    def test_on_push_forwards_to_publisher_with_combo_topic(self):
        source = FakeQuoteSource()
        publisher = RecordingPublisher()
        manager = QuoteSubscriptionManager(source, on_push_publisher=publisher)

        result = manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        handle = next(iter(source.subscriptions))
        source.fire(handle, {"000001.SZ": {"lastPrice": 10.5}})

        self.assertEqual(len(publisher.calls), 1)
        topic, data = publisher.calls[0]
        self.assertEqual(topic, result["topic"])
        self.assertEqual(data["000001.SZ"]["lastPrice"], 10.5)

    def test_on_push_ignored_without_publisher(self):
        source = FakeQuoteSource()
        manager = QuoteSubscriptionManager(source)  # no publisher wired
        manager.subscribe("clientA", "sub1", ["SH"])
        handle = next(iter(source.subscriptions))
        # Must not raise even though nothing consumes the push.
        source.fire(handle, {"000001.SZ": {"lastPrice": 10.5}})

    def test_on_push_concurrent_callbacks_are_serialized(self):
        source = FakeQuoteSource()
        publisher = RecordingPublisher()
        manager = QuoteSubscriptionManager(source, on_push_publisher=publisher)
        manager.subscribe("clientA", "sub1", ["SH"])
        handle = next(iter(source.subscriptions))

        threads = [
            threading.Thread(target=source.fire, args=(handle, {"000001.SZ": {"n": i}}))
            for i in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(publisher.calls), 20)
        self.assertTrue(all(topic == "SH" for topic, _ in publisher.calls))

    def test_push_stops_after_unsubscribe(self):
        source = FakeQuoteSource()
        publisher = RecordingPublisher()
        manager = QuoteSubscriptionManager(source, on_push_publisher=publisher)
        manager.subscribe("clientA", "sub1", ["SH"])
        handle = next(iter(source.subscriptions))
        manager.unsubscribe("clientA", "sub1")
        # Subscription torn down at the source; nothing left to fire.
        self.assertNotIn(handle, source.subscriptions)

    def test_concurrent_subscribe_unsubscribe_reap_and_push(self):
        # Hammer the manager from the RPC thread (subscribe/unsubscribe), the
        # scheduler thread (reap) and the quote thread (on_push) at once. The
        # shared combo/sub-index state must stay consistent: no KeyError, no
        # publish to a torn-down combo, and the source ends fully unsubscribed.
        source = FakeQuoteSource()
        publisher = RecordingPublisher()
        clock = [0.0]
        manager = QuoteSubscriptionManager(
            source, heartbeat_timeout_seconds=30.0, time_func=lambda: clock[0],
            on_push_publisher=publisher,
        )

        errors = []

        def churn(i):
            try:
                for n in range(30):
                    sub = "sub-%d-%d" % (i, n)
                    manager.subscribe("client-%d" % i, sub, ["SH"])
                    handle = next(iter(source.subscriptions), None)
                    if handle is not None:
                        source.fire(handle, {"x": n})
                    manager.unsubscribe("client-%d" % i, sub)
            except Exception as exc:  # noqa: BLE001 - surface any race as a failure
                errors.append(exc)

        def reap():
            try:
                for _ in range(30):
                    clock[0] += 1.0
                    manager.reap_expired()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        workers = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
        workers.append(threading.Thread(target=reap))
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(source.subscriptions), 0)


class OnPushToRedisChannelTest(unittest.TestCase):
    def test_manager_wired_to_real_channel_publishes(self):
        class FakeRedis:
            def __init__(self):
                self.messages = {}

            def publish(self, channel, value):
                self.messages.setdefault(channel, []).append(value)
                return 1

        redis_client = FakeRedis()
        channel = RedisQuotePushChannel(redis_client, account_id="acct")
        channel.start_publisher()

        source = FakeQuoteSource()
        manager = QuoteSubscriptionManager(source, on_push_publisher=channel.publish)
        manager.subscribe("clientA", "sub1", ["SH"])
        handle = next(iter(source.subscriptions))
        source.fire(handle, {"000001.SZ": {"lastPrice": 10.5}})

        self.assertIn("bigqmt:quote_push:acct:SH", redis_client.messages)
        channel.stop()


if __name__ == "__main__":
    unittest.main()
