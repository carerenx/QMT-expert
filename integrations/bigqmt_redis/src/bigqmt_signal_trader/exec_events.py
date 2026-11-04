"""Real-time order/trade (execution) event push over Redis.

Big QMT fires ``order_callback(ContextInfo, orderInfo)`` and
``deal_callback(ContextInfo, dealInfo)`` inside the strategy process. We normalize
the QMT order/deal object (ThinkTrader ``m_*`` fields) into a plain dict and
publish it to a Redis channel, so clients receive ``on_stock_order`` /
``on_stock_trade`` callbacks in real time (MiniQMT style) instead of polling.

Channels (also used as capped streams for short replay, xadd + publish):
- ``bigqmt:order_events:{account_id}``
- ``bigqmt:trade_events:{account_id}``

The normalized field names match ``BigQmtXtTrader._order_from_dict`` /
``_trade_from_dict`` so the client can shape them straight into MiniQMT objects.
"""

import json
import time


ORDER_CHANNEL_TEMPLATE = "bigqmt:order_events:{account_id}"
TRADE_CHANNEL_TEMPLATE = "bigqmt:trade_events:{account_id}"
ORDER_ERROR_CHANNEL_TEMPLATE = "bigqmt:order_error_events:{account_id}"
CANCEL_ERROR_CHANNEL_TEMPLATE = "bigqmt:cancel_error_events:{account_id}"
ORDER_IDENTITY_KEY_TEMPLATE = "bigqmt:order_identity:{account_id}:{user_order_id}"

EVENT_ORDER = "order"
EVENT_TRADE = "trade"
EVENT_ORDER_ERROR = "order_error"
EVENT_CANCEL_ERROR = "cancel_error"

# ThinkTrader enum_EEntrustBS (买卖方向, the m_nDirection field), universal across
# 股票/期货/期权. Ref: https://dict.thinktrader.net/innerApi/enum_constants.html
ENTRUST_BUY = 48         # 买入 / 多
ENTRUST_SELL = 49        # 卖出 / 空
ENTRUST_PLEDGE_IN = 81   # 质押入库
ENTRUST_PLEDGE_OUT = 66  # 质押出库

# enum_EEntrustBS (买卖方向, the m_nDirection field), per QMT enum docs.
# 48=买, 49=卖.  Universal across 股票/期货/期权.
#
# Real-world findings from live COrderDetail/CDealDetail callbacks
# (diagnosed via exec_events_debug_raw_fields=True, 2026-07-29):
#   QMT returns m_nDirection=48 **unconditionally** — even for sell orders.
#   m_nOffsetFlag correctly reflects direction (48=买, 49=卖 for stocks).
#   m_nOpType correctly reflects direction (23=买, 24=卖) on orders.
#   query_orders uses m_nOffsetFlag and works correctly in production.
#
# Therefore _extract_direction uses an arbitration chain:
#   Preferred: m_nOffsetFlag (most reliable in live callbacks, matches query_orders)
#   Fallback:  m_nDirection (traditional EEntrustBS; can be stuck at 48 in calls)
#   Arbiter:   when direction≠offset (futures: sell+open=49+48),
#              consult m_nOpType (23/24) to resolve the conflict; for trades
#              (no m_nOpType) trust m_nOffsetFlag (QMT docs confirm stock
#              direction=offset).
#   Last:      order_type (MiniQMT STOCK_BUY=23 / STOCK_SELL=24) and plain text
# Unknown -> "" (the raw value is always preserved so callers can refine).
OFFSET_OPEN = 48
OFFSET_CLOSE = 49
OFFSET_CLOSE_TODAY = 51
OFFSET_CLOSE_YESTERDAY = 52

_BUY_DIRECTIONS = {ENTRUST_BUY, str(ENTRUST_BUY), OFFSET_OPEN, str(OFFSET_OPEN), 23, "23", "BUY", "buy", "B"}
_SELL_DIRECTIONS = {ENTRUST_SELL, str(ENTRUST_SELL), OFFSET_CLOSE, str(OFFSET_CLOSE), OFFSET_CLOSE_TODAY, str(OFFSET_CLOSE_TODAY), OFFSET_CLOSE_YESTERDAY, str(OFFSET_CLOSE_YESTERDAY), 24, "24", "SELL", "sell", "S"}


