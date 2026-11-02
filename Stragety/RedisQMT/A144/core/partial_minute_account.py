# -*- coding: utf-8 -*-
"""Research minute account: causal child orders, realized partial fills, no orders.

Submission uses only a completed previous bar. Realized fills use at most5%
of the following bar's volume, at an adverse high/low plus modeled slippage.
Both signal generation and provider validation are supplied by the caller.
"""
import math

import pandas as pd

from .replay import buy_quantity, fee, metrics

PARTICIPATION = .05
BUY_START = "093300"
BUY_END = "094600"
SELL_START = "093200"
SELL_END = "145600"


def round_capacity(volume):
    return max(0, int(float(volume) * 100 * PARTICIPATION // 100) * 100)


def replay(frame, actions, execution, secondary_trigger, primary_exit=None,
           cost_scale=1., slippage=.001, start="20251010", end="20261009", secondary_exit=None,
           initial_cash=1_000_000.):
    """Fixed execution protocol, 1M cash,5slots,2new parents/day, T+1.

    Prices are pessimistic historical bounds, not promised market fills. Limit
    children reprice from the previous close within1%; no same-minute sale cash
    funds a buy intention. Each child has its own conservative minimum fee.
    """
    secondary = frame.get("strategy", pd.Series("secondary", index=frame.index)).eq("secondary")
    if (frame.entry & secondary & ~frame.secondary_active).any():
        raise ValueError("Secondary entry outside market activation regime")
    cash = float(initial_cash)
    receivable = 0.
    last_equity = cash
    positions = {}
    sell_plans = {}
    cooldown = {}
    entry_plans = []
    fills = []
    trades = []
    children = []
    dividends = []
    equity_rows = []
    minute_equity = []
    timeline = list(pd.date_range("2000-01-01 09:31", "2000-01-01 11:30", freq="min").strftime("%H%M%S"))
    timeline += list(pd.date_range("2000-01-01 13:01", "2000-01-01 15:00", freq="min").strftime("%H%M%S"))
    events = {(code, event["date"]): event for code, rows in actions.items() for event in rows}
    for day_index, (date, daily) in enumerate(frame.loc[frame.date.between(start, end)].groupby("date", sort=True)):
        rows = daily.set_index("code").to_dict("index")
        session = {}
        parents = {}
        issued = 0
        for code, pos in positions.items():
            event = events.get((code, date), {})
            if any(event.get(field, 0) for field in ("bonus", "gift", "allot", "reform")):
                raise ValueError(f"Unverified noncash action on held {code} {date}")
            credit = pos["quantity"] * event.get("interest", 0.) * .8
            if credit:
                receivable += credit
                pos["dividend"] += credit
                dividends.append({"code": code, "date": date, "quantity": pos["quantity"], "net_receivable": credit})

        def bars(code):
            if code not in rows:
                raise ValueError(f"Missing daily stock {code} {date}")
            if code not in session:
                row = rows[code]
                if row["volume"] == 0 and row["amount"] == 0:
                    session[code] = None
                else:
                    data = execution.bars(code, date, row)
                    if not data.index.is_monotonic_increasing or data.index.duplicated().any():
                        raise ValueError(f"Invalid minute index {code} {date}")
                    if set(data.index.astype(str).str[8:14]) != set(timeline) or len(data) != 240:
                        raise ValueError(f"Incomplete minute session {code} {date}")
                    session[code] = data
            return session[code]

        def allowed(code, price, side):
            event = events.get((code, date), {})
            previous = (rows[code]["previous_raw"] - event.get("interest", 0.) +
                        event.get("allot", 0.) * event.get("allot_price", 0.))
            previous /= 1 + event.get("bonus", 0.) + event.get("gift", 0.) + event.get("allot", 0.)
            boundary = .195 if code.startswith(("300", "301", "688", "689")) else .048
            change = price / previous - 1
            return price > 0 and (change < boundary if side == "BUY" else change > -boundary)

        for step, time in enumerate(timeline):
            previous_time = timeline[step - 1] if step else None
            # Reserve new parents before reading any current bar's price/volume.
            if BUY_START <= time <= BUY_END and issued < 2:
                for plan in entry_plans:
                    code = plan["code"]
                    if issued >= 2 or len(set(positions) | set(parents)) >= 5:
                        break
                    if code in positions or code in parents or cooldown.get(code, -1) > day_index:
                        continue
                    data = bars(code)
                    if data is None:
                        continue
                    previous = data.loc[date + previous_time]
                    reference = float(previous["close"])
                    if not allowed(code, reference, "BUY"):
                        continue
                    free_cash = cash - sum(p["budget_left"] for p in parents.values())
                    budget = min(free_cash, last_equity * float(plan.get("weight", .2)), float(plan["amount20"]) * .001)
                    quantity = buy_quantity(budget, reference * 1.01, code, cost_scale)
                    if quantity <= 0:
                        continue
                    parents[code] = {"quantity_left": quantity, "budget_left": budget,
                                     "signal_date": plan["date"], "strategy": plan.get("strategy", "secondary"),
                                     "buy_offset": float(plan.get("buy_offset", 0.))}
                    if plan.get("fixed_bid", False):
                        parents[code]["bid_anchor_time"] = date + previous_time
                    issued += 1
            submitted = []
            if previous_time and SELL_START <= time <= SELL_END:
                for code in sorted(sell_plans):
                    pos = positions[code]
                    if pos["entry_date"] >= date:
                        continue
                    data = bars(code)
                    if data is None:
                        continue
                    previous = data.loc[date + previous_time]
                    quantity = min(pos["quantity"], round_capacity(previous["volume"]))
                    if code.startswith(("688", "689")) and quantity < 200 and quantity != pos["quantity"]:
                        continue
                    if quantity <= 0:
                        continue
                    submitted.append({"code": code, "side": "SELL", "submitted_quantity": quantity,
                        "limit": float(previous["close"]) * .99, "trigger_time": sell_plans[code]["time"],
                        "reason": sell_plans[code]["reason"], "previous_volume": float(previous["volume"])})
            if previous_time and BUY_START <= time <= BUY_END:
                for code in sorted(parents):
                    parent = parents[code]
                    previous = bars(code).loc[date + previous_time]
                    limit = float(previous["close"]) * 1.01
                    offset = parent["buy_offset"]
                    if offset:
                        anchor = parent.get("bid_anchor_time", date + previous_time)
                        reference = float(bars(code).loc[anchor, "close"])
                        limit = math.floor(reference * (1 - offset) * 100 + 1e-8) / 100
                    quantity = min(parent["quantity_left"], round_capacity(previous["volume"]),
                                   buy_quantity(parent["budget_left"], limit, code, cost_scale))
                    if code.startswith(("688", "689")) and quantity < 200:
                        continue
                    if quantity > 0:
                        child = {"code": code, "side": "BUY", "submitted_quantity": quantity,
                            "limit": limit, "signal_date": parent["signal_date"], "trigger_time": parent["signal_date"] + "150000",
                            "previous_volume": float(previous["volume"])}
                        if offset:
                            child["buy_offset"] = offset
                            if "bid_anchor_time" in parent:
                                child["bid_anchor_time"] = parent["bid_anchor_time"]
                        submitted.append(child)
            # Actual volume determines only fill realization, never submission.
            for child in submitted:
                code = child["code"]
                side = child["side"]
                current = bars(code).loc[date + time]
                quantity = min(child["submitted_quantity"], round_capacity(current["volume"]))
                raw_price = float(current["high"] if side == "BUY" else current["low"])
                price = raw_price * (1 + slippage if side == "BUY" else 1 - slippage)
                if child.get("buy_offset", 0.):
                    price = math.ceil(price * 100 - 1e-8) / 100
                limit_ok = price <= child["limit"] if side == "BUY" else price >= child["limit"]
                if not allowed(code, price, side) or not limit_ok:
                    quantity = 0
                if side == "BUY" and code.startswith(("688", "689")) and quantity < 200:
                    quantity = 0
                child_row = dict(child, date=date, time=time, previous_time=previous_time,
                                 actual_volume=float(current["volume"]), filled_quantity=quantity,
                                 price=price, raw_price=raw_price)
                children.append(child_row)
                if not quantity:
                    continue
                notional = quantity * price
                charge = fee(notional, side, date, cost_scale)
                if side == "BUY":
                    parent = parents[code]
                    cost = notional + charge
                    if cost > parent["budget_left"] + 1e-7 or cost > cash + 1e-7:
                        raise AssertionError("Buy fills exceed pre-reserved available cash")
                    if code not in positions:
                        positions[code] = {"quantity": 0, "initial_quantity": 0, "cash_in": 0.,
                            "buy_notional": 0., "dividend": 0., "realized_proceeds": 0.,
                            "entry_date": date, "entry_factor": rows[code]["causal_factor"],
                            "strategy": parent["strategy"], "days": 0, "peak": 0., "last_mark": price}
                    pos = positions[code]
                    pos["quantity"] += quantity
                    pos["initial_quantity"] += quantity
                    pos["cash_in"] += cost
                    pos["buy_notional"] += notional
                    pos["entry_price"] = pos["buy_notional"] / pos["initial_quantity"]
                    pos["peak"] = max(pos["peak"], pos["entry_price"] * pos["entry_factor"])
                    cash -= cost
                    parent["budget_left"] -= cost
                    parent["quantity_left"] -= quantity
                    if parent["quantity_left"] == 0:
                        del parents[code]
                else:
                    pos = positions[code]
                    assert pos["entry_date"] < date
                    proceeds = notional - charge
                    cash += proceeds
                    pos["realized_proceeds"] += proceeds
                    pos["quantity"] -= quantity
                fills.append(dict(child_row, quantity=quantity, notional=notional, fee=charge,
                                  strategy=pos["strategy"], entry_date=pos["entry_date"]))
                if side == "SELL" and pos["quantity"] == 0:
                    pnl = pos["realized_proceeds"] + pos["dividend"] - pos["cash_in"]
                    trades.append({"code": code, "entry_date": pos["entry_date"], "exit_date": date,
                        "quantity": pos["initial_quantity"], "capital_in": pos["cash_in"], "dividend": pos["dividend"],
                        "proceeds": pos["realized_proceeds"], "pnl": pnl, "return": pnl / pos["cash_in"],
                        "strategy": pos["strategy"], "reason": sell_plans[code]["reason"]})
                    cooldown[code] = day_index + (10 if pos["strategy"] == "primary" else 5)
                    del positions[code]
                    del sell_plans[code]
            if time == BUY_END:
                parents.clear()  # Unfilled buy quantity expires, never a position.
            value = 0.
            for code, pos in positions.items():
                data = bars(code)
                close = float(data.loc[date + time, "close"]) if data is not None else pos["last_mark"]
                pos["last_mark"] = close
                value += pos["quantity"] * close
                if time >= SELL_START and pos["strategy"] == "secondary" and pos["entry_date"] < date and code not in sell_plans and data is not None:
                    reason = secondary_trigger(pos, close, rows[code]["causal_factor"])
                    if reason:
                        sell_plans[code] = {"reason": reason, "time": date + time}
            total = cash + value + receivable
            assert cash >= -1e-7 and len(set(positions) | set(parents)) <= 5
            assert cash - sum(p["budget_left"] for p in parents.values()) >= -1e-7
            minute_equity.append({"timestamp": date + time, "equity": total, "cash": cash,
                                  "receivable": receivable, "exposure": value / total, "positions": len(positions)})
        for code, pos in positions.items():
            row = rows[code]
            pos["days"] += 1
            pos["peak"] = max(pos["peak"], pos["last_mark"] * row["causal_factor"])
            if code not in sell_plans:
                if pos["strategy"] == "primary":
                    view = dict(pos, entry_price=pos["entry_price"] * pos["entry_factor"])
                    reason = primary_exit(view, row)
                else:
                    if secondary_exit is not None:
                        reason = secondary_exit(pos, row)
                    else:
                        reason = secondary_trigger(pos, pos["last_mark"], row["causal_factor"])
                        if not reason and pos["days"] >= 5:
                            reason = "time"
                if reason:
                    sell_plans[code] = {"reason": reason, "time": date + "150000"}
        last_equity = minute_equity[-1]["equity"]
        equity_rows.append(dict(minute_equity[-1], date=date))
        candidates = daily.loc[daily.entry & ~daily.code.isin(positions)]
        candidates = candidates.loc[candidates.code.map(lambda code: cooldown.get(code, -1) <= day_index)]
        # Signal plans are a ranked queue, not anticipatory released slots/cash.
        entry_plans = candidates.sort_values(["score", "code"], ascending=[False, True]).to_dict("records")
        if (day_index + 1) % 40 == 0:
            print(f"partial replay: {date}, equity={last_equity:.2f}, closed={len(trades)}", flush=True)
    equity = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trades, columns=["code", "entry_date", "exit_date", "quantity", "capital_in", "dividend", "proceeds", "pnl", "return", "strategy", "reason"])
    minute_frame = pd.DataFrame(minute_equity)
    values = minute_frame.equity
    result = metrics(equity, initial_cash)
    result.update(trades=len(trades), wins=int(trade_frame.pnl.gt(0).sum()),
        win_rate=float(trade_frame.pnl.gt(0).mean()) if len(trades) else None,
        open_positions=len(positions), receivable=receivable, cost_scale=cost_scale, slippage=slippage,
        participation=PARTICIPATION, price_model="minute-high-BUY/minute-low-SELL plus adverse slippage",
        minute_close_drawdown=float((values / values.cummax().clip(lower=initial_cash) - 1).min()),
        corporate_action_entitlements=dividends, remaining_positions=positions,
        closed_by_strategy={kind: {"trades": len(group), "wins": int(group.pnl.gt(0).sum()),
            "win_rate": float(group.pnl.gt(0).mean()), "pnl": float(group.pnl.sum())}
            for kind, group in trade_frame.groupby("strategy")})
    fill_frame = pd.DataFrame(fills) if fills else pd.DataFrame(columns=["code", "side", "date", "time", "previous_time",
        "trigger_time", "signal_date", "entry_date", "submitted_quantity", "filled_quantity", "quantity", "limit", "price", "notional", "fee", "strategy"])
    return result, equity, trade_frame, fill_frame, pd.DataFrame(children), minute_frame
