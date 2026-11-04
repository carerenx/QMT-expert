"""frozen_cash must survive the whole chain: QMT row -> AssetSnapshot -> RPC
serialization -> client CompatObject, plus the Redis cached-asset fallback.

Field names follow MiniQMT's XtAsset(account_id, cash, frozen_cash,
market_value, total_asset), where total_asset = cash + frozen_cash + market_value.
"""

import datetime as _dt
import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters import position_bigqmt
from bigqmt_signal_trader.adapters.position_bigqmt import BigQmtPositionProvider
from bigqmt_signal_trader.adapters.position_sync_redis import RedisPositionSyncSink
from bigqmt_signal_trader.models import AccountSnapshot, AssetSnapshot, PositionSnapshot
from bigqmt_signal_trader.redis_rpc import to_jsonable
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, StockAccount


class Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _provider(row):
    def query(account_id, account_type, detail_type):
        return [row] if detail_type in ("ACCOUNT", "ASSET") else []

    return BigQmtPositionProvider(query)


class AssetSnapshotModelTest(unittest.TestCase):
    def test_defaults_keep_existing_positional_callers_working(self):
        snapshot = AssetSnapshot("acct", 100.0, 1000.0)

        self.assertEqual(snapshot.cash, 100.0)
        self.assertEqual(snapshot.total_asset, 1000.0)
        self.assertIsNone(snapshot.frozen_cash)
        self.assertIsNone(snapshot.market_value)

    def test_carries_the_full_xtasset_field_set(self):
        snapshot = AssetSnapshot("acct", 100.0, 1000.0, frozen_cash=50.0, market_value=850.0)

        self.assertEqual(snapshot.frozen_cash, 50.0)
        self.assertEqual(snapshot.market_value, 850.0)


class QmtCollectionTest(unittest.TestCase):
    def setUp(self):
        position_bigqmt._missing_field_reported.clear()

    def test_reads_frozen_cash_from_the_account_row(self):
        provider = _provider(
            Row(m_dAvailable=100.0, m_dBalance=1000.0, m_dFrozenCash=50.0, m_dInstrumentValue=850.0)
        )

        asset = provider.get_asset("acct")

        self.assertEqual(asset.cash, 100.0)
        self.assertEqual(asset.frozen_cash, 50.0)
        self.assertEqual(asset.market_value, 850.0)
        self.assertEqual(asset.total_asset, 1000.0)

    def test_accepts_alternate_broker_spellings(self):
        for field in ("m_dFrozenCash", "m_dFrozen", "m_dFrozenBalance", "frozen_cash"):
            provider = _provider(Row(m_dAvailable=100.0, m_dBalance=1000.0, **{field: 50.0}))

            self.assertEqual(provider.get_asset("acct").frozen_cash, 50.0, field)

    def test_derived_market_value_excludes_frozen_cash(self):
        """Without this, market value is overstated by the frozen amount."""
        provider = _provider(Row(m_dAvailable=100.0, m_dBalance=1000.0, m_dFrozenCash=50.0))

        self.assertEqual(provider.get_asset("acct").market_value, 850.0)

    def test_derivation_falls_back_when_frozen_is_absent(self):
        provider = _provider(Row(m_dAvailable=100.0, m_dBalance=1000.0))
        asset = provider.get_asset("acct")

        self.assertIsNone(asset.frozen_cash)
        self.assertEqual(asset.market_value, 900.0)  # legacy behaviour preserved

    def test_missing_frozen_field_reports_what_the_row_actually_has(self):
        """The ThinkTrader spelling is unverified offline; make it self-reporting
        instead of silently returning None forever."""
        import io
        import contextlib

        provider = _provider(Row(m_dAvailable=100.0, m_dBalance=1000.0, m_dWhateverElse=1.0))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            provider.get_asset("acct")
        output = buffer.getvalue()

        self.assertIn("frozen_cash not found", output)
        self.assertIn("m_dWhateverElse", output)  # names the real fields

    def test_missing_field_is_reported_once_not_per_call(self):
        import io
        import contextlib

        provider = _provider(Row(m_dAvailable=100.0, m_dBalance=1000.0))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            for _ in range(5):
                provider.get_asset("acct")

        self.assertEqual(buffer.getvalue().count("frozen_cash not found"), 1)

    def test_empty_rows_still_degrade_to_all_none(self):
        provider = BigQmtPositionProvider(lambda *args: [])

        asset = provider.get_asset("acct")

        self.assertIsNone(asset.cash)
        self.assertIsNone(asset.frozen_cash)


