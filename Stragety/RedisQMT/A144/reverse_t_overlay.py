"""Pure reverse-T sizing and state transitions for the A144 v3 overlay."""
from __future__ import annotations

import math

import pandas as pd


PHASE_WATCH = "WATCH"
PHASE_ARMED = "ARMED"
PHASE_SOLD = "SOLD"
PHASE_DIPPING = "DIPPING"
PHASE_DONE = "DONE"
PHASE_BLOCKED = "BLOCKED"


def reverse_t_sell_shares(
        sellable_shares,
        owned_shares,
        fraction,
        lot_size):
    sellable = max(0, int(sellable_shares or 0))
    owned = max(0, int(owned_shares or 0))
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    if not math.isfinite(float(fraction)) or not 0 < float(fraction) <= 1:
        raise ValueError("fraction must be within (0, 1]")
    scoped = min(sellable, owned)
    if scoped < lot_size:
        return 0
    fractional = int(scoped * float(fraction))
    rounded = fractional // lot_size * lot_size
    return min(scoped, max(lot_size, rounded))


def atr_percent(history, period):
    if history is None or len(history) < period + 1:
        return None
    high = pd.to_numeric(history["high"], errors="coerce")
    low = pd.to_numeric(history["low"], errors="coerce")
    close = pd.to_numeric(history["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    latest_close = float(close.iloc[-1])
    atr = float(true_range.iloc[-period:].mean())
    if not math.isfinite(atr) or latest_close <= 0:
        return None
    return atr / latest_close


def sell_trigger(open_price, atr_pct, config):
    rise = float(atr_pct) * float(config.DAYT_SELL_ATR_MULT)
    rise = max(float(config.DAYT_SELL_RISE_MIN_PCT), rise)
    rise = min(float(config.DAYT_SELL_RISE_MAX_PCT), rise)
    return round(float(open_price) * (1.0 + rise), 2)


def buyback_percent(atr_pct, config):
    value = float(atr_pct) * float(config.DAYT_BUYBACK_ATR_MULT)
    value = max(float(config.DAYT_BUYBACK_MIN_PCT), value)
    return min(float(config.DAYT_BUYBACK_MAX_PCT), value)


def new_symbol_state(today, open_price, atr_pct, config):
    trigger = sell_trigger(open_price, atr_pct, config)
    return {
        "date": str(today),
        "phase": PHASE_WATCH,
        "open_price": float(open_price),
        "atr_pct": float(atr_pct),
        "sell_trigger": trigger,
        "peak_price": 0.0,
        "armed_samples": 0,
        "sold_shares": 0,
        "initial_sold_shares": 0,
        "sell_price": 0.0,
        "sell_order_id": None,
        "buyback_target": 0.0,
        "dip_price": 0.0,
        "buy_order_id": None,
        "buy_price": 0.0,
        "gross_spread": 0.0,
        "cycle_count": 0,
        "order_sequence": 0,
        "last_event": "initialized",
    }


def next_action(state, price, now_hms, config):
    price = float(price)
    if price <= 0:
        return None
    phase = state.get("phase", PHASE_WATCH)
    if phase in (PHASE_DONE, PHASE_BLOCKED):
        return None
    if phase == PHASE_WATCH:
        if now_hms >= config.DAYT_NEW_SELL_CUTOFF:
            return None
        if int(state.get("cycle_count", 0)) >= config.DAYT_MAX_CYCLES_PER_SYMBOL:
            return None
        if price >= float(state["sell_trigger"]):
            state["phase"] = PHASE_ARMED
            state["peak_price"] = price
            state["armed_samples"] = 0
            state["last_event"] = "sell threshold reached"
        return None
    if phase == PHASE_ARMED:
        if now_hms >= config.DAYT_NEW_SELL_CUTOFF:
            state["phase"] = PHASE_DONE
            state["last_event"] = "armed sell expired at cutoff"
            return None
        state["armed_samples"] = int(state.get("armed_samples", 0)) + 1
        state["peak_price"] = max(float(state.get("peak_price", 0.0)), price)
        peak = float(state["peak_price"])
        trigger = float(state["sell_trigger"])
        extension = peak / trigger - 1.0
        pullback = (peak - price) / peak if peak > 0 else 0.0
        confirmed = bool(
            state["armed_samples"] >= config.DAYT_ARM_MIN_SAMPLES and
            extension >= config.DAYT_ARM_EXTENSION_PCT and
            pullback >= config.DAYT_SELL_PULLBACK_PCT)
        if confirmed:
            return {"side": "SELL", "reason": "reverse-t-reversal"}
        return None
    if phase in (PHASE_SOLD, PHASE_DIPPING):
        sell_price_value = float(state.get("sell_price", 0.0))
        if now_hms >= config.DAYT_FORCE_BUYBACK_TIME:
            return {"side": "BUY", "reason": "force-buyback"}
        if price >= sell_price_value * (
                1.0 + config.DAYT_EMERGENCY_RISE_PCT):
            return {"side": "BUY", "reason": "emergency-buyback"}
        target = float(state.get("buyback_target", 0.0))
        if phase == PHASE_SOLD and price <= target:
            state["phase"] = PHASE_DIPPING
            state["dip_price"] = price
            state["last_event"] = "buyback target touched"
            return None
        if phase == PHASE_DIPPING:
            state["dip_price"] = min(
                float(state.get("dip_price", price) or price),
                price)
            dip = float(state["dip_price"])
            bounce = price / dip - 1.0 if dip > 0 else 0.0
            if bounce >= config.DAYT_BUYBACK_BOUNCE_PCT:
                return {"side": "BUY", "reason": "normal-buyback"}
    return None


def record_sell_fill(state, shares, price, order_id, config):
    shares = int(shares)
    price = float(price)
    if shares <= 0 or price <= 0:
        raise ValueError("sell fill must have positive shares and price")
    target_pct = buyback_percent(state["atr_pct"], config)
    state["phase"] = PHASE_SOLD
    state["sold_shares"] = shares
    state["initial_sold_shares"] = shares
    state["sell_price"] = price
    state["sell_order_id"] = order_id
    state["buyback_target"] = round(price * (1.0 - target_pct), 2)
    state["dip_price"] = 0.0
    state["cycle_count"] = int(state.get("cycle_count", 0)) + 1
    state["last_event"] = "sell filled"


def record_buy_fill(state, shares, price, order_id):
    shares = int(shares)
    price = float(price)
    outstanding = int(state.get("sold_shares", 0))
    if shares <= 0 or price <= 0 or outstanding <= 0:
        raise ValueError("buy fill requires an open reverse-T leg")
    bought = min(shares, outstanding)
    state["gross_spread"] = float(state.get("gross_spread", 0.0)) + (
        float(state["sell_price"]) - price) * bought
    state["sold_shares"] = outstanding - bought
    state["buy_order_id"] = order_id
    state["buy_price"] = price
    if state["sold_shares"] <= 0:
        state["sold_shares"] = 0
        state["phase"] = PHASE_DONE
        state["last_event"] = "buyback completed"
    else:
        state["phase"] = PHASE_SOLD
        state["last_event"] = "buyback partially filled"


def has_open_reverse_t_legs(payload):
    symbols = (payload or {}).get("symbols", {})
    return any(
        int(state.get("sold_shares", 0) or 0) > 0
        for state in symbols.values())
