import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.quote_subscription_manager import (
    ContextInfoQuoteSource,
    QuoteSourceAdapter,
    QuoteSubscriptionManager,
    combo_key,
)


class FakeContextInfo:
    """Mirrors big-QMT ContextInfo subscribe_whole_quote/unsubscribe_quote."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self._next = 0

    def subscribe_whole_quote(self, code_list, callback=None):
        self.calls.append(("subscribe", list(code_list)))
        if self.fail:
            return -1
        self._next += 1
        return self._next

    def unsubscribe_quote(self, sub_id):
        self.calls.append(("unsubscribe", sub_id))
        return 0


class ContextInfoQuoteSourceTest(unittest.TestCase):
    def test_subscribe_returns_handle_and_forwards_callback(self):
        context = FakeContextInfo()
        source = ContextInfoQuoteSource(context)
        received = []
        handle = source.subscribe(["SH", "SZ"], received.append)
        self.assertEqual(handle, 1)
        self.assertEqual(context.calls, [("subscribe", ["SH", "SZ"])])

    def test_subscribe_failure_raises(self):
        context = FakeContextInfo(fail=True)
        source = ContextInfoQuoteSource(context)
        with self.assertRaises(RuntimeError):
            source.subscribe(["SH"], lambda d: None)

    def test_unsubscribe_forwards_sub_id(self):
        context = FakeContextInfo()
        source = ContextInfoQuoteSource(context)
        handle = source.subscribe(["SH"], lambda d: None)
        source.unsubscribe(handle)
        self.assertEqual(context.calls[-1], ("unsubscribe", handle))



class FakeQuoteSource(QuoteSourceAdapter):
    """Records subscribe/unsubscribe calls against a fake big-QMT quote source."""

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


class ComboKeyTest(unittest.TestCase):
    def test_order_independent(self):
        self.assertEqual(combo_key(["SH", "SZ"]), combo_key(["SZ", "SH"]))

    def test_case_and_whitespace_normalized(self):
        self.assertEqual(combo_key([" sh ", "sz"]), combo_key(["SH", "SZ"]))

    def test_duplicates_collapse(self):
        self.assertEqual(combo_key(["SH", "SH", "SZ"]), combo_key(["SH", "SZ"]))

    def test_symbol_list(self):
        self.assertEqual(
            combo_key(["600000.SH", "000001.SZ"]),
            combo_key(["000001.SZ", "600000.SH"]),
        )

    def test_empty_entries_dropped(self):
        self.assertEqual(combo_key(["SH", "", None, "  "]), "SH")

    def test_empty_list(self):
        self.assertEqual(combo_key([]), "")


class QuoteSubscriptionManagerTest(unittest.TestCase):
    def setUp(self):
        self.source = FakeQuoteSource()
        self.manager = QuoteSubscriptionManager(
            self.source,
            heartbeat_timeout_seconds=30.0,
        )

    # -- first client creates the big-QMT subscription -----------------------
    def test_first_subscribe_creates_qmt_subscription(self):
        result = self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.assertEqual(len(self.source.subscriptions), 1)
        handle = next(iter(self.source.subscriptions))
        self.assertEqual(self.source.subscriptions[handle]["codes"], ["SH", "SZ"])
        self.assertEqual(result["combo_key"], "SH,SZ")
        self.assertIn("topic", result)

    def test_same_combo_different_order_shares_subscription(self):
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.manager.subscribe("clientB", "sub2", ["sz", "sh"])
        # Same normalized combo -> only one big-QMT subscription.
        self.assertEqual(len(self.source.subscriptions), 1)

    def test_different_combos_create_separate_subscriptions(self):
        self.manager.subscribe("clientA", "sub1", ["SH"])
        self.manager.subscribe("clientB", "sub2", ["SZ"])
        self.assertEqual(len(self.source.subscriptions), 2)

    def test_idempotent_replay_same_client_and_sub(self):
        # Replayed subscribe (recovery) must not create a second big-QMT subscription.
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.assertEqual(len(self.source.subscriptions), 1)

    def test_subscribe_response_carries_push_endpoint_when_configured(self):
        manager = QuoteSubscriptionManager(
            self.source, push_endpoint="tcp://127.0.0.1:15561"
        )
        result = manager.subscribe("clientA", "sub1", ["SH"])
        self.assertEqual(result["push_endpoint"], "tcp://127.0.0.1:15561")

    def test_subscribe_response_push_endpoint_defaults_empty(self):
        result = self.manager.subscribe("clientA", "sub1", ["SH"])
        self.assertEqual(result["push_endpoint"], "")

    # -- reference counting / unsubscribe ------------------------------------
    def test_unsubscribe_one_of_two_keeps_qmt_subscription(self):
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.manager.subscribe("clientB", "sub2", ["SH", "SZ"])
        self.manager.unsubscribe("clientA", "sub1")
        self.assertEqual(len(self.source.subscriptions), 1)
        self.assertEqual(self.source.unsubscribed, [])

    def test_unsubscribe_one_sub_of_same_client_keeps_combo_alive(self):
        """同一 client 两个 sub_id 订阅同一组合,退订一个后另一个仍在:
        服务端引用按 (client_id, sub_id) 粒度,不按 client 粒度。"""
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.manager.subscribe("clientA", "sub2", ["SH", "SZ"])
        self.manager.unsubscribe("clientA", "sub1")
        self.assertEqual(len(self.source.subscriptions), 1, "组合不应被拆掉")
        self.assertEqual(self.source.unsubscribed, [], "不应退订大 QMT 订阅")
        # 另一个 sub 还在:keepalive 应仍有效(不产生新订阅/退订)
        self.manager.keepalive("clientA", "sub2")
        self.assertEqual(len(self.source.subscriptions), 1)
        # 最后一个 sub 退订 -> 组合拆掉
        self.manager.unsubscribe("clientA", "sub2")
        self.assertEqual(len(self.source.subscriptions), 0)
        self.assertEqual(len(self.source.unsubscribed), 1)

    def test_unsubscribe_last_client_unsubscribes_qmt(self):
        self.manager.subscribe("clientA", "sub1", ["SH", "SZ"])
        self.manager.subscribe("clientB", "sub2", ["SH", "SZ"])
        self.manager.unsubscribe("clientA", "sub1")
        self.manager.unsubscribe("clientB", "sub2")
        self.assertEqual(len(self.source.subscriptions), 0)
        self.assertEqual(len(self.source.unsubscribed), 1)

    def test_unsubscribe_unknown_sub_is_noop(self):
        self.manager.subscribe("clientA", "sub1", ["SH"])
        # Should not raise, should not affect the live subscription.
        self.manager.unsubscribe("clientA", "no-such-sub")
        self.assertEqual(len(self.source.subscriptions), 1)

    # -- keepalive / reaper ----------------------------------------------------
    def test_keepalive_refreshes_last_seen(self):
        self.manager.subscribe("clientA", "sub1", ["SH"])
        self.manager.keepalive("clientA", "sub1")
        # Still alive -> reaping at a fresh timestamp removes nothing.
        self.assertEqual(self.manager.reap_expired(now=self.manager._now()), 0)
        self.assertEqual(len(self.source.subscriptions), 1)

    def test_reap_removes_timed_out_client_and_unsubscribes(self):
        self.manager.subscribe("clientA", "sub1", ["SH"])
        now = self.manager._now()
        # Advance past the 30s timeout with no keepalive.
        self.assertEqual(self.manager.reap_expired(now=now + 31.0), 1)
        self.assertEqual(len(self.source.subscriptions), 0)

    def test_reap_keeps_combo_alive_while_one_client_alive(self):
        clock = [1000.0]
        manager = QuoteSubscriptionManager(
            self.source, heartbeat_timeout_seconds=30.0, time_func=lambda: clock[0]
        )
        manager.subscribe("clientA", "sub1", ["SH"])
        manager.subscribe("clientB", "sub2", ["SH"])
        # clientB keepalives at +31 (fresh); clientA has been silent since subscribe.
        clock[0] = 1031.0
        manager.keepalive("clientB", "sub2")
        removed = manager.reap_expired()
        # clientA reaped, but combo still has clientB -> big-QMT subscription stays.
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.source.subscriptions), 1)

    def test_keepalive_unknown_sub_is_noop(self):
        # Unknown sub should not raise.
        self.manager.keepalive("clientA", "ghost")


if __name__ == "__main__":
    unittest.main()