def order_channel(account_id):
    return ORDER_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def trade_channel(account_id):
    return TRADE_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def order_error_channel(account_id):
    return ORDER_ERROR_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def cancel_error_channel(account_id):
    return CANCEL_ERROR_CHANNEL_TEMPLATE.format(account_id=str(account_id or ""))


def order_identity_key(account_id, user_order_id):
    return ORDER_IDENTITY_KEY_TEMPLATE.format(
        account_id=str(account_id or ""),
        user_order_id=str(user_order_id or ""),
    )


def _attr(obj, names, default=None):
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _with_exchange_suffix(raw_code, obj):
    """Append the exchange suffix to a bare instrument code.

    Live order/deal callbacks carry ``m_strInstrumentID`` as the bare code
    ('600000') while the exchange sits in ``m_strExchangeID`` — the same shape
    ``get_trade_detail_data`` rows use. Native MiniQMT callback objects carry
    the full '600000.SH' form, so events must match it. Codes that already
    carry a suffix (or objects without exchange info) pass through unchanged.
    """
    text = str(raw_code or "")
    if not text or "." in text:
        return text
    exchange = str(
        _attr(obj, ["m_strExchangeID", "m_strMarketID", "exchange_id", "market"], "") or ""
    ).strip().upper()
    if exchange:
        return "%s.%s" % (text, exchange)
    return text


def _action_from_direction(direction):
    if direction in _BUY_DIRECTIONS:
        return "BUY"
    if direction in _SELL_DIRECTIONS:
        return "SELL"
    return ""


def date_time_seconds(raw_date, raw_time):
    """Combine a QMT date + time pair into Unix seconds; 0 when unavailable.

    Official docs (dict.thinktrader.net, data_structure) leave the format of
    m_strTradeDate/m_strTradeTime/m_strInsertDate/m_strInsertTime unspecified,
    so tolerate the shapes seen in practice: date '20260819' or '2026-08-19',
    time '093015', '09:30:15(.123)', or a full 'YYYY-MM-DD HH:MM:SS' carried
    in the time field alone. Numeric timestamps pass through (ms normalized).
    """
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        value = float(raw_time)
        if value > 1e11:  # ms epoch
            value /= 1000.0
        if value > 1e8:   # looks like an epoch, not HHMMSS
            return int(value)

    date_digits = "".join(ch for ch in str(raw_date or "") if ch.isdigit())
    time_digits = "".join(ch for ch in str(raw_time or "") if ch.isdigit())
    # Time field carrying its own date ('YYYYMMDDHHMMSS' or the dashed form).
    if len(time_digits) >= 14:
        date_digits, time_digits = time_digits[:8], time_digits[8:14]
    if not date_digits or len(date_digits) < 8:
        return 0
    time_digits = (time_digits + "000000")[:6]  # pad to HHMMSS, drop ms
    try:
        parsed = time.strptime(date_digits[:8] + time_digits, "%Y%m%d%H%M%S")
        return int(time.mktime(parsed))
    except ValueError:
        return 0


def _is_buy(val):
    v = int(val)
    return v in _BUY_DIRECTIONS


def _is_sell(val):
    v = int(val)
    return v in _SELL_DIRECTIONS


def _conflict_resolve(d_val, o_val, obj):
    """When m_nDirection and m_nOffsetFlag disagree, arbitrate via m_nOpType.

    Live diagnosis confirms:
      - Stock sell: direction=48(buy), offset=49(sell), op_type=24(sell) → sell
      - Futures sell+open: direction=49(sell), offset=48(open), op_type=24(sell) → sell
      - Futures buy+close: direction=48(buy), offset=49(close), op_type=23(buy) → buy

    Returns a resolved value, or None if no arbiter can decide.
    """
    op = _attr(obj, ["m_nOpType", "op_type", "order_type"])
    if op is not None:
        try:
            op_int = int(op)
            if op_int in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op_int in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
        except (TypeError, ValueError):
            if op in _BUY_DIRECTIONS:
                return d_val if _is_buy(d_val) else o_val if _is_buy(o_val) else op
            if op in _SELL_DIRECTIONS:
                return d_val if _is_sell(d_val) else o_val if _is_sell(o_val) else op
    # no arbiter — trust offset (QMT docs confirm stock direction=offset)
    return o_val


