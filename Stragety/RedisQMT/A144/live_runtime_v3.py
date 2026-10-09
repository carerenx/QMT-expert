"""A144 v3 runtime that composes the daily strategy with a reverse-T overlay."""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

from Stragety.RedisQMT.A144.live_runtime import AccountView
from Stragety.RedisQMT.A144.live_runtime import empty_checkpoint
from Stragety.RedisQMT.A144.live_runtime import load_json
from Stragety.RedisQMT.A144.live_runtime import run as run_daily
from Stragety.RedisQMT.A144.live_runtime import save_json
from Stragety.RedisQMT.A144.reverse_t_overlay import PHASE_BLOCKED
from Stragety.RedisQMT.A144.reverse_t_overlay import PHASE_DONE
from Stragety.RedisQMT.A144.reverse_t_overlay import has_open_reverse_t_legs
from Stragety.RedisQMT.A144.reverse_t_overlay import atr_percent
from Stragety.RedisQMT.A144.reverse_t_overlay import new_symbol_state
from Stragety.RedisQMT.A144.reverse_t_overlay import next_action
from Stragety.RedisQMT.A144.reverse_t_overlay import record_buy_fill
from Stragety.RedisQMT.A144.reverse_t_overlay import record_sell_fill
from Stragety.RedisQMT.A144.reverse_t_overlay import reverse_t_sell_shares


def runtime_paths(strategy_file, config):
    strategy_path = Path(strategy_file).resolve()
    state_dir = strategy_path.parent / "state"
    version_tag = config.STRATEGY_NAME.lower()
    return {
        "base_checkpoint": state_dir / "checkpoint_{}.json".format(
            version_tag),
        "base_plan": state_dir / "pending_plan_{}.json".format(version_tag),
        "dayt": state_dir / "dayt_checkpoint_{}.json".format(version_tag),
        "v2_checkpoint": state_dir / "checkpoint_redisqmt_a144_v2.json",
        "log_directory": strategy_path.parent / "logs",
    }


def empty_dayt_state(config):
    return {
        "strategy": config.STRATEGY_NAME,
        "updated_at": "",
        "symbols": {},
        "ledger": [],
    }


def normalize_date(value):
    return "".join(
        character for character in str(value)
        if character.isdigit())[:8]


