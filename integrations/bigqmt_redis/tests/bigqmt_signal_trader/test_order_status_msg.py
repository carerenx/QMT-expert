"""issue #60: the counter's reason for rejecting an order never reached callers.

status_msg and error_msg both came back empty, so a rejection like
"[COUNTER] 资金可用余额不足，尚需[4789.630]" surfaced as a failure with no cause.

Field names come from the official table in
docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md: m_strCancelInfo is 废单原因 (the
counter's text lands there), m_strErrorMsg is 状态信息.
"""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bigqmt_signal_trader.adapters.order_bigqmt import BigQmtOrderGateway, _status_message
from bigqmt_signal_trader.exec_events import (
    normalize_order_error_event,
    normalize_order_event,
)
from bigqmt_signal_trader.models import OrderSnapshot
from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader, StockAccount


COUNTER_MSG = "[COUNTER] 资金可用余额不足，尚需[4789.630]"


class Row(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _order_row(**overrides):
    base = dict(
        m_strOrderSysID="sys-1",
        m_strRemark="tag-1",
        m_strInstrumentID="601398",
        m_strExchangeID="SH",
        m_nOffsetFlag=48,
        m_nVolumeTotalOriginal=100,
        m_nVolumeTraded=0,
        m_nOrderStatus=57,  # 废单
        m_dLimitPrice=7.66,
        m_strStrategyName="s1",
    )
    base.update(overrides)
    return Row(**base)


class StatusMessageExtractionTest(unittest.TestCase):
    def test_reads_the_cancel_reason(self):
        self.assertEqual(
            _status_message(_order_row(m_strCancelInfo=COUNTER_MSG)), COUNTER_MSG)

    def test_cancel_reason_wins_over_the_generic_status(self):
        """废单原因 is the specific one; 状态信息 is often just a status word."""
        row = _order_row(m_strCancelInfo=COUNTER_MSG, m_strErrorMsg="失败")
        self.assertEqual(_status_message(row), COUNTER_MSG)

    def test_falls_back_to_the_status_field(self):
        self.assertEqual(_status_message(_order_row(m_strErrorMsg="废单")), "废单")

    def test_blank_fields_are_skipped(self):
        """An empty 废单原因 must not shadow a populated 状态信息."""
        row = _order_row(m_strCancelInfo="   ", m_strErrorMsg=COUNTER_MSG)
        self.assertEqual(_status_message(row), COUNTER_MSG)

    def test_no_message_yields_empty_string(self):
        self.assertEqual(_status_message(_order_row()), "")


class OrderSnapshotStatusMsgTest(unittest.TestCase):
    def test_defaults_keep_positional_callers_working(self):
        self.assertEqual(
            OrderSnapshot("sys", "tag", "601398.SH", "BUY", 100, 0, "50").status_msg, "")

    def test_query_orders_carries_the_reason(self):
        rows = [_order_row(m_strCancelInfo=COUNTER_MSG)]
        gateway = BigQmtOrderGateway(
            context_info=None,
            passorder_func=None,
            cancel_func=None,
            get_trade_detail_data_func=lambda a, t, d, s="": rows if d == "ORDER" else [],
        )

        self.assertEqual(gateway.query_orders("acct", "")[0].status_msg, COUNTER_MSG)


class OrderEventStatusMsgTest(unittest.TestCase):
    def test_order_event_carries_the_reason(self):
        event = normalize_order_event(_order_row(m_strCancelInfo=COUNTER_MSG), "acct")
        self.assertEqual(event["status_msg"], COUNTER_MSG)

    def test_order_error_event_reads_the_cancel_reason(self):
        """The counter's text is in m_strCancelInfo; reading only m_strErrorMsg
        is why error_msg was empty."""
        event = normalize_order_error_event(
            Row(m_strOrderSysID="sys-1", m_strInstrumentID="601398.SH",
                m_nErrorID=-1, m_strCancelInfo=COUNTER_MSG), "acct")

        self.assertEqual(event["error_msg"], COUNTER_MSG)

    def test_order_error_event_still_reads_the_plain_error_field(self):
        event = normalize_order_error_event(
            Row(m_strOrderSysID="sys-1", m_nErrorID=-1, m_strErrorMsg="rejected"), "acct")

        self.assertEqual(event["error_msg"], "rejected")


class ClientStatusMsgTest(unittest.TestCase):
    """The reported symptom: XtOrder.status_msg is empty."""

    def _orders(self, payload):
        trader = BigQmtXtTrader(account_id="acct")
        trader.client.call = lambda method, params=None, account_id=None, **kw: payload
        return trader.query_stock_orders(StockAccount("acct"))

    def test_status_msg_is_exposed(self):
        orders = self._orders([{
            "stock_code": "601398.SH", "action": "BUY", "order_sys_id": "sys-1",
            "volume": 100, "status": 57, "status_msg": COUNTER_MSG,
        }])

        self.assertEqual(orders[0].status_msg, COUNTER_MSG)

    def test_missing_status_msg_defaults_to_empty(self):
        """A server predating this field must not raise AttributeError."""
        orders = self._orders([{
            "stock_code": "601398.SH", "action": "BUY", "order_sys_id": "sys-1",
            "volume": 100,
        }])

        self.assertEqual(orders[0].status_msg, "")


if __name__ == "__main__":
    unittest.main()
