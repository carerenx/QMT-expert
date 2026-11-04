import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_subscription_manager import (
    QuoteSourceAdapter,
    QuoteSubscriptionManager,
)
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers


class FakeQuoteSource(QuoteSourceAdapter):
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


class FakeMarketData:
    def get_ticks(self, codes):
        return {}


class FakePositionProvider:
    def get_positions(self, account_id):
        return {}

    def get_asset(self, account_id):
        return None


def _handlers(with_manager=True):
    source = FakeQuoteSource()
    manager = QuoteSubscriptionManager(source) if with_manager else None
    handlers = BigQmtRpcHandlers(
        account_id="acct",
        market_data=FakeMarketData(),
        position_provider=FakePositionProvider(),
        quote_subscription_manager=manager,
    )
    return handlers, source


class WholeQuoteRpcHandlersTest(unittest.TestCase):
    def test_subscribe_whole_quote_allowed_and_creates_subscription(self):
        handlers, source = _handlers()
        result = handlers.handle(
            "subscribe_whole_quote",
            {"client_id": "c1", "sub_id": "s1", "codes": ["SH", "SZ"]},
        )
        self.assertEqual(len(source.subscriptions), 1)
        self.assertEqual(result["combo_key"], "SH,SZ")
        self.assertIn("topic", result)

    def test_subscribe_whole_quote_idempotent_replay(self):
        handlers, source = _handlers()
        params = {"client_id": "c1", "sub_id": "s1", "codes": ["SH", "SZ"]}
        handlers.handle("subscribe_whole_quote", params)
        handlers.handle("subscribe_whole_quote", params)  # replay after recovery
        self.assertEqual(len(source.subscriptions), 1)

    def test_subscribe_whole_quote_requires_client_and_codes(self):
        handlers, _source = _handlers()
        with self.assertRaises(ValueError):
            handlers.handle("subscribe_whole_quote", {"sub_id": "s1", "codes": ["SH"]})
        with self.assertRaises(ValueError):
            handlers.handle("subscribe_whole_quote", {"client_id": "c1", "sub_id": "s1", "codes": []})

    def test_unsubscribe_whole_quote_tears_down_last_client(self):
        handlers, source = _handlers()
        handlers.handle("subscribe_whole_quote", {"client_id": "c1", "sub_id": "s1", "codes": ["SH"]})
        handlers.handle("unsubscribe_whole_quote", {"client_id": "c1", "sub_id": "s1"})
        self.assertEqual(len(source.subscriptions), 0)
        self.assertEqual(len(source.unsubscribed), 1)

    def test_quote_keepalive_ok(self):
        handlers, _source = _handlers()
        handlers.handle("subscribe_whole_quote", {"client_id": "c1", "sub_id": "s1", "codes": ["SH"]})
        result = handlers.handle("quote_keepalive", {"client_id": "c1", "sub_id": "s1"})
        self.assertEqual(result, {})

    def test_quote_methods_rejected_without_manager(self):
        handlers, _source = _handlers(with_manager=False)
        with self.assertRaises(RuntimeError):
            handlers.handle("subscribe_whole_quote", {"client_id": "c1", "sub_id": "s1", "codes": ["SH"]})
        with self.assertRaises(RuntimeError):
            handlers.handle("quote_keepalive", {"client_id": "c1", "sub_id": "s1"})

    def test_quote_methods_in_default_allowed_set(self):
        # The three methods must be reachable through the default whitelist (no
        # explicit allowed_methods passed), same as every other read method.
        handlers = BigQmtRpcHandlers(
            account_id="acct",
            market_data=FakeMarketData(),
            position_provider=FakePositionProvider(),
            quote_subscription_manager=QuoteSubscriptionManager(FakeQuoteSource()),
        )
        for method in ("subscribe_whole_quote", "unsubscribe_whole_quote", "quote_keepalive"):
            self.assertIn(method, handlers.allowed_methods)


if __name__ == "__main__":
    unittest.main()
