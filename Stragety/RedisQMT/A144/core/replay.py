# -*- coding: utf-8 -*-
"""Continuous-account replay; execution providers cannot submit orders."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .trend import TrendConfig
from .trend import exit_reason


INITIAL_CASH = 1_000_000.0


def metrics(equity: pd.DataFrame, initial: float = INITIAL_CASH) -> dict:
    if equity.empty:
        raise ValueError("empty equity curve")
    values = equity["equity"]
    elapsed = max((pd.Timestamp(equity.iloc[-1]["date"]) -
                   pd.Timestamp(equity.iloc[0]["date"])).days / 365.25, 1 / 252)
    daily = values.pct_change().fillna(values.iloc[0] / initial - 1)
    peaks = values.cummax().clip(lower=initial)
    return {"annual_return": float((values.iloc[-1] / initial) ** (1 / elapsed) - 1),
            "total_return": float(values.iloc[-1] / initial - 1),
            "max_drawdown": float((values / peaks - 1).min()),
            "sharpe": float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0,
            "final_equity": float(values.iloc[-1]),
            "average_exposure": float(equity["exposure"].mean())}


def fee(notional: float, side: str, date: str, scale: float) -> float:
    rate = 0.00076 if side == "BUY" else (0.00176 if date < "20230828" else 0.00126)
    return max(5.0, notional * rate) * scale


def buy_quantity(budget: float, price: float, code: str, cost_scale: float) -> int:
    if not np.isfinite(price) or price <= 0 or budget <= 0:
        return 0
    # Conservative 100-share increments; STAR requires at least 200 shares.
    quantity = int(budget / (price * (1 + 0.00076 * cost_scale)) // 100) * 100
    while quantity > 0 and quantity * price + fee(quantity * price, "BUY", "", cost_scale) > budget:
        quantity -= 100
    minimum = 200 if code.startswith(("688", "689")) else 100
    return quantity if quantity >= minimum else 0


class DailyExecution:
    label = "daily-prescreen-only"

    def quote(self, code: str, date: str, row: dict) -> dict:
        return {"open": row["open"], "close": row["close"], "complete": True}


def executable(price: float, previous_adjusted: float, factor: float, code: str, side: str) -> bool:
    if not np.isfinite(price) or price <= 0 or previous_adjusted <= 0:
        return False
    change = price * factor / previous_adjusted - 1
    # Unknown historical ST: conservatively block all main-board gaps near 5%.
    boundary = 0.195 if code.startswith(("300", "301", "688", "689")) else 0.048
    return change < boundary if side == "BUY" else change > -boundary


def replay(frame: pd.DataFrame, config: TrendConfig, start: str, end: str,
           execution=None, cost_scale: float = 1.0, baseline: bool = False,
           capacity_fraction: float = 0.001,
           reserve_weak_slot: bool = True):
    execution = execution or DailyExecution()
    data = frame.loc[(frame["date"] >= start) & (frame["date"] <= end)]
    cash = INITIAL_CASH
    positions = {}
    cooldown = {}
    pending_entries = []
    pending_exits = {}
    reservations = []
    equity_rows = []
    trades = []
    fills = []
    missing = []
    last_equity = INITIAL_CASH
    for day_index, daily in enumerate(data.groupby("date", sort=True)):
        date = str(daily[0])
        rows = daily[1].set_index("code").to_dict("index")
        reservations = [release for release in reservations if release > day_index]
        quotes = {}

        def quote(code):
            if code not in quotes:
                quotes[code] = execution.quote(code, date, rows[code])
                if not quotes[code]["complete"]:
                    raise ValueError(f"incomplete minute session: {code} {date}")
            return quotes[code]

        for code in sorted(list(pending_exits)):
            if code not in rows or code not in positions:
                continue
            row = rows[code]
            factor = row["adj_close"] / row["close"]
            price = float(quote(code)["open"])
            if not executable(price, row["previous"], factor, code, "SELL"):
                continue
            position = positions[code]
            # Synthetic total-return units; corporate actions are not cash-ledger exact.
            notional = position["units"] * price * factor
            proceeds = notional - fee(notional, "SELL", date, cost_scale)
            cash += proceeds
            pnl = proceeds - position["cash_in"]
            reason = pending_exits.pop(code)
            trades.append({"code": code, "entry_date": position["entry_date"], "exit_date": date,
                           "capital_in": position["cash_in"], "pnl": pnl,
                           "return": pnl / position["cash_in"], "reason": reason})
            fills.append({"date": date, "code": code, "side": "SELL", "price": price,
                          "notional": notional, "reason": reason})
            if baseline and reserve_weak_slot and reason == "weak-time":
                reservations.append(day_index + max(0, 20 - position["days"]))
            cooldown[code] = day_index + ((60 if pnl < 0 else 30) if baseline else config.cooldown)
            del positions[code]

        slots = config.max_positions - len(positions) - len(reservations)
        entries = 0
        for plan in pending_entries:
            code = plan["code"]
            if slots <= 0 or entries >= config.max_entries:
                break
            if code not in rows or code in positions or cooldown.get(code, -1) > day_index:
                continue
            row = rows[code]
            factor = row["adj_close"] / row["close"]
            price = float(quote(code)["open"])
            if not executable(price, row["previous"], factor, code, "BUY"):
                continue
            budget = min(cash, last_equity * plan["weight"], plan["capacity"])
            quantity = buy_quantity(budget, price, code, cost_scale)
            if quantity <= 0:
                continue
            notional = quantity * price
            cash_in = notional + fee(notional, "BUY", date, cost_scale)
            cash -= cash_in
            adjusted_price = price * factor
            positions[code] = {"units": quantity / factor, "cash_in": cash_in,
                               "entry_price": adjusted_price, "entry_date": date,
                               "peak": adjusted_price, "mark": adjusted_price, "days": 0}
            fills.append({"date": date, "code": code, "side": "BUY", "price": price,
                          "notional": notional, "reason": config.entry})
            slots -= 1
            entries += 1
        pending_entries = []

        for code, position in positions.items():
            position["days"] += 1
            if code not in rows:
                missing.append({"date": date, "code": code})
                continue
            row = rows[code]
            factor = row["adj_close"] / row["close"]
            position["mark"] = float(quote(code)["close"]) * factor
            position["peak"] = max(position["peak"], float(row["adj_close"]))
            if code in pending_exits:
                continue
            if baseline:
                change = row["adj_close"] / position["entry_price"] - 1
                stop = -0.12 if position["days"] <= 3 else -0.18
                reason = ""
                if change <= stop:
                    reason = "stop"
                elif position["days"] >= 12 and change <= -0.04 and row["adj_close"] < row["ma20"]:
                    reason = "weak-time"
                elif position["days"] >= 20:
                    reason = "max-hold"
                elif row["market_exit"]:
                    reason = "market"
            else:
                reason = exit_reason(position, row, config)
            if reason:
                pending_exits[code] = reason
        market_value = sum(p["units"] * p["mark"] for p in positions.values())
        last_equity = cash + market_value
        if cash < -1e-6:
            raise AssertionError("negative cash")
        equity_rows.append({"date": date, "equity": last_equity, "cash": cash,
                            "exposure": market_value / last_equity, "positions": len(positions)})
        slots = config.max_positions - len(positions) + len(pending_exits) - len(reservations)
        candidates = daily[1].loc[daily[1]["entry"]].sort_values(
            ["score", "code"], ascending=[False, True])
        for row in candidates.to_dict("records"):
            code = row["code"]
            if code in positions or cooldown.get(code, -1) > day_index:
                continue
            if len(pending_entries) >= min(slots, config.max_entries):
                break
            pending_entries.append({"code": code, "weight": float(row["weight"]),
                                    "capacity": float(row["amount20"]) * capacity_fraction,
                                    "atr_pct": float(row["atr_pct"])})
        if baseline and pending_entries:
            risks = [1 / max(plan["atr_pct"], 0.02) for plan in pending_entries]
            average = float(np.mean(risks))
            for plan, risk in zip(pending_entries, risks):
                plan["weight"] = (min(1.35, max(0.65, risk / average)) /
                                  config.max_positions)
    equity = pd.DataFrame(equity_rows)
    results = metrics(equity)
    results["trades"] = len(trades)
    results["missing_held_daily_rows"] = len(missing)
    results["execution"] = execution.label
    results["open_positions"] = len(positions)
    results["cost_scale"] = cost_scale
    return results, equity, pd.DataFrame(trades), pd.DataFrame(fills)
