# -*- coding: utf-8 -*-
"""Fixed passive ETF bid account; conservative whole-bar crossing and partial fills."""
import math

import pandas as pd

from .commodity_intraday_account import commission, lots, tick_price


def replay(daily, gates, provider, strategy, deviation, cost_scale=1, initial_cash=1e6, slippage=.001):
    cash = float(initial_cash)
    positions = {}
    parents = {}
    fills = []
    children = []
    trades = []
    minute_equity = []
    equity = []
    serial = 0
    clock = 0
    marks = {}
    times = list(pd.date_range("2000-01-01 09:31", "2000-01-01 11:30", freq="min").strftime("%H%M%S"))
    times += list(pd.date_range("2000-01-01 13:01", "2000-01-01 15:00", freq="min").strftime("%H%M%S"))
    for date, enabled in sorted(gates.items()):
        counts = {code: 0 for code in strategy.CODES}
        active = set(strategy.CODES) if enabled else set(positions)
        sessions = {code: strategy.features(provider.bars(code, date, daily[code].loc[date])) for code in active}
        for code in active:
            marks[code] = float(daily[code].loc[date, "prior_close"])
        for i, time in enumerate(times):
            stamp = date + time
            clock += 1
            intents = []
            cash_before = cash
            if i:
                previous = date + times[i - 1]
                for code in sorted(active):
                    row = sessions[code].loc[previous]
                    position = positions.get(code)
                    if position and not position["exit_reason"]:
                        reason = strategy.exit_reason(position, row, times[i - 1], clock - 1 - position["first_clock"])
                        if reason:
                            position["exit_reason"] = reason
                            position["exit_signal"] = previous
                            parents.pop(code, None)
                    parent = parents.get(code)
                    if parent and (clock >= parent["expires"] or not enabled):
                        parents.pop(code)
                    if (code not in parents and code not in positions and counts[code] < strategy.MAX_ENTRIES_PER_CODE and
                            strategy.entry(row, times[i - 1], enabled, deviation)):
                        reserved = sum(parent["budget"] for parent in parents.values())
                        account = cash + sum(position["quantity"] * marks.get(symbol, 0.) for symbol, position in positions.items())
                        budget = min(account * strategy.WEIGHT, daily[code].loc[date, "prior_amount20"] * .001,
                                     max(0., cash - reserved))
                        if budget >= 100 * row["close"] * 1.01 + 5 * cost_scale:
                            serial += 1
                            parents[code] = dict(id=serial, budget=float(budget), signal=previous,
                                expires=clock + strategy.PARENT_LIFETIME,
                                buy_limit=tick_price(row["close"] * (1 - strategy.BUY_OFFSET), "SELL"))
                            counts[code] += 1
                    position = positions.get(code)
                    side = "SELL" if position and position["exit_reason"] else "BUY" if code in parents else ""
                    if not side or time > "145600":
                        continue
                    limit = tick_price(row["close"] * (1.01 if side == "BUY" else .99), side)
                    quantity = lots(row["volume"])
                    if side == "BUY":
                        parent = parents[code]
                        limit = parent["buy_limit"]
                        available = parent["budget"]
                        affordable = math.floor(max(0., available - 5 * cost_scale) / (limit * (1 + .0001 * cost_scale)) / 100) * 100
                        quantity = min(quantity, affordable)
                        parent_id = parent["id"]
                        signal = parent["signal"]
                    else:
                        quantity = min(quantity, position["quantity"])
                        available = 0.
                        parent_id = position["id"]
                        signal = position["exit_signal"]
                    if quantity:
                        intent = dict(timestamp=stamp, previous=previous, code=code, side=side, parent=parent_id,
                                      signal=signal, limit=float(limit), submitted=int(quantity), cash_before=cash_before,
                                      budget=available, previous_volume=float(row["volume"]))
                        intents.append(intent)
            # Every submission is finished before this minute realizes sale cash.
            for intent in intents:
                code = intent["code"]
                side = intent["side"]
                bar = sessions[code].loc[stamp]
                price = tick_price(bar["high"] * (1 + slippage) if side == "BUY" else bar["low"] * (1 - slippage), side)
                quantity = min(intent["submitted"], lots(bar["volume"]))
                prior_close = daily[code].loc[date, "prior_close"]
                eligible = abs(price / prior_close - 1) < .095
                eligible &= price <= intent["limit"] if side == "BUY" else price >= intent["limit"]
                if not eligible:
                    quantity = 0
                intent["filled"] = int(quantity)
                children.append(intent)
                if not quantity:
                    continue
                notional = quantity * price
                charge = commission(notional, cost_scale)
                if side == "BUY":
                    if notional + charge > parents[code]["budget"] + 1e-7 or notional + charge > cash + 1e-7:
                        raise ValueError("BUY exceeds reserved or actual cash")
                    cash -= notional + charge
                    parents[code]["budget"] -= notional + charge
                    if code not in positions:
                        positions[code] = dict(id=intent["parent"], quantity=0, bought=0, buy_notional=0., buy_fee=0.,
                            sell_notional=0., sell_fee=0., entry=stamp, first_clock=clock, exit_reason="", exit_signal="")
                    position = positions[code]
                    position["quantity"] += quantity
                    position["bought"] += quantity
                    position["buy_notional"] += notional
                    position["buy_fee"] += charge
                else:
                    position = positions[code]
                    cash += notional - charge
                    position["quantity"] -= quantity
                    position["sell_notional"] += notional
                    position["sell_fee"] += charge
                    if not position["quantity"]:
                        pnl = position["sell_notional"] - position["buy_notional"] - position["buy_fee"] - position["sell_fee"]
                        trades.append(dict(code=code, parent=position["id"], entry=position["entry"], exit=stamp,
                            shares=position["bought"], buy_notional=position["buy_notional"], sell_notional=position["sell_notional"],
                            buy_fee=position["buy_fee"], sell_fee=position["sell_fee"], pnl=pnl,
                            return_pct=pnl / (position["buy_notional"] + position["buy_fee"]) * 100,
                            reason=position["exit_reason"]))
                        positions.pop(code)
                fills.append(dict(timestamp=stamp, code=code, parent=intent["parent"], side=side, quantity=quantity,
                                  price=price, notional=notional, fee=charge, cash_after=cash))
            for code in active:
                marks[code] = float(sessions[code].loc[stamp, "close"])
            value = cash + sum(position["quantity"] * marks[code] for code, position in positions.items())
            minute_equity.append(dict(timestamp=stamp, cash=cash, equity=value))
            if cash < -1e-7:
                raise ValueError("Negative cash")
        parents.clear()
        equity.append(dict(date=date, cash=cash, equity=value, open_positions=len(positions)))
    tables = [pd.DataFrame(rows) for rows in (equity, trades, fills, children, minute_equity)]
    closed = len(trades)
    wins = sum(trade["pnl"] > 0 for trade in trades)
    annual = (value / initial_cash) ** (365 / ((pd.Timestamp(max(gates)) - pd.Timestamp(min(gates))).days + 1)) - 1
    curve = tables[-1].equity
    summary = dict(execution_mode="fixed_passive_bid", buy_offset=strategy.BUY_OFFSET,
                   parent_lifetime=strategy.PARENT_LIFETIME, final_equity=value, profit=value - initial_cash, annual_return=annual * 100, closed_positions=closed,
                   slippage=slippage,
                   wins=wins, win_rate=wins / closed * 100 if closed else None,
                   minute_max_drawdown=(1 - curve / curve.cummax()).max() * 100,
                   open_positions=positions, fee=sum(fill["fee"] for fill in fills),
                   passed=bool(annual > .20 and closed >= 30 and wins / closed >= .60))
    return summary, tables