def complete_daily_history(adapter, code, config, today):
    data = adapter.xtdata.get_market_data_ex(
        ["high", "low", "close"],
        [code],
        period="1d",
        count=config.DAYT_HISTORY_BARS,
        dividend_type="none",
        fill_data=False,
        timeout_seconds=config.HISTORY_TIMEOUT_SECONDS,
        chunk_size=1)
    frame = (data or {}).get(code)
    if frame is None or len(frame) == 0:
        return None
    frame = frame.copy()
    frame.index = [normalize_date(value) for value in frame.index]
    if str(frame.index[-1]) == str(today):
        frame = frame.iloc[:-1]
    for column in ("high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["high", "low", "close"])


def pending_exit_codes(plan):
    if not plan or plan.get("executed"):
        return set()
    return {
        str(order.get("code"))
        for order in plan.get("orders", [])
        if order.get("side") == "SELL"
    }


def order_remark(config, today, code, side, order_sequence):
    symbol = str(code).split(".")[0]
    return "{}T-{}-{}-{}-{:02d}".format(
        config.ORDER_REMARK_PREFIX,
        today,
        symbol,
        side[0],
        int(order_sequence))


class ReverseTOverlay(object):
    def __init__(
            self,
            adapter,
            config,
            logger,
            paths,
            observe_only=False):
        self.adapter = adapter
        self.config = config
        self.logger = logger
        self.paths = paths
        self.observe_only = bool(observe_only)
        if self.observe_only:
            self.state = empty_dayt_state(config)
        else:
            self.state = load_json(paths["dayt"], empty_dayt_state(config))
        if self.state.get("strategy") != config.STRATEGY_NAME:
            raise RuntimeError("reverse-T state belongs to another strategy")

    def _save(self):
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if not self.observe_only:
            save_json(self.paths["dayt"], self.state)

    def _initialize_symbol(self, code, tick, today):
        history = complete_daily_history(
            self.adapter,
            code,
            self.config,
            today)
        atr_pct = atr_percent(history, self.config.DAYT_ATR_PERIOD)
        open_price = float(tick.get("open", 0.0) or 0.0)
        if open_price <= 0:
            open_price = float(tick.get("last_close", 0.0) or 0.0)
        if atr_pct is None or open_price <= 0:
            self.logger.warning(
                "[DAYT-BLOCK] %s missing complete daily ATR/open", code)
            return None
        state = new_symbol_state(today, open_price, atr_pct, self.config)
        self.state.setdefault("symbols", {})[code] = state
        self.logger.info(
            "[%s][REV-T PLAN] open=%.2f ATR=%.2f%% sell=%.2f "
            "fraction=%.0f%% cutoff=%s force=%s",
            code,
            open_price,
            atr_pct * 100.0,
            state["sell_trigger"],
            self.config.DAYT_SELL_FRACTION * 100.0,
            self.config.DAYT_NEW_SELL_CUTOFF,
            self.config.DAYT_FORCE_BUYBACK_TIME)
        return state

    def _submit(self, code, side, shares, tick, state, today, reason):
        price = float(
            tick.get("bid1", 0.0) if side == "SELL"
            else tick.get("ask1", 0.0))
        if price <= 0:
            price = float(tick.get("price", 0.0) or 0.0)
        if price <= 0 or shares <= 0:
            self.logger.warning(
                "[%s][DAYT-SKIP] %s invalid shares=%s price=%.2f",
                code,
                side,
                shares,
                price)
            return
        if self.observe_only:
            result = {
                "order_id": "observe",
                "status": "OBSERVED",
                "filled_shares": shares,
                "average_price": price,
            }
        else:
            order_sequence = int(state.get("order_sequence", 0)) + 1
            state["order_sequence"] = order_sequence
            remark = order_remark(
                self.config,
                today,
                code,
                side,
                order_sequence)
            result = self.adapter.submit(
                side,
                code,
                shares,
                price,
                remark,
                allow_odd_lot=False)
        filled = int(result.get("filled_shares", 0) or 0)
        average_price = float(result.get("average_price", 0.0) or 0.0)
        self.logger.info(
            "[%s][DAYT-EXECUTION] %s requested=%s filled=%s avg=%.2f "
            "status=%s reason=%s",
            code,
            side,
            shares,
            filled,
            average_price,
            result.get("status"),
            reason)
        if filled <= 0:
            return
        order_id = result.get("order_id")
        if side == "SELL":
            record_sell_fill(
                state,
                filled,
                average_price,
                order_id,
                self.config)
            self.logger.info(
                "[%s][NEXT-PLAN] buyback %s sh sold=%.2f target=%.2f "
                "bounce=%.2f%% emergency=+%.2f%% force=%s",
                code,
                filled,
                average_price,
                state["buyback_target"],
                self.config.DAYT_BUYBACK_BOUNCE_PCT * 100.0,
                self.config.DAYT_EMERGENCY_RISE_PCT * 100.0,
                self.config.DAYT_FORCE_BUYBACK_TIME)
        else:
            record_buy_fill(state, filled, average_price, order_id)
            if int(state.get("sold_shares", 0)) == 0:
                self.state.setdefault("ledger", []).append({
                    "date": today,
                    "code": code,
                    "shares": state.get("initial_sold_shares", filled),
                    "sell_price": state.get("sell_price", 0.0),
                    "buy_price": average_price,
                    "gross_spread": state.get("gross_spread", 0.0),
                    "reason": reason,
                })
                self.logger.info(
                    "[%s][REV-T DONE] buyback=%s sh price=%.2f "
                    "gross-spread=%.2f",
                    code,
                    filled,
                    average_price,
                    state.get("gross_spread", 0.0))

    def cycle(self, now=None):
        now = now or datetime.now()
        today = now.strftime("%Y%m%d")
        now_hms = now.strftime("%H:%M:%S")
        account = AccountView(self.adapter).snapshot()
        checkpoint = load_json(
            self.paths["base_checkpoint"],
            empty_checkpoint(self.config))
        if checkpoint.get("strategy") != self.config.STRATEGY_NAME:
            raise RuntimeError("A144 base checkpoint belongs to another strategy")
        owned = checkpoint.setdefault("owned", {})
        plan = load_json(self.paths["base_plan"], None)
        exit_codes = pending_exit_codes(plan)
        symbol_states = self.state.setdefault("symbols", {})
        active_codes = set(owned)
        active_codes.update(
            code for code, state in symbol_states.items()
            if int(state.get("sold_shares", 0) or 0) > 0)
        if not active_codes:
            self.logger.info("[DAYT] no A144-owned symbols")
            return
        for code in sorted(active_codes):
            tick = self.adapter.tick(code)
            price = float(tick.get("price", 0.0) or 0.0)
            if price <= 0:
                self.logger.warning("[%s][DAYT-BLOCK] invalid tick", code)
                continue
            state = symbol_states.get(code)
            open_leg = int((state or {}).get("sold_shares", 0) or 0) > 0
            if state is None or (state.get("date") != today and not open_leg):
                state = self._initialize_symbol(code, tick, today)
                if state is None:
                    continue
            if open_leg and state.get("date") != today:
                action = {"side": "BUY", "reason": "overnight-recovery"}
            else:
                action = next_action(state, price, now_hms, self.config)
            if action is None:
                continue
            if action["side"] == "SELL":
                if code in exit_codes:
                    state["phase"] = PHASE_BLOCKED
                    state["last_event"] = "base exit plan owns sell priority"
                    self.logger.info(
                        "[%s][DAYT-BLOCK] pending A144 base exit", code)
                    continue
                owned_shares = int(
                    (owned.get(code) or {}).get("shares", 0) or 0)
                entry_date = str(
                    (owned.get(code) or {}).get("entry_date", "") or "")
                if entry_date == today:
                    state["phase"] = PHASE_DONE
                    state["last_event"] = "T+1 entry is not eligible"
                    self.logger.info(
                        "[%s][DAYT-BLOCK] A144 position entered today", code)
                    continue
                position = account["positions"].get(code, {})
                actual_shares = int(position.get("shares", 0) or 0)
                sellable = int(position.get("sellable", 0) or 0)
                if actual_shares < owned_shares:
                    state["phase"] = PHASE_BLOCKED
                    state["last_event"] = "account below A144 ownership"
                    self.logger.error(
                        "[%s][DAYT-BLOCK] account=%s below A144-owned=%s",
                        code,
                        actual_shares,
                        owned_shares)
                    continue
                shares = reverse_t_sell_shares(
                    sellable,
                    owned_shares,
                    self.config.DAYT_SELL_FRACTION,
                    self.config.TRADE_LOT_SIZE)
                if shares < self.config.TRADE_LOT_SIZE:
                    state["phase"] = PHASE_DONE
                    state["last_event"] = "less than one sellable lot"
                    self.logger.info(
                        "[%s][DAYT-BLOCK] sellable=%s owned=%s < one lot",
                        code,
                        sellable,
                        owned_shares)
                    continue
            else:
                shares = int(state.get("sold_shares", 0) or 0)
            self._submit(
                code,
                action["side"],
                shares,
                tick,
                state,
                today,
                action["reason"])
        self._save()


def migrate_v2_checkpoint(adapter, config, paths):
    old = load_json(paths["v2_checkpoint"], None)
    if old is None:
        raise RuntimeError("v2 checkpoint does not exist")
    current = load_json(paths["base_checkpoint"], empty_checkpoint(config))
    if current.get("owned"):
        raise RuntimeError("v3 checkpoint already owns positions")
    account = AccountView(adapter).snapshot()
    migrated_owned = {}
    for code, state in old.get("owned", {}).items():
        expected = int(state.get("shares", 0) or 0)
        actual = int(
            account["positions"].get(code, {}).get("shares", 0) or 0)
        if expected <= 0 or actual < expected:
            raise RuntimeError(
                "cannot migrate {}: account={} expected={}".format(
                    code,
                    actual,
                    expected))
        migrated_owned[code] = dict(state)
    current.update({
        "strategy": config.STRATEGY_NAME,
        "last_plan_date": old.get("last_plan_date", ""),
        "last_execution_date": old.get("last_execution_date", ""),
        "owned": migrated_owned,
        "cooldowns": dict(old.get("cooldowns", {})),
        "reserved_slots": list(old.get("reserved_slots", [])),
        "migrated_from": old.get("strategy", "RedisQMT_A144_v2"),
        "migrated_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_json(paths["base_checkpoint"], current)
    return current


def run(config, strategy_file, argv=None):
    from Stragety.RedisQMT.Common.logger import build_logger
    from Stragety.RedisQMT.Common.redis_qmt import RedisQmtAdapter

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise RuntimeError("action is required")
    action = arguments[0]
    paths = runtime_paths(strategy_file, config)
    if action in ("plan", "execute"):
        dayt_state = load_json(paths["dayt"], empty_dayt_state(config))
        if has_open_reverse_t_legs(dayt_state):
            raise RuntimeError(
                "open reverse-T leg exists; base plan/execute is blocked")
        return run_daily(config, strategy_file, argv=arguments)
    if action == "check":
        return run_daily(config, strategy_file, argv=arguments)

    parser = argparse.ArgumentParser(description="A144 v3 reverse-T overlay")
    parser.add_argument(
        "action",
        choices=("dayt", "dayt-status", "migrate-v2"))
    parser.add_argument("--mode", choices=("signal", "live"), default="signal")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(arguments)
    logger = build_logger(config.STRATEGY_NAME, paths["log_directory"])
    if args.action == "dayt-status":
        payload = load_json(paths["dayt"], empty_dayt_state(config))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    adapter = RedisQmtAdapter(
        live=args.mode == "live",
        account_id=config.ACCOUNT,
        strategy_name=config.STRATEGY_NAME)
    if args.action == "migrate-v2":
        if args.mode != "live" or args.confirm != "MIGRATE":
            raise RuntimeError(
                "migration requires --mode live --confirm MIGRATE")
        payload = migrate_v2_checkpoint(adapter, config, paths)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "live" and args.confirm != "LIVE":
        raise RuntimeError("live DayT requires --mode live --confirm LIVE")
    overlay = ReverseTOverlay(
        adapter,
        config,
        logger,
        paths,
        observe_only=args.mode != "live")
    if args.once:
        overlay.cycle()
        return 0
    logger.info(
        "[DAYT-START] mode=%s reverse-only multi-symbol fraction=%.0f%%",
        args.mode,
        config.DAYT_SELL_FRACTION * 100.0)
    try:
        while True:
            now = datetime.now()
            now_hms = now.strftime("%H:%M:%S")
            market_open = bool(
                "09:30:00" <= now_hms <= "11:30:00" or
                "13:00:00" <= now_hms <= "15:00:00")
            if market_open:
                try:
                    overlay.cycle(now)
                except Exception:
                    logger.error("[DAYT-ERROR]\n%s", traceback.format_exc())
            elif now_hms > "15:05:00":
                logger.info("[DAYT-STOP] market closed")
                return 0
            time.sleep(config.DAYT_POLL_SECONDS)
    except KeyboardInterrupt:
        logger.info("[DAYT-STOP] interrupted by user")
        return 0
