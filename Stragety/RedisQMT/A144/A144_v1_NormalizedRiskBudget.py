#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RedisQMT Alpha144 v1 — normalized inverse-ATR risk budget.

The command is signal-only by default.  ``plan`` reads RedisQMT market/account
state and writes a next-open order plan.  ``execute --mode live`` is the only
path that submits orders, and it requires an explicit confirmation token.
Only positions created by this strategy and recorded in its checkpoint may be
sold automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Stragety.RedisQMT.A144 import config
from Stragety.RedisQMT.A144.model import anchored_refresh_date
from Stragety.RedisQMT.A144.model import market_is_open_for_entries
from Stragety.RedisQMT.A144.model import normalized_inverse_atr_allocations
from Stragety.RedisQMT.A144.model import symbol_snapshot
from Stragety.RedisQMT.A144.model import top_factor_codes
from Stragety.RedisQMT.A144.model import whole_lot_shares
from Stragety.RedisQMT.Common.logger import build_logger
from Stragety.RedisQMT.Common.redis_qmt import RedisQmtAdapter


STATE_DIR = Path(__file__).resolve().parent / "state"
PLAN_PATH = STATE_DIR / "pending_plan.json"
CHECKPOINT_PATH = STATE_DIR / "checkpoint.json"


def _value(source, *names, default=None):
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return default


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class MarketData(object):
    def __init__(self, xtdata, logger):
        self.xtdata = xtdata
        self.logger = logger

    def universe(self):
        codes = self.xtdata.get_stock_list_in_sector(config.SECTOR) or []
        return sorted(set(str(code) for code in codes if "." in str(code)))

    def history(self, codes):
        requested = sorted(set(codes + [config.BENCHMARK]))
        self.xtdata.download_history_data2(
            requested, "1d", start_time="", end_time="",
            download_timeout_seconds=900)
        fields = ["open", "high", "low", "close", "amount", "volume"]
        adjusted = self.xtdata.get_market_data_ex(
            fields, requested, period="1d", count=config.HISTORY_BARS,
            dividend_type="front", fill_data=False, timeout_seconds=600)
        raw = self.xtdata.get_market_data_ex(
            ["close"], requested, period="1d", count=config.HISTORY_BARS,
            dividend_type="none", fill_data=False, timeout_seconds=600)
        output = {}
        for code in requested:
            frame = (adjusted or {}).get(code)
            if frame is None or len(frame) == 0:
                continue
            frame = frame.copy()
            frame.index = ["".join(ch for ch in str(value) if ch.isdigit())[:8]
                           for value in frame.index]
            frame = frame.rename(columns={
                "open": "adj_open",
                "high": "adj_high",
                "low": "adj_low",
                "close": "adj_close",
            })
            raw_frame = (raw or {}).get(code)
            if raw_frame is not None and len(raw_frame):
                raw_close = raw_frame.copy()
                raw_close.index = [
                    "".join(ch for ch in str(value) if ch.isdigit())[:8]
                    for value in raw_close.index]
                frame["raw_close"] = pd.to_numeric(
                    raw_close["close"], errors="coerce").reindex(frame.index)
            else:
                frame["raw_close"] = frame["adj_close"]
            frame = frame.dropna(subset=["adj_open", "adj_high", "adj_low",
                                         "adj_close", "amount", "raw_close"])
            output[code] = frame
        return output


class AccountView(object):
    def __init__(self, adapter):
        self.adapter = adapter

    def snapshot(self):
        asset = self.adapter.trader.query_stock_asset(self.adapter.account)
        positions = self.adapter.trader.query_stock_positions(
            self.adapter.account) or []
        mapped = {}
        for position in positions:
            code = str(_value(
                position, "stock_code", "m_strStockCode", default=""))
            if not code:
                continue
            mapped[code] = {
                "shares": int(_value(
                    position, "volume", "m_nVolume", default=0) or 0),
                "sellable": int(_value(
                    position, "can_use_volume", "enable_amount",
                    "m_nCanUseVolume", default=0) or 0),
            }
        return {
            "cash": float(_value(
                asset, "available_cash", "cash", "m_dAvailableCash",
                "m_dCash", default=0.0) or 0.0),
            "total_asset": float(_value(
                asset, "total_asset", "m_dBalance", default=0.0) or 0.0),
            "positions": mapped,
        }


def empty_checkpoint():
    return {
        "strategy": config.STRATEGY_NAME,
        "last_plan_date": "",
        "last_execution_date": "",
        "owned": {},
        "cooldowns": {},
    }


