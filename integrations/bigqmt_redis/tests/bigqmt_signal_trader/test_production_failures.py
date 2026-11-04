# coding: utf-8
"""Unit tests that simulate real QMT failure modes (production edge cases).

The existing unit tests use happy-path mocks (FakeMarketData always returns
{"close": [10.0]}). These tests instead simulate the failure modes users hit
in production — so they are caught by the unit suite, not in production:

  - get_positions / get_asset return EMPTY when QMT context is unbound
  - query_orders returns [] because strategy_name filtering mismatched
  - get_market_data_ex(dividend_type='front') returns ALL-ZERO bars when the
    server has no raw data downloaded
  - order_stock returns -1 (submit failed) -> must surface on_order_error
  - get_trade_detail_data ORDER/DEAL returns empty in some contexts

Each test asserts the CODE behaves correctly for that failure (degrades /
self-heals / reports) — not merely that the call returns *something*.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.models import PositionSnapshot, AssetSnapshot
from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers
from bigqmt_signal_trader.adapters.order_dryrun import DryRunOrderGateway


# ---------------------------------------------------------------------------
# Fakes that simulate QMT failure modes
# ---------------------------------------------------------------------------

class _EmptyPositionProvider:
    """QMT context unbound -> POSITION/ACCOUNT queries return empty."""
    def get_positions(self, account_id):
        return {}

    def get_asset(self, account_id):
        return AssetSnapshot(account_id=account_id, cash=None, total_asset=None)


class _EmptyMarketData:
    """QMT returns all-zero bars for adjusted reads with no raw data."""
    def get_market_data_ex(self, **kwargs):
        import pandas as pd
        return {"600654.SH": pd.DataFrame({"stime": ["20240101", "20240102"], "close": [0.0, 0.0]})}


class _EmptyOrderGateway(DryRunOrderGateway):
    """get_trade_detail_data(ORDER/DEAL) returns empty in some QMT contexts."""
    def query_orders(self, account_id, strategy_name):
        return []

    def query_trades(self, account_id, strategy_name):
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ProductionFailureTest(unittest.TestCase):
    def _handlers(self, market_data=None, position_provider=None, order_gateway=None, allow_order_methods=False):
        return BigQmtRpcHandlers(
            account_id="acct",
            market_data=market_data or _EmptyMarketData(),
            position_provider=position_provider or _EmptyPositionProvider(),
            order_gateway=order_gateway or _EmptyOrderGateway(),
            allow_order_methods=allow_order_methods,
        )

    # 1. 持仓查询返回空（上下文未绑定）—— 不应崩溃，返回空 dict
    def test_get_positions_empty_is_graceful(self):
        handlers = self._handlers()
        result = handlers.handle("get_positions", {})
        # Empty dict is a valid graceful result, not an exception.
        self.assertEqual(result, {})

    def test_get_asset_empty_fields_are_none(self):
        handlers = self._handlers()
        result = handlers.handle("get_asset", {})
        # AssetSnapshot with None fields is valid (QMT context unbound).
        self.assertIsInstance(result, AssetSnapshot)
        self.assertIsNone(result.cash)

    # 2. 委托查询返回空（strategy_name 不匹配 / ORDER 上下文无数据）
    def test_query_orders_empty_is_graceful(self):
        handlers = self._handlers()
        result = handlers.handle("query_orders", {})
        self.assertEqual(result, [])

    def test_query_trades_empty_is_graceful(self):
        handlers = self._handlers()
        result = handlers.handle("query_trades", {})
        self.assertEqual(result, [])

    # 3. 复权全 0 —— 服务端返回全 0 时，结果应能被识别（客户端自愈逻辑在 compat 层）
    def test_front_market_data_all_zero_detectable(self):
        handlers = self._handlers()
        result = handlers.handle("get_market_data_ex", {
            "field_list": ["close"], "stock_list": ["600654.SH"],
            "period": "1d", "count": 2, "dividend_type": "front",
        })
        df = result["600654.SH"]
        closes = list(df["close"]) if hasattr(df, "columns") else df
        # The handler must NOT silently fix this — it returns the zeros so the
        # client self-heal logic (in xtquant_compat) can detect and retry.
        self.assertTrue(all(c == 0.0 for c in closes), "expected all-zero bars from unready server")

    # 4. 下单失败（order_stock 返回 -1）—— 必须能感知，不是静默成功
    def test_submit_order_negative_one_means_failed(self):
        class FailingGateway(DryRunOrderGateway):
            def submit(self, request):
                from bigqmt_signal_trader.models import OrderSubmitResult
                return OrderSubmitResult(status="REJECTED", user_order_id="-1", order_sys_id=None, message="submit failed")

        handlers = self._handlers(order_gateway=FailingGateway(), allow_order_methods=True)
        result = handlers.handle("submit_order", {
            "stock_code": "600654.SH", "action": "BUY", "volume": 100,
            "price": 3.0, "price_type": "LIMIT", "strategy_name": "test",
        })
        # The result must expose the failure, not look like a success.
        self.assertEqual(result.user_order_id, "-1")
        self.assertEqual(result.status, "REJECTED")

    # 5. 委托/成交查询空 + strategy_name 为空字符串时应返回全部（Issue 修复验证）
    def test_query_orders_empty_strategy_returns_all(self):
        class TwoStrategyGateway(DryRunOrderGateway):
            def query_orders(self, account_id, strategy_name):
                # Simulate: only "" (empty) returns all; specific name returns subset
                if strategy_name == "":
                    return ["order-A", "order-B", "order-C"]
                if strategy_name == "rpc_test":
                    return ["order-A"]
                return []

        handlers = self._handlers(order_gateway=TwoStrategyGateway())
        # Default (no strategy_name) should pass "" and get all 3
        all_orders = handlers.handle("query_orders", {})
        self.assertEqual(len(all_orders), 3)
        # Explicit filter
        filtered = handlers.handle("query_orders", {"strategy_name": "rpc_test"})
        self.assertEqual(len(filtered), 1)


if __name__ == "__main__":
    unittest.main()