def _extract_direction(obj):
    """Extract buy/sell direction, matching query_orders' reliable logic.

    Priority chain (documented with live-diagnosis justification):
      1. m_nOffsetFlag         — most reliable in live callbacks (matches query_orders)
      2. m_nDirection           — traditional EEntrustBS (can be stuck at 48)
      3. Arbitration: when direction≠offset, consult m_nOpType (orders: 23/24)
         to resolve correctly for both stocks AND futures.
      4. m_nOpType / order_type — last resort fallback.

    The raw value is always returned (even pledge=81) so callers can inspect it;
    _action_from_direction maps only known buy/sell values, leaving others "".

    References
    ----------
    - Live diagnosis 2026-07-29 (COrderDetail/CDealDetail):
      m_nDirection=48 unconditionally, m_nOffsetFlag=48(buy)/49(sell) correct,
      m_nOpType=23(buy)/24(sell) correct (orders only).
    - QMT enum docs: enum_EEntrustBS (48=买,49=卖), enum_EOffset_Flag_Type
      (48=开仓,49=平仓). For stocks direction=offset; for futures they differ.
    - query_orders uses m_nOffsetFlag and works correctly in production.
    """
    offset = _attr(obj, ["m_nOffsetFlag", "offset_flag"])
    direction = _attr(obj, ["m_nDirection", "direction"])

    # 1. offset alone — use it directly (matches query_orders)
    if offset is not None and direction is None:
        try:
            o = int(offset)
            if o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS:
                return offset
        except (TypeError, ValueError):
            if offset in _BUY_DIRECTIONS or offset in _SELL_DIRECTIONS:
                return offset

    # 2. direction alone — use it
    if direction is not None and offset is None:
        try:
            d = int(direction)
            if d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS:
                return direction
            if d != 0:
                return direction
        except (TypeError, ValueError):
            if direction in _BUY_DIRECTIONS or direction in _SELL_DIRECTIONS:
                return direction
            return direction

    # 3. both present
    if direction is not None and offset is not None:
        try:
            d = int(direction)
            o = int(offset)
            d_valid = (d in _BUY_DIRECTIONS or d in _SELL_DIRECTIONS)
            o_valid = (o in _BUY_DIRECTIONS or o in _SELL_DIRECTIONS)

            if d_valid and o_valid:
                if d == o:
                    return direction  # agree → use either
                # disagree → arbitrate via m_nOpType
                return _conflict_resolve(d, o, obj)

            if d_valid and not o_valid:
                return direction
            if o_valid and not d_valid:
                return offset
            # neither valid — fall through
        except (TypeError, ValueError):
            pass

    # 4. last resort: m_nOpType / order_type
    return _attr(obj, ["m_nOpType", "op_type", "order_type"])


# Fields we care about when diagnosing a direction misread. Anything starting
# with "m_" is captured automatically; these are the MiniQMT-style names that
# do not match that prefix.
_RAW_SNAPSHOT_EXTRA_FIELDS = (
    "stock_code",
    "order_type",
    "op_type",
    "direction",
    "offset_flag",
    "order_status",
    "order_volume",
    "traded_volume",
    "price",
    "order_id",
    "order_sysid",
    "order_sys_id",
    "trade_id",
    "traded_price",
    "traded_id",
    "strategy_name",
    "strategyName",
    "user_order_id",
    "order_remark",
    "remark",
)