class RpcSerializationTest(unittest.TestCase):
    def test_to_jsonable_carries_frozen_cash_over_the_wire(self):
        snapshot = AssetSnapshot("acct", 100.0, 1000.0, frozen_cash=50.0, market_value=850.0)

        payload = to_jsonable(snapshot)

        self.assertEqual(payload["frozen_cash"], 50.0)
        self.assertEqual(payload["market_value"], 850.0)


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.streams = {}

    def set(self, key, value):
        self.kv[key] = value

    def setex(self, key, ttl_seconds, value):
        self.kv[key] = value

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.streams.setdefault(key, []).append(fields)
        return b"1-0"

    def publish(self, key, value):
        return 1


class PositionSyncTest(unittest.TestCase):
    def test_cached_snapshot_includes_frozen_cash(self):
        redis_client = FakeRedis()
        RedisPositionSyncSink(redis_client).publish(
            AccountSnapshot(
                account_id="acct",
                asset=AssetSnapshot("acct", 100.0, 1000.0, frozen_cash=50.0, market_value=850.0),
                positions={"600000.SH": PositionSnapshot("600000.SH", 100, 100, 10.0, "PF")},
                reason="test",
                updated_at=_dt.datetime(2026, 7, 1, 9, 31),
            )
        )

        payload = json.loads(redis_client.kv["bigqmt:positions:acct"])

        self.assertEqual(payload["asset"]["frozen_cash"], 50.0)
        self.assertEqual(payload["asset"]["market_value"], 850.0)


class ClientSurfaceTest(unittest.TestCase):
    """The reported AttributeError: asset.frozen_cash must simply exist."""

    def _trader(self, response):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client.call = lambda method, params=None, account_id=None, **kw: response
        return trader

    def test_frozen_cash_is_exposed(self):
        asset = self._trader(
            {"cash": 100.0, "total_asset": 1000.0, "frozen_cash": 50.0, "market_value": 850.0}
        ).query_stock_asset(StockAccount("acct"))

        self.assertEqual(asset.frozen_cash, 50.0)
        self.assertEqual(asset.cash, 100.0)
        self.assertEqual(asset.market_value, 850.0)
        self.assertEqual(asset.total_asset, 1000.0)

    def test_frozen_cash_defaults_to_zero_not_missing(self):
        """A server that predates this field must not resurrect the
        AttributeError, and callers do arithmetic on it."""
        asset = self._trader({"cash": 100.0, "total_asset": 1000.0}).query_stock_asset(
            StockAccount("acct")
        )

        self.assertEqual(asset.frozen_cash, 0.0)
        self.assertEqual(asset.cash + asset.frozen_cash, 100.0)

    def test_derived_market_value_excludes_frozen_cash(self):
        asset = self._trader(
            {"cash": 100.0, "total_asset": 1000.0, "frozen_cash": 50.0}
        ).query_stock_asset(StockAccount("acct"))

        self.assertEqual(asset.market_value, 850.0)

    def test_server_market_value_wins_over_derivation(self):
        asset = self._trader(
            {"cash": 100.0, "total_asset": 1000.0, "frozen_cash": 50.0, "market_value": 111.0}
        ).query_stock_asset(StockAccount("acct"))

        self.assertEqual(asset.market_value, 111.0)

    def test_components_reconstruct_total_asset(self):
        asset = self._trader(
            {"cash": 100.0, "total_asset": 1000.0, "frozen_cash": 50.0, "market_value": 850.0}
        ).query_stock_asset(StockAccount("acct"))

        self.assertAlmostEqual(
            asset.cash + asset.frozen_cash + asset.market_value, asset.total_asset
        )


if __name__ == "__main__":
    unittest.main()
