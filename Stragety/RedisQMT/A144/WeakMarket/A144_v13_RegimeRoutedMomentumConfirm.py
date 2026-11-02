# -*- coding: utf-8 -*-
"""Independent routed RSI2 stock reversals and dual-horizon commodity ETF trends.

ETF eligibility confirms positive prior20day and60day returns, then ranks
by the original60day return. All daily filters are shifted to the next session.
No sibling strategy imports.
"""
import numpy as np
import pandas as pd


ETF_CODES = ("159980.SZ", "159981.SZ", "159985.SZ")
ETF_CASH_FRACTION = .8
STOCK_CASH_FRACTION = .2
ETF_TARGET_WEIGHT = 1.
ETF_MAX_ENTRIES_PER_CODE = 2
ETF_STOP = .04
ETF_PROFIT = .06
ETF_CONFIRM_WINDOW = 20
ETF_HOLD_DAYS = 10
STOCK_TARGET_WEIGHT = .2
STOCK_STOP = .08
STOCK_PROFIT = .03


def stock_signals(frame):
    if "causal_factor" not in frame:
        raise ValueError("Causal corporate-action price adjustment required")
    frame = frame.sort_values(["code", "date"]).copy()
    group = frame.groupby("code", sort=False)
    delta = group.adj_close.diff()
    gain = delta.clip(lower=0).groupby(frame.code).transform(
        lambda values: values.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    loss = (-delta.clip(upper=0)).groupby(frame.code).transform(
        lambda values: values.ewm(alpha=.5, adjust=False, min_periods=20).mean())
    rsi = 100 * gain / (gain + loss).replace(0, np.nan)
    ret3 = group.adj_close.pct_change(3, fill_method=None)
    down2 = frame.ret.lt(0) & group.ret.shift().lt(0)
    frame["secondary_active"] = frame.market_strict & frame.breadth.lt(.4)
    frame["entry"] = (frame.secondary_active & down2 & rsi.lt(10) & frame.mom60_rank.ge(.8)
                      & frame.adj_close.gt(frame.ma50) & ret3.le(-.04)
                      & frame.age.ge(130) & frame.amount20.ge(3e7)
                      & frame.atr_pct.between(.01, .06) & frame.close.ge(3))
    frame["score"] = 1 - rsi / 100 + .1 * frame.mom60_rank
    frame["weight"] = STOCK_TARGET_WEIGHT
    return frame


def stock_minute_trigger(position, completed_price, causal_factor):
    change = completed_price * causal_factor / (position["entry_price"] * position["entry_factor"]) - 1
    if change <= -STOCK_STOP:
        return "intraday-stop"
    if change >= STOCK_PROFIT:
        return "intraday-profit"
    return ""


def etf_gates(macro, trade_dates):
    calendar = macro.drop_duplicates("date").sort_values("date")[["date", "market_strict"]].copy()
    calendar["next_date"] = calendar.date.shift(-1)
    prior_index_down = {row.next_date: not bool(row.market_strict) for row in calendar.itertuples()
                        if isinstance(row.next_date, str)}
    return {date: bool(prior_index_down.get(date, False)) for date in trade_dates}


def etf_daily_signals(daily):
    result = {}
    for code in ETF_CODES:
        frame = daily[code].sort_index()
        average = frame.close.rolling(20, min_periods=20).mean()
        momentum = frame.close.pct_change(60, fill_method=None)
        amount = frame.amount.rolling(20, min_periods=20).mean()
        fast = frame.close.pct_change(ETF_CONFIRM_WINDOW, fill_method=None)
        good = frame.close.gt(average) & momentum.gt(0) & fast.gt(0) & amount.ge(3e7)
        result[code] = pd.DataFrame(dict(prior_ma20=average.shift(),
            prior_momentum=momentum.shift(), prior_good=good.shift(fill_value=False),
            prior_fast_momentum=fast.shift()), index=frame.index)
    scores = pd.DataFrame({code: result[code].prior_momentum.where(result[code].prior_good)
                           for code in ETF_CODES})
    leader = scores.fillna(-float("inf")).idxmax(axis=1)
    for code in ETF_CODES:
        result[code]["prior_leader"] = result[code].prior_good & leader.eq(code)
    return result


def etf_features(bars):
    return bars.copy()


def etf_entry(row, time, enabled, unused):
    return bool(enabled and "093100" <= time <= "094000" and row["prior_leader"] and
                row["close"] >= row["prior_ma20"])


def etf_exit_reason(position, row, time, age, hold_days=ETF_HOLD_DAYS):
    change = row["close"] / (position["buy_notional"] / position["bought"]) - 1
    if change <= -ETF_STOP:
        return "stop"
    if change >= ETF_PROFIT:
        return "profit"
    if age >= hold_days * 240 and time >= "145000":
        return "holding_expiry"
    return ""