def raw_field_snapshot(obj, max_repr=120):
    """Capture every readable field of a live QMT callback object.

    Direction extraction relies on understanding what ``m_nDirection``,
    ``m_nOffsetFlag`` and ``m_nOpType`` carry in live callbacks. This dumps
    every readable field so one live order settles the question.

    Returns ``{name: "<type> <value>"}``. Never raises: a callback that dies
    while being diagnosed would be worse than no diagnosis.
    """
    snapshot = {}
    try:
        if isinstance(obj, dict):
            names = list(obj.keys())
        else:
            names = [name for name in dir(obj) if name.startswith("m_")]
            names.extend(_RAW_SNAPSHOT_EXTRA_FIELDS)
    except Exception:
        return {"__error__": "dir() failed"}
    seen = set()
    for name in names:
        key = str(name)
        if key in seen or key.startswith("__"):
            continue
        seen.add(key)
        try:
            if isinstance(obj, dict):
                if key not in obj:
                    continue
                value = obj[key]
            else:
                if not hasattr(obj, key):
                    continue
                value = getattr(obj, key)
            if callable(value):
                continue
            text = repr(value)
            if len(text) > max_repr:
                text = text[:max_repr] + "..."
            snapshot[key] = "%s %s" % (type(value).__name__, text)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break callbacks
            snapshot[key] = "<unreadable: %s>" % exc.__class__.__name__
    return snapshot


def format_raw_snapshot(kind, obj):
    """One-line, GBK-safe rendering of :func:`raw_field_snapshot` for the QMT panel."""
    snapshot = raw_field_snapshot(obj)
    parts = ["%s=%s" % (name, snapshot[name]) for name in sorted(snapshot)]
    return "[bigqmt_exec_raw] %s type=%s %s" % (
        kind,
        type(obj).__name__,
        " | ".join(parts) or "<no fields>",
    )