def build_plan(history, account, checkpoint, logger):
    benchmark = history.get(config.BENCHMARK)
    if benchmark is None or len(benchmark) < config.MIN_HISTORY_BARS:
        raise RuntimeError("benchmark history is incomplete")
    asof = str(benchmark.index[-1])
    refresh_date = anchored_refresh_date(benchmark.index, asof)
    if refresh_date is None:
        raise RuntimeError("cannot determine anchored factor refresh date")

    snapshots = {}
    for code, frame in history.items():
        if code == config.BENCHMARK:
            continue
        snapshot = symbol_snapshot(frame, refresh_date)
        if snapshot is not None and snapshot["date"] == asof:
            snapshots[code] = snapshot
    ranked_codes = set(top_factor_codes(snapshots))
    market_ok = market_is_open_for_entries(benchmark)

    owned = checkpoint.get("owned", {})
    cooldowns = checkpoint.get("cooldowns", {})
    if checkpoint.get("last_plan_date") != asof:
        for code in list(cooldowns):
            remaining = int(cooldowns.get(code, 0) or 0) - 1
            if remaining > 0:
                cooldowns[code] = remaining
            else:
                del cooldowns[code]
    exits = []
    for code, state in list(owned.items()):
        snapshot = snapshots.get(code)
        if snapshot is None:
            continue
        raw_close = snapshot["raw_close"]
        entry_price = float(state.get("entry_price", 0.0) or 0.0)
        bars_held = int(state.get("bars_held", 0) or 0) + 1
        state["bars_held"] = bars_held
        state["peak_price"] = max(
            float(state.get("peak_price", 0.0) or 0.0), raw_close)
        pnl = raw_close / entry_price - 1.0 if entry_price > 0 else 0.0
        stop = (config.EARLY_STOP_PCT if bars_held <= config.EARLY_STOP_DAYS
                else config.HARD_STOP_PCT)
        reason = ""
        if pnl <= stop:
            reason = "stop"
        elif bars_held >= config.MAX_HOLD_BARS:
            reason = "max-hold"
        elif not market_ok:
            reason = "market-defence"
        if reason:
            exits.append({
                "side": "SELL",
                "code": code,
                "shares": int(state.get("shares", 0) or 0),
                "reason": reason,
                "reference_price": raw_close,
            })

    expected_held = max(0, len(owned) - len(exits))
    slots = config.MAX_POSITIONS - expected_held
    entries = []
    if market_ok and slots > 0:
        eligible = {}
        for code in ranked_codes:
            snapshot = snapshots.get(code)
            if snapshot is None or not snapshot["eligible"]:
                continue
            if code in owned:
                continue
            if account["positions"].get(code, {}).get("shares", 0) > 0:
                logger.info(
                    "[ENTRY-SKIP] %s is already held outside A144 ownership",
                    code)
                continue
            if int(cooldowns.get(code, 0) or 0) > 0:
                continue
            eligible[code] = snapshot
        ordered = sorted(
            eligible, key=lambda code: eligible[code]["factor"], reverse=True)
        selected_codes = ordered[:min(slots, config.MAX_NEW_ENTRIES_PER_DAY)]
        selected = {code: eligible[code] for code in selected_codes}
        equity = account["total_asset"] or account["cash"]
        allocations = normalized_inverse_atr_allocations(selected, equity)
        remaining_cash = account["cash"]
        for code in selected_codes:
            reference_price = selected[code]["raw_close"]
            allocation = min(allocations[code], remaining_cash * 0.98)
            shares = whole_lot_shares(allocation, reference_price)
            if shares < config.TRADE_LOT_SIZE:
                continue
            entries.append({
                "side": "BUY",
                "code": code,
                "shares": shares,
                "reason": "alpha144-breakout",
                "reference_price": reference_price,
                "factor": selected[code]["factor"],
                "atr_pct": selected[code]["atr_pct"],
                "allocation": allocation,
            })
            remaining_cash -= shares * reference_price

    plan = {
        "strategy": config.STRATEGY_NAME,
        "signal_date": asof,
        "execute_after": asof,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "refresh_date": refresh_date,
        "market_ok": market_ok,
        "universe_count": len(snapshots),
        "ranked_count": len(ranked_codes),
        "orders": exits + entries,
        "executed": False,
    }
    logger.info(
        "[PLAN] asof=%s refresh=%s market=%s universe=%s top=%s "
        "sell=%s buy=%s", asof, refresh_date, market_ok, len(snapshots),
        len(ranked_codes), len(exits), len(entries))
    for order in plan["orders"]:
        logger.info(
            "[PLAN-ORDER] %s %s %s sh ref=%.2f reason=%s",
            order["side"], order["code"], order["shares"],
            order["reference_price"], order["reason"])
    return plan


