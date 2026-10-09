"""Pure Alpha144 signal and sizing functions shared by live and tests."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import config


def alpha144(close_values, amount_values, window=config.FACTOR_WINDOW):
    close = np.asarray(close_values, dtype=float)
    amount = np.asarray(amount_values, dtype=float)
    count = min(len(close), len(amount))
    if count < window + 1:
        return None
    close = close[-(window + 1):]
    amount = amount[-(window + 1):]
    returns = close[1:] / close[:-1] - 1.0
    valid = (returns < 0) & np.isfinite(amount[1:]) & (amount[1:] > 0)
    if not valid.any():
        return 0.0
    return float(np.sum(np.abs(returns[valid]) / amount[1:][valid]))


def anchored_refresh_date(trading_dates, asof,
                          anchor=config.INITIAL_RESEARCH_ANCHOR,
                          interval=config.REFRESH_INTERVAL):
    dates = [str(value) for value in trading_dates
             if str(value) >= anchor and str(value) <= asof]
    if not dates:
        return None
    index = len(dates) - 1
    refresh_index = index - index % interval
    return dates[refresh_index]


def market_is_open_for_entries(benchmark):
    if benchmark is None or len(benchmark) < config.MARKET_MA:
        return False
    close = pd.to_numeric(benchmark["adj_close"], errors="coerce").dropna()
    if len(close) < config.MARKET_MA:
        return False
    moving_average = float(close.iloc[-config.MARKET_MA:].mean())
    return bool(close.iloc[-1] >= moving_average *
                (1.0 - config.MARKET_FILTER_PCT))


def symbol_snapshot(frame, refresh_date):
    if frame is None or len(frame) < config.MIN_HISTORY_BARS:
        return None
    data = frame.sort_index().copy()
    close = pd.to_numeric(data["adj_close"], errors="coerce")
    high = pd.to_numeric(data["adj_high"], errors="coerce")
    low = pd.to_numeric(data["adj_low"], errors="coerce")
    open_price = pd.to_numeric(data["adj_open"], errors="coerce")
    amount = pd.to_numeric(data["amount"], errors="coerce")
    if close.isna().any() or len(close) < config.MIN_HISTORY_BARS:
        return None
    refresh = data.loc[data.index <= refresh_date]
    factor = alpha144(refresh["adj_close"], refresh["amount"])
    if factor is None:
        return None
    returns = close.pct_change(fill_method=None)
    current = float(close.iloc[-1])
    previous = float(close.iloc[-2])
    previous_high = float(close.iloc[-(config.BREAKOUT_PERIOD + 1):-1].max())
    previous_amount = float(amount.iloc[-(config.VOL_LOOKBACK + 1):-1].mean())
    ma20 = float(close.iloc[-config.MA_STOCK:].mean())
    previous_ma20 = float(close.iloc[
        -(config.MA_STOCK + config.MA_SLOPE_PERIOD):-config.MA_SLOPE_PERIOD].mean())
    recent_up = int((returns.iloc[-config.MOMENTUM_DAYS:] > 0).sum())
    minimum_return = float(returns.iloc[-config.GAP_HISTORY_DAYS:].min())
    recent_range = ((high - low) / close.replace(0, np.nan)).iloc[
        -config.RECENT_RANGE_DAYS:]
    max_range = float(recent_range.max())
    consecutive_up = 0
    for offset in range(2, config.MAX_CONSECUTIVE_UP + 2):
        if close.iloc[-offset] > close.iloc[-offset - 1]:
            consecutive_up += 1
        else:
            break
    gap = float(open_price.iloc[-1] / close.iloc[-2] - 1.0)
    true_range = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_pct = float(true_range.iloc[-20:].mean() / current)
    daily_return = current / previous - 1.0
    eligible = all([
        daily_return < 0.098,
        minimum_return > config.GAP_HISTORY_MAX_PCT,
        consecutive_up < config.MAX_CONSECUTIVE_UP,
        max_range <= config.MAX_RECENT_RANGE_PCT,
        gap > config.GAP_DOWN_PCT,
        recent_up >= config.MOMENTUM_UP_MIN,
        ma20 > previous_ma20,
        current > ma20,
        current > previous_high * (1.0 + config.BREAKOUT_STRENGTH_PCT),
        float(amount.iloc[-1]) >= previous_amount * config.VOL_RATIO_MIN,
        float(amount.iloc[-20:].mean()) >= config.MIN_DAILY_AMOUNT,
    ])
    return {
        "date": str(data.index[-1]),
        "factor": factor,
        "eligible": eligible,
        "close": current,
        "raw_close": float(data["raw_close"].iloc[-1]),
        "atr_pct": atr_pct,
        "daily_return": daily_return,
        "below_ma20": bool(current < ma20),
    }


def top_factor_codes(snapshots, top_pct=config.FACTOR_TOP_PCT):
    valid = [(code, values["factor"]) for code, values in snapshots.items()
             if values is not None and math.isfinite(values["factor"])]
    valid.sort(key=lambda item: item[1], reverse=True)
    count = max(1, int(len(valid) * top_pct)) if valid else 0
    return [code for code, _ in valid[:count]]


def normalized_inverse_atr_allocations(
        candidates,
        equity,
        max_positions=config.MAX_POSITIONS,
        multiplier_min=config.RISK_MULTIPLIER_MIN,
        multiplier_max=config.RISK_MULTIPLIER_MAX,
        budget_scale=1.0):
    if not candidates or equity <= 0 or max_positions <= 0:
        return {}
    base_allocation = float(equity) / max_positions
    inverse = {
        code: 1.0 / max(float(values["atr_pct"]), 0.02)
        for code, values in candidates.items()
    }
    average_inverse = float(np.mean(list(inverse.values())))
    allocations = {}
    for code, inverse_value in inverse.items():
        multiplier = inverse_value / average_inverse
        multiplier = min(multiplier_max, max(multiplier_min, multiplier))
        allocations[code] = base_allocation * multiplier * budget_scale
    return allocations


def should_trigger_reserved_weak_exit(
        bars_held,
        pnl,
        below_ma20,
        minimum_bars,
        maximum_return):
    return bool(
        int(bars_held) >= int(minimum_bars) and
        float(pnl) <= float(maximum_return) and
        bool(below_ma20))


def age_slot_reservations(remaining_bars):
    aged = []
    for value in remaining_bars:
        remaining = int(value) - 1
        if remaining > 0:
            aged.append(remaining)
    return aged


def whole_lot_shares(allocation, price, lot=config.TRADE_LOT_SIZE):
    if allocation <= 0 or price <= 0 or lot <= 0:
        return 0
    return int(allocation / price / lot) * lot