def normalize_order_event(order, account_id=""):
    """Build a JSON-able order event dict from a Big QMT orderInfo object."""
    direction = _extract_direction(order)
    return {
        "event_type": EVENT_ORDER,
        "account_id": str(_attr(order, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _with_exchange_suffix(
            _attr(order, ["m_strInstrumentID", "stock_code", "m_strInstrument"], ""), order
        ),
        "order_sys_id": str(_attr(order, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        # MiniQMT XtOrder.order_volume is the ORIGINAL ordered volume; the
        # remaining volume (m_nVolumeTotal) drops to 0 on the filled push.
        "order_volume": _attr(
            order, ["m_nVolumeTotalOriginal", "m_nVolumeTotal", "order_volume", "volume"]
        ),
        "traded_volume": _attr(order, ["m_nVolumeTraded", "traded_volume"]),
        "price": _attr(order, ["m_dLimitPrice", "price", "limit_price"]),
        "traded_price": _attr(
            order, ["m_dTradedPrice", "traded_price", "avg_traded_price"]
        ),
        "status": _attr(order, ["m_nOrderStatus", "order_status", "status"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(order, ["m_nOffsetFlag", "offset_flag"]),
        "strategy_name": str(_attr(order, ["strategyName", "m_strStrategyName", "strategy_name"], "") or ""),
        "remark": str(_attr(order, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(order, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        "opt_name": str(_attr(order, ["m_strOptName", "opt_name"], "") or ""),
        # 委托状态描述。官方字段表: m_strCancelInfo=废单原因, m_strErrorMsg=状态信息。
        # 柜台的拒单理由 ("[COUNTER] 资金可用余额不足，尚需[...]") 只在这里,
        # 此前完全没有透传, 客户端看到的是一个没有原因的失败 (issue #60)。
        "status_msg": str(
            _attr(order, ["m_strCancelInfo", "m_strErrorMsg", "m_strStatusMsg",
                          "status_msg", "error_msg"], "") or ""
        ),
        # 官方 Order 字段 m_strInsertDate+m_strInsertTime -> 真实报单 Unix 秒。
        # 0 = 回调对象未携带 (老版本), 客户端会退回 created_at_ts。
        "order_time": date_time_seconds(
            _attr(order, ["m_strInsertDate", "m_strOrderDate", "insert_date", "order_date"]),
            _attr(order, ["m_strInsertTime", "m_strOrderTime", "insert_time", "order_time"]),
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def remember_order_identity(redis_client, account_id, user_order_id, strategy_name="", stock_code="", ttl_seconds=86400):
    user_order_id = str(user_order_id or "").strip()
    if not user_order_id or redis_client is None:
        return None
    payload = {
        "account_id": str(account_id or ""),
        "user_order_id": user_order_id,
        "strategy_name": str(strategy_name or ""),
        "stock_code": str(stock_code or ""),
        "created_at_ts": time.time(),
    }
    try:
        redis_client.setex(
            order_identity_key(account_id, user_order_id),
            int(ttl_seconds or 86400),
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    except Exception:
        pass
    return payload


def enrich_order_identity(redis_client, account_id, event):
    if redis_client is None or not isinstance(event, dict):
        return event
    user_order_id = str(event.get("user_order_id") or event.get("remark") or "").strip()
    if not user_order_id:
        return event
    try:
        raw = redis_client.get(order_identity_key(account_id, user_order_id))
    except Exception:
        raw = None
    if not raw:
        return event
    try:
        identity = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
    except Exception:
        return event
    if not event.get("strategy_name") and identity.get("strategy_name"):
        event["strategy_name"] = str(identity.get("strategy_name") or "")
    if not event.get("stock_code") and identity.get("stock_code"):
        event["stock_code"] = str(identity.get("stock_code") or "")
    return event


def normalize_trade_event(trade, account_id=""):
    """Build a JSON-able trade (成交) event dict from a Big QMT dealInfo object."""
    direction = _extract_direction(trade)
    return {
        "event_type": EVENT_TRADE,
        "account_id": str(_attr(trade, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": _with_exchange_suffix(
            _attr(trade, ["m_strInstrumentID", "stock_code"], ""), trade
        ),
        "order_sys_id": str(_attr(trade, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "trade_id": str(_attr(trade, ["m_strTradeID", "trade_id"], "") or ""),
        "volume": _attr(trade, ["m_nVolume", "volume", "traded_volume"]),
        "price": _attr(trade, ["m_dPrice", "price", "traded_price"]),
        "amount": _attr(trade, ["m_dTradeAmount", "amount"]),
        "commission": _attr(trade, ["m_dComssion", "m_dCommission", "commission"]),
        "direction": direction,
        "action": _action_from_direction(direction),
        "offset_flag": _attr(trade, ["m_nOffsetFlag", "offset_flag"]),
        "traded_at": str(_attr(trade, ["m_strTradeTime", "traded_at", "trade_time"], "") or ""),
        # 和委托事件一致带上 remark。客户端要在拿到 order_sys_id 之前就把成交
        # 关联到某笔异步委托 (issue #51)，而那时唯一已知的标识就是 remark。
        # QMT 的成交行不一定有这个字段，取不到则为空，客户端退回按
        # order_sys_id 关联。
        "remark": str(_attr(trade, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(trade, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        # 官方 Deal 字段 m_strTradeDate+m_strTradeTime -> 真实成交 Unix 秒。
        # 0 = 未携带, 客户端会退回 created_at_ts。
        "traded_time": date_time_seconds(
            _attr(trade, ["m_strTradeDate", "trade_date", "m_strDealDate"]),
            _attr(trade, ["m_strTradeTime", "trade_time", "traded_at"]),
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


# Push-channel topics, used when exec events travel over the quote push channel
# instead of Redis pub/sub. They mirror the Redis channel names minus the
# account (a zmq PUB socket is already per-account).
EXEC_TOPICS = {
    EVENT_ORDER: "exec:order",
    EVENT_TRADE: "exec:trade",
    "order_error": "exec:order_error",
    "cancel_error": "exec:cancel_error",
}


def exec_topic(event_type):
    return EXEC_TOPICS.get(str(event_type or ""), "exec:order")


def publish_exec_event(sink, account_id, event):
    """Publish one exec event through whichever sink the deployment has.

    ``sink`` is either a Redis client or a QuotePushChannel. Redis stays on the
    original per-account channels (streams + pub/sub, so short replay keeps
    working); a push channel gets one topic per event type.

    Exec events used to be Redis-only, which meant a zmq deployment silently
    received no order/trade callbacks at all (issue #76) -- the publish path
    just returned when no Redis client could be built.
    """
    event_type = str((event or {}).get("event_type") or EVENT_ORDER)
    if hasattr(sink, "publish") and not hasattr(sink, "xadd"):
        # QuotePushChannel: publish(topic, data).
        sink.publish(exec_topic(event_type), event)
        return event
    if event_type == EVENT_TRADE:
        return publish_trade_event(sink, account_id, event)
    if event_type == "order_error":
        return publish_order_error_event(sink, account_id, event)
    if event_type == "cancel_error":
        return publish_cancel_error_event(sink, account_id, event)
    return publish_order_event(sink, account_id, event)


def _publish(redis_client, channel, event, maxlen=2000):
    raw = json.dumps(event, ensure_ascii=False, default=str)
    try:
        redis_client.xadd(channel, {"payload": raw}, maxlen=maxlen, approximate=True)
    except Exception:
        pass
    redis_client.publish(channel, raw)
    return event


def publish_order_event(redis_client, account_id, event):
    return _publish(redis_client, order_channel(account_id), event)


def publish_trade_event(redis_client, account_id, event):
    return _publish(redis_client, trade_channel(account_id), event)


def publish_order_error_event(redis_client, account_id, event):
    return _publish(redis_client, order_error_channel(account_id), event)


def publish_cancel_error_event(redis_client, account_id, event):
    return _publish(redis_client, cancel_error_channel(account_id), event)


def normalize_order_error_event(order_error, account_id=""):
    """Build a JSON-able order-error event dict (废单/拒单).

    QMT order callbacks carry the failed order via m_strOrderSysID / error info.
    MiniQMT's on_order_error receives an XtOrderError with error_id/error_msg.
    """
    return {
        "event_type": EVENT_ORDER_ERROR,
        "account_id": str(_attr(order_error, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": str(_attr(order_error, ["m_strInstrumentID", "stock_code"], "") or ""),
        "order_sys_id": str(_attr(order_error, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "error_id": _attr(order_error, ["m_nErrorID", "error_id", "m_nOrderStatus"]),
        # 废单状态和备注也要带上：客户端 on_order_error 需要 order_remark 关联
        # 回报（issue #64），柜台拒单理由已在 error_msg（m_strCancelInfo，#60）。
        "status": _attr(order_error, ["m_nOrderStatus", "status"]),
        "order_remark": str(_attr(order_error, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(order_error, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        # m_strCancelInfo 排在最前: 官方字段表把它标为「废单原因」, 而柜台的
        # 拒单理由 ("[COUNTER] 资金可用余额不足，尚需[...]") 正是走这个字段。
        # 之前只读 m_strErrorMsg, 于是 error_msg 常常是空的 (issue #60)。
        "error_msg": str(
            _attr(order_error, ["m_strCancelInfo", "m_strErrorMsg", "error_msg",
                                "m_strMsg", "m_strStatusMsg", "status_msg"], "") or ""
        ),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }


def normalize_cancel_error_event(cancel_error, account_id=""):
    """Build a JSON-able cancel-error event dict (撤单失败)."""
    return {
        "event_type": EVENT_CANCEL_ERROR,
        "account_id": str(_attr(cancel_error, ["m_strAccountID", "account_id"], account_id) or account_id or ""),
        "stock_code": str(_attr(cancel_error, ["m_strInstrumentID", "stock_code"], "") or ""),
        "order_sys_id": str(_attr(cancel_error, ["m_strOrderSysID", "order_sys_id", "order_sysid", "order_id"], "") or ""),
        "error_id": _attr(cancel_error, ["m_nErrorID", "error_id"]),
        "order_remark": str(_attr(cancel_error, ["m_strRemark", "order_remark", "remark", "user_order_id"], "") or ""),
        "user_order_id": str(_attr(cancel_error, ["m_strRemark", "user_order_id", "order_remark", "remark"], "") or ""),
        "error_msg": str(_attr(cancel_error, ["m_strErrorMsg", "error_msg", "m_strMsg"], "") or ""),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "created_at_ts": time.time(),
    }