def execute_plan(adapter, plan, checkpoint, logger):
    today = datetime.now().strftime("%Y%m%d")
    current_time = datetime.now().strftime("%H:%M:%S")
    market_open = (
        "09:30:00" <= current_time <= "11:30:00" or
        "13:00:00" <= current_time <= "14:55:00")
    if not market_open:
        raise RuntimeError("orders may only execute during the configured session")
    if plan.get("strategy") != config.STRATEGY_NAME:
        raise RuntimeError("plan belongs to another strategy")
    if plan.get("executed"):
        raise RuntimeError("plan has already been executed")
    if str(plan.get("signal_date", "")) >= today:
        raise RuntimeError("plan may only execute on a later trading day")
    account = AccountView(adapter).snapshot()
    owned = checkpoint.setdefault("owned", {})
    for index, order in enumerate(plan.get("orders", []), 1):
        code = order["code"]
        side = order["side"]
        shares = int(order["shares"])
        if side == "SELL":
            if code not in owned:
                logger.error("[BLOCK] refusing to sell unowned symbol %s", code)
                continue
            sellable = account["positions"].get(code, {}).get("sellable", 0)
            shares = min(shares, sellable)
        tick = adapter.tick(code)
        price = tick["ask1"] if side == "BUY" else tick["bid1"]
        if price <= 0:
            price = tick["price"]
        if shares <= 0 or price <= 0:
            logger.warning("[SKIP] %s %s invalid shares/price", side, code)
            continue
        remark = "A144-{}-{}-{:02d}".format(
            plan["signal_date"], side[0], index)
        result = adapter.submit(
            side, code, shares, price, remark,
            allow_odd_lot=(side == "SELL"))
        filled = int(result.get("filled_shares", 0) or 0)
        average_price = float(result.get("average_price", 0.0) or 0.0)
        logger.info(
            "[EXECUTION] %s %s requested=%s filled=%s avg=%.2f status=%s",
            side, code, shares, filled, average_price, result.get("status"))
        if filled <= 0:
            continue
        if side == "BUY":
            owned[code] = {
                "shares": filled,
                "entry_price": average_price,
                "entry_date": today,
                "bars_held": 0,
                "peak_price": average_price,
            }
        else:
            state = owned[code]
            remaining = max(0, int(state.get("shares", 0)) - filled)
            if remaining:
                state["shares"] = remaining
            else:
                reason = order.get("reason", "sell")
                days = (config.STOP_COOLDOWN if reason == "stop"
                        else config.ALL_SELL_COOLDOWN)
                checkpoint.setdefault("cooldowns", {})[code] = days
                del owned[code]
    plan["executed"] = True
    plan["executed_at"] = datetime.now().isoformat(timespec="seconds")
    checkpoint["last_execution_date"] = today
    save_json(PLAN_PATH, plan)
    save_json(CHECKPOINT_PATH, checkpoint)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "check"))
    parser.add_argument("--mode", choices=("signal", "live"), default="signal")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    logger = build_logger(
        "A144_v1", Path(__file__).resolve().parent / "logs")
    live = args.mode == "live"
    adapter = RedisQmtAdapter(
        live=live, account_id=config.ACCOUNT,
        strategy_name=config.STRATEGY_NAME)
    if args.action == "check":
        account = AccountView(adapter).snapshot()
        print(json.dumps({
            "account": config.ACCOUNT,
            "cash": account["cash"],
            "positions": len(account["positions"]),
        }, ensure_ascii=False, indent=2))
        return 0
    checkpoint = load_json(CHECKPOINT_PATH, empty_checkpoint())
    if args.action == "plan":
        market = MarketData(adapter.xtdata, logger)
        universe = market.universe()
        if not universe:
            raise RuntimeError("RedisQMT returned an empty CSI500 universe")
        history = market.history(universe)
        account = AccountView(adapter).snapshot()
        plan = build_plan(history, account, checkpoint, logger)
        today = datetime.now().strftime("%Y%m%d")
        current_time = datetime.now().strftime("%H:%M:%S")
        if plan["signal_date"] == today and current_time < "15:05:00":
            raise RuntimeError(
                "today's daily bar is incomplete; run plan after 15:05")
        save_json(PLAN_PATH, plan)
        checkpoint["last_plan_date"] = plan["signal_date"]
        save_json(CHECKPOINT_PATH, checkpoint)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not live or args.confirm != "LIVE":
        raise RuntimeError(
            "execution requires --mode live --confirm LIVE")
    plan = load_json(PLAN_PATH, None)
    if plan is None:
        raise RuntimeError("pending plan does not exist")
    execute_plan(adapter, plan, checkpoint, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
