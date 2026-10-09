"""Redis RPC adapter for external BigQMT strategies."""
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_SRC = PROJECT_ROOT / "integrations" / "bigqmt" / "src"
if str(BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(BRIDGE_SRC))

from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, load_client_config
from xtquant.xtconstant import FIX_PRICE, STOCK_BUY, STOCK_SELL

from . import config


def _value(source, *names, **kwargs):
    default = kwargs.get("default")
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


class RedisQmtAdapter(object):
    """Hide BigQMT Redis transport, account fields, and fill polling."""

    def __init__(self, live=False, trader=None, xtdata=None, account_id=None,
                 poll_interval=0.25, fill_timeout=config.FILL_TIMEOUT_SECONDS,
                 strategy_name=None):
        if trader is None or xtdata is None:
            client_config = load_client_config()
            account_id = str(account_id or client_config.get("account_id") or "")
            trader, xtdata = configure(account_id=account_id)
        self.account_id = str(account_id or config.ACCOUNT)
        self.trader = trader
        self.xtdata = xtdata
        self.account = StockAccount(self.account_id, "STOCK")
        self.live = bool(live)
        self.strategy_name = str(strategy_name or config.STRATEGY_NAME)
        self.poll_interval = float(poll_interval)
        self.fill_timeout = float(fill_timeout)

    def verify(self):
        pong = self.trader.client.call("ping", account_id=self.account_id)
        if (not isinstance(pong, dict) or not pong.get("pong") or
                str(pong.get("account_id")) != self.account_id):
            raise RuntimeError("BigQMT Redis bridge did not confirm the configured account")
        self.trader.query_stock_asset(self.account)
        self.trader.query_stock_positions(self.account)
        self.xtdata.get_full_tick([config.STOCK_QMT])
        return True

    def tick(self, symbol):
        raw = (self.xtdata.get_full_tick([symbol]) or {}).get(symbol) or {}
        ask = _value(raw, "askPrice", default=[]) or []
        bid = _value(raw, "bidPrice", default=[]) or []
        return {
            "price": float(_value(raw, "lastPrice", "last_price", default=0) or 0),
            "ask1": float(ask[0] if ask else 0),
            "bid1": float(bid[0] if bid else 0),
            "last_close": float(_value(raw, "lastClose", "last_close", default=0) or 0),
            "open": float(_value(raw, "open", "openPrice", default=0) or 0),
        }

    def history(self, symbol, count=config.HISTORY_BAR_COUNT):
        return self.xtdata.get_market_data_ex(
            field_list=["open", "high", "low", "close", "volume"],
            stock_list=[symbol], period="1d", count=int(count),
            dividend_type="front", fill_data=True, timeout_seconds=20,
        ).get(symbol)

    def snapshot(self, symbol):
        asset = self.trader.query_stock_asset(self.account)
        positions = self.trader.query_stock_positions(self.account) or []
        position = next((item for item in positions
                         if str(_value(item, "stock_code", "m_strStockCode", default="")) == symbol), None)
        tick = self.tick(symbol)
        return {
            "cash": float(_value(asset, "available_cash", "cash", "m_dAvailableCash", "m_dCash", default=0) or 0),
            "shares": int(_value(position, "volume", "m_nVolume", default=0) or 0),
            "sellable_shares": int(_value(position, "can_use_volume", "enable_amount", "m_nCanUseVolume", default=0) or 0),
            "cost": float(_value(position, "avg_price", "cost_price", "m_dAvgPrice", default=0) or 0),
            **tick,
        }

    def submit(self, side, symbol, shares, price, remark, allow_odd_lot=False):
        side = str(side).upper()
        shares = int(shares)
        price = float(price)
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        odd_lot_allowed = allow_odd_lot and side == "SELL"
        if shares <= 0 or (not odd_lot_allowed and shares % config.TRADE_LOT_SIZE):
            raise ValueError("shares must be a positive whole lot")
        if price <= 0:
            raise ValueError("price must be positive")
        if not self.live:
            return {"order_id": "virtual", "status": "FILLED",
                    "filled_shares": shares, "average_price": price,
                    "virtual": True}
        order_type = STOCK_BUY if side == "BUY" else STOCK_SELL
        try:
            order_id = self.trader.order_stock(
                self.account, symbol, order_type, shares, FIX_PRICE, price,
                self.strategy_name, remark)
        except TimeoutError:
            recovered = self._recover_timed_out_submission(remark, shares, price)
            if recovered is not None:
                return recovered
            return {"order_id": None, "status": "TIMEOUT",
                    "filled_shares": 0, "average_price": 0.0,
                    "virtual": False}
        if not order_id or order_id == -1:
            return {"order_id": order_id, "status": "REJECTED",
                    "filled_shares": 0, "average_price": 0.0, "virtual": False}
        return self._wait_for_fill(order_id, shares, price, remark)

    def _wait_for_fill(self, order_id, expected_shares, fallback_price, remark):
        deadline = time.time() + self.fill_timeout
        latest = None
        while True:
            orders = self.trader.query_stock_orders(
                self.account, cancelable_only=False,
                strategy_name=self.strategy_name) or []
            latest = next((item for item in orders
                           if str(_value(item, "order_id", "order_sys_id", default="")) == str(order_id)
                           or str(_value(item, "order_remark", default="")) == str(remark)), latest)
            filled = int(_value(latest, "traded_volume", "traded_amount", default=0) or 0)
            if filled >= expected_shares:
                return self._fill_result(order_id, "FILLED", filled, fallback_price, latest)
            if time.time() >= deadline:
                try:
                    self.trader.cancel_order_stock(self.account, order_id)
                except Exception:
                    pass
                trades = self.trader.query_stock_trades(
                    self.account, strategy_name=self.strategy_name) or []
                matching = [item for item in trades
                            if str(_value(item, "order_id", "order_sys_id", default="")) == str(order_id)
                            or str(_value(item, "order_remark", default="")) == str(remark)]
                trade_filled = sum(int(_value(item, "traded_volume", "volume", default=0) or 0)
                                   for item in matching)
                filled = max(filled, trade_filled)
                status = "PARTIAL" if filled > 0 else "TIMEOUT"
                return self._fill_result(order_id, status, filled, fallback_price, latest)
            time.sleep(self.poll_interval)

    def _fill_result(self, order_id, status, filled, fallback_price, order):
        average = float(_value(order, "traded_price", "average_price", "price",
                               default=fallback_price) or fallback_price)
        return {"order_id": order_id, "status": status,
                "filled_shares": int(filled), "average_price": average,
                "virtual": False}

    def _recover_timed_out_submission(self, remark, shares, fallback_price):
        orders = self.trader.query_stock_orders(
            self.account, cancelable_only=False,
            strategy_name=self.strategy_name) or []
        order = next((item for item in orders
                      if str(_value(item, "order_remark", default="")) == str(remark)), None)
        if order is not None:
            order_id = _value(order, "order_id", "order_sys_id", default=None)
            return self._wait_for_fill(order_id, shares, fallback_price, remark)
        trades = self.trader.query_stock_trades(
            self.account, strategy_name=self.strategy_name) or []
        matching = [item for item in trades
                    if str(_value(item, "order_remark", default="")) == str(remark)]
        filled = sum(int(_value(item, "traded_volume", "volume", default=0) or 0)
                     for item in matching)
        if not filled:
            return None
        amount = sum(float(_value(item, "traded_price", "price", default=fallback_price) or fallback_price) *
                     int(_value(item, "traded_volume", "volume", default=0) or 0)
                     for item in matching)
        status = "FILLED" if filled >= shares else "PARTIAL"
        return {"order_id": None, "status": status,
                "filled_shares": filled, "average_price": amount / filled,
                "virtual": False}
