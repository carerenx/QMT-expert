"""A144 v4 runtime: normal reverse-T buyback or candidate replacement."""
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
from Stragety.RedisQMT.A144.live_runtime import MarketData
from Stragety.RedisQMT.A144.live_runtime import empty_checkpoint
from Stragety.RedisQMT.A144.live_runtime import load_json
from Stragety.RedisQMT.A144.live_runtime import run as run_daily
from Stragety.RedisQMT.A144.live_runtime import save_json
from Stragety.RedisQMT.A144.model import anchored_refresh_date
from Stragety.RedisQMT.A144.model import market_is_open_for_entries
from Stragety.RedisQMT.A144.model import symbol_snapshot
from Stragety.RedisQMT.A144.model import top_factor_codes
from Stragety.RedisQMT.A144.replacement_t_overlay import PHASE_BLOCKED
from Stragety.RedisQMT.A144.replacement_t_overlay import PHASE_DONE
from Stragety.RedisQMT.A144.replacement_t_overlay import PHASE_REPLACED
from Stragety.RedisQMT.A144.replacement_t_overlay import atr_percent
from Stragety.RedisQMT.A144.replacement_t_overlay import has_open_replacement_legs
from Stragety.RedisQMT.A144.replacement_t_overlay import new_symbol_state
from Stragety.RedisQMT.A144.replacement_t_overlay import next_action
from Stragety.RedisQMT.A144.replacement_t_overlay import record_buy_fill
from Stragety.RedisQMT.A144.replacement_t_overlay import record_sell_fill
from Stragety.RedisQMT.A144.replacement_t_overlay import replacement_buy_shares
from Stragety.RedisQMT.A144.replacement_t_overlay import reverse_t_sell_shares
from Stragety.RedisQMT.A144.replacement_t_overlay import transfer_replacement_ownership
from Stragety.RedisQMT.A144.replacement_t_overlay import unlock_single_member_replacement_groups


def runtime_paths(strategy_file, config):
    strategy_path = Path(strategy_file).resolve()
    state_dir = strategy_path.parent / "state"
    version_tag = config.STRATEGY_NAME.lower()
    return {
        "base_checkpoint": state_dir / "checkpoint_{}.json".format(
            version_tag),
        "base_plan": state_dir / "pending_plan_{}.json".format(version_tag),
        "dayt": state_dir / "dayt_checkpoint_{}.json".format(version_tag),
        "v3_checkpoint": state_dir / "checkpoint_redisqmt_a144_v3.json",
        "v3_dayt": state_dir / "dayt_checkpoint_redisqmt_a144_v3.json",
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


def replacement_candidates(
        history,
        account,
        checkpoint,
        config,
        today,
        excluded_codes=None):
    complete = {}
    for code, frame in history.items():
        trimmed = frame.loc[frame.index.astype(str) < str(today)].copy()
        if len(trimmed):
            complete[code] = trimmed
    benchmark = complete.get(config.BENCHMARK)
    if benchmark is None or len(benchmark) < config.MIN_HISTORY_BARS:
        return []
    if not market_is_open_for_entries(benchmark):
        return []
    asof = str(benchmark.index[-1])
    refresh_date = anchored_refresh_date(
        benchmark.index,
        asof,
        anchor=config.INITIAL_RESEARCH_ANCHOR,
        interval=config.REFRESH_INTERVAL)
    if refresh_date is None:
        return []
    snapshots = {}
    for code, frame in complete.items():
        if code == config.BENCHMARK:
            continue
        snapshot = symbol_snapshot(frame, refresh_date)
        if snapshot is not None and snapshot["date"] == asof:
            snapshots[code] = snapshot
    ranked = set(top_factor_codes(
        snapshots,
        top_pct=config.FACTOR_TOP_PCT))
    excluded = set(excluded_codes or [])
    excluded.update(checkpoint.get("owned", {}))
    cooldowns = checkpoint.get("cooldowns", {})
    candidates = []
    for code in ranked:
        snapshot = snapshots.get(code)
        if snapshot is None or not snapshot.get("eligible"):
            continue
        if code in excluded:
            continue
        if int(cooldowns.get(code, 0) or 0) > 0:
            continue
        if int(account["positions"].get(code, {}).get("shares", 0) or 0) > 0:
            continue
        candidates.append({
            "code": code,
            "factor": float(snapshot["factor"]),
            "reference_price": float(snapshot["raw_close"]),
            "signal_date": asof,
        })
    candidates.sort(key=lambda item: item["factor"], reverse=True)
    return candidates[:config.DAYT_REPLACEMENT_POOL_LIMIT]


class CandidateReplacementOverlay(object):
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
        self.pool_cache_date = ""
        self.pool_cache = []
        if self.observe_only:
            self.state = empty_dayt_state(config)
        else:
            self.state = load_json(paths["dayt"], empty_dayt_state(config))
        if self.state.get("strategy") != config.STRATEGY_NAME:
            raise RuntimeError("replacement-T state belongs to another strategy")

    def _save(self):
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if not self.observe_only:
            save_json(self.paths["dayt"], self.state)

    def _save_checkpoint(self, checkpoint):
        if not self.observe_only:
            save_json(self.paths["base_checkpoint"], checkpoint)

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
            "fraction=%.0f%% cutoff=%s replacement=%s",
            code,
            open_price,
            atr_pct * 100.0,
            state["sell_trigger"],
            self.config.DAYT_SELL_FRACTION * 100.0,
            self.config.DAYT_NEW_SELL_CUTOFF,
            self.config.DAYT_REPLACEMENT_TIME)
        return state

    def _submit_trade(self, code, side, shares, tick, state, today, reason):
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
            return False
        order_sequence = int(state.get("order_sequence", 0)) + 1
        state["order_sequence"] = order_sequence
        if self.observe_only:
            result = {
                "order_id": "observe",
                "status": "OBSERVED",
                "filled_shares": shares,
                "average_price": price,
            }
        else:
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
            return False
        order_id = result.get("order_id")
        if side == "SELL":
            record_sell_fill(
                state,
                filled,
                average_price,
                order_id,
                self.config)
            self.logger.info(
                "[%s][NEXT-PLAN] normal buyback=%s sh sold=%.2f "
                "target=%.2f bounce=%.2f%%; otherwise candidate at %s",
                code,
                filled,
                average_price,
                state["buyback_target"],
                self.config.DAYT_BUYBACK_BOUNCE_PCT * 100.0,
                self.config.DAYT_REPLACEMENT_TIME)
        else:
            record_buy_fill(state, filled, average_price, order_id)
            if int(state.get("sold_shares", 0)) == 0:
                self.state.setdefault("ledger", []).append({
                    "date": today,
                    "code": code,
                    "outcome": "normal-buyback",
                    "shares": state.get("initial_sold_shares", filled),
                    "sell_price": state.get("sell_price", 0.0),
                    "buy_price": average_price,
                    "gross_spread": state.get("gross_spread", 0.0),
                    "reason": reason,
                })
                self.logger.info(
                    "[%s][REV-T DONE] normal buyback=%s sh price=%.2f "
                    "gross-spread=%.2f",
                    code,
                    filled,
                    average_price,
                    state.get("gross_spread", 0.0))
        return True

    def _load_pool(self, today, account, checkpoint):
        if self.pool_cache_date == today:
            return list(self.pool_cache)
        market = MarketData(self.adapter.xtdata, self.config)
        universe = market.universe()
        if not universe:
            self.logger.error("[REPLACE-BLOCK] empty A144 universe")
            return []
        history = market.history(universe)
        occupied_replacements = {
            str(state.get("replacement_code"))
            for state in self.state.get("symbols", {}).values()
            if state.get("replacement_code")
        }
        self.pool_cache = replacement_candidates(
            history,
            account,
            checkpoint,
            self.config,
            today,
            excluded_codes=occupied_replacements)
        self.pool_cache_date = today
        self.logger.info(
            "[REPLACE-POOL] date=%s candidates=%s",
            today,
            len(self.pool_cache))
        return list(self.pool_cache)

    def _replace(self, source_code, state, today, account, checkpoint):
        pool = self._load_pool(today, account, checkpoint)
        budget = (
            float(state.get("sell_price", 0.0)) *
            int(state.get("sold_shares", 0) or 0))
        if budget <= 0:
            self.logger.error("[%s][REPLACE-BLOCK] invalid sale budget", source_code)
            return False
        candidate = None
        candidate_tick = None
        candidate_shares = 0
        for item in pool:
            code = item["code"]
            if code in checkpoint.get("owned", {}):
                continue
            if int(account["positions"].get(code, {}).get("shares", 0) or 0) > 0:
                continue
            tick = self.adapter.tick(code)
            price = float(tick.get("ask1", 0.0) or tick.get("price", 0.0) or 0.0)
            shares = replacement_buy_shares(
                budget,
                account["cash"],
                price,
                self.config.DAYT_REPLACEMENT_CASH_USAGE,
                self.config.TRADE_LOT_SIZE)
            if shares >= self.config.TRADE_LOT_SIZE:
                candidate = item
                candidate_tick = tick
                candidate_shares = shares
                break
        if candidate is None:
            self.logger.warning(
                "[%s][REPLACE-WAIT] no eligible affordable candidate; "
                "cash vacancy remains %.2f",
                source_code,
                budget)
            state["last_event"] = "waiting for replacement candidate"
            return False
        replacement_code = candidate["code"]
        price = float(
            candidate_tick.get("ask1", 0.0) or
            candidate_tick.get("price", 0.0) or 0.0)
        order_sequence = int(state.get("order_sequence", 0)) + 1
        state["order_sequence"] = order_sequence
        if self.observe_only:
            result = {
                "order_id": "observe-replacement",
                "status": "OBSERVED",
                "filled_shares": candidate_shares,
                "average_price": price,
            }
        else:
            remark = order_remark(
                self.config,
                today,
                replacement_code,
                "BUY",
                order_sequence)
            result = self.adapter.submit(
                "BUY",
                replacement_code,
                candidate_shares,
                price,
                remark,
                allow_odd_lot=False)
        filled = int(result.get("filled_shares", 0) or 0)
        average_price = float(result.get("average_price", 0.0) or 0.0)
        self.logger.info(
            "[%s][REPLACE-EXECUTION] candidate=%s requested=%s filled=%s "
            "avg=%.2f status=%s budget=%.2f",
            source_code,
            replacement_code,
            candidate_shares,
            filled,
            average_price,
            result.get("status"),
            budget)
        if filled <= 0:
            state["last_event"] = "replacement order unfilled"
            return False
        sold_shares = int(state.get("sold_shares", 0) or 0)
        group = transfer_replacement_ownership(
            checkpoint,
            source_code,
            replacement_code,
            sold_shares,
            filled,
            average_price,
            today)
        state["replacement_code"] = replacement_code
        state["replacement_shares"] = filled
        state["replacement_price"] = average_price
        state["replacement_order_id"] = result.get("order_id")
        state["replacement_group"] = group
        state["sold_shares"] = 0
        state["phase"] = PHASE_REPLACED
        state["last_event"] = "candidate replacement completed"
        account["cash"] = max(
            0.0,
            float(account.get("cash", 0.0)) - filled * average_price)
        account.setdefault("positions", {})[replacement_code] = {
            "shares": filled,
            "sellable": 0,
        }
        self.state.setdefault("ledger", []).append({
            "date": today,
            "code": source_code,
            "outcome": "candidate-replacement",
            "sold_shares": sold_shares,
            "sell_price": state.get("sell_price", 0.0),
            "replacement_code": replacement_code,
            "replacement_shares": filled,
            "replacement_price": average_price,
            "cash_residual": max(
                0.0,
                budget - filled * average_price),
            "group": group,
        })
        self._save_checkpoint(checkpoint)
        self.logger.info(
            "[%s][REPLACED] sold=%s sh -> %s %s sh; "
            "emergency/forced original buyback disabled",
            source_code,
            sold_shares,
            replacement_code,
            filled)
        return True

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
        unlocked = unlock_single_member_replacement_groups(owned)
        if unlocked:
            self.logger.info("[REPLACE-UNLOCK] eligible again: %s", unlocked)
            self._save_checkpoint(checkpoint)
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
        has_open_leg = any(
            int(state.get("sold_shares", 0) or 0) > 0
            for state in symbol_states.values())
        prepare_time = self.config.DAYT_REPLACEMENT_PREPARE_TIME
        replacement_time = self.config.DAYT_REPLACEMENT_TIME
        if (has_open_leg and
                prepare_time <= now_hms < replacement_time and
                self.pool_cache_date != today):
            self.logger.info(
                "[REPLACE-PREPARE] preloading candidate pool before %s",
                replacement_time)
            self._load_pool(today, account, checkpoint)
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
                action = {"side": "REPLACE", "reason": "overnight-replacement"}
            else:
                action = next_action(state, price, now_hms, self.config)
            if action is None:
                continue
            if action["side"] == "REPLACE":
                self._replace(code, state, today, account, checkpoint)
                continue
            if action["side"] == "SELL":
                if code in exit_codes:
                    state["phase"] = PHASE_BLOCKED
                    state["last_event"] = "base exit plan owns sell priority"
                    self.logger.info(
                        "[%s][DAYT-BLOCK] pending A144 base exit", code)
                    continue
                owned_state = owned.get(code) or {}
                if owned_state.get("replacement_group"):
                    state["phase"] = PHASE_DONE
                    state["last_event"] = "replacement group blocks resplitting"
                    self.logger.info(
                        "[%s][DAYT-BLOCK] active replacement group=%s",
                        code,
                        owned_state.get("replacement_group"))
                    continue
                owned_shares = int(owned_state.get("shares", 0) or 0)
                entry_date = str(owned_state.get("entry_date", "") or "")
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
            self._submit_trade(
                code,
                action["side"],
                shares,
                tick,
                state,
                today,
                action["reason"])
        self._save()


def migrate_checkpoint(adapter, config, paths, source):
    source_path = paths["{}_checkpoint".format(source)]
    old = load_json(source_path, None)
    if old is None:
        raise RuntimeError("{} checkpoint does not exist".format(source))
    if source == "v3":
        old_dayt = load_json(paths["v3_dayt"], {})
        if has_open_replacement_legs(old_dayt):
            raise RuntimeError("v3 has an open reverse-T leg; migrate after reconciliation")
    current = load_json(paths["base_checkpoint"], empty_checkpoint(config))
    if current.get("owned"):
        raise RuntimeError("v4 checkpoint already owns positions")
    account = AccountView(adapter).snapshot()
    migrated_owned = {}
    for code, state in old.get("owned", {}).items():
        expected = int(state.get("shares", 0) or 0)
        actual = int(account["positions"].get(code, {}).get("shares", 0) or 0)
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
        "migrated_from": old.get("strategy", source),
        "migrated_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_json(paths["base_checkpoint"], current)
    return current


def ensure_dayt_mode_allowed(config, action, mode):
    if (action == "dayt" and mode == "live" and
            not bool(getattr(config, "DAYT_LIVE_ENABLED", False))):
        raise RuntimeError(
            "v4 live DayT is disabled by the production gate; "
            "use dayt --mode signal, or run daily plan/execute")


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
        if has_open_replacement_legs(dayt_state):
            raise RuntimeError(
                "open replacement leg exists; base plan/execute is blocked")
        return run_daily(config, strategy_file, argv=arguments)
    if action == "check":
        return run_daily(config, strategy_file, argv=arguments)

    parser = argparse.ArgumentParser(description="A144 v4 candidate replacement")
    parser.add_argument(
        "action",
        choices=("dayt", "dayt-status", "migrate-v2", "migrate-v3"))
    parser.add_argument("--mode", choices=("signal", "live"), default="signal")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(arguments)
    logger = build_logger(config.STRATEGY_NAME, paths["log_directory"])
    if args.action == "dayt-status":
        payload = load_json(paths["dayt"], empty_dayt_state(config))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    ensure_dayt_mode_allowed(config, args.action, args.mode)
    adapter = RedisQmtAdapter(
        live=args.mode == "live",
        account_id=config.ACCOUNT,
        strategy_name=config.STRATEGY_NAME)
    if args.action in ("migrate-v2", "migrate-v3"):
        if args.mode != "live" or args.confirm != "MIGRATE":
            raise RuntimeError("migration requires --mode live --confirm MIGRATE")
        source = args.action.split("-")[1]
        payload = migrate_checkpoint(adapter, config, paths, source)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "live" and args.confirm != "LIVE":
        raise RuntimeError("live DayT requires --mode live --confirm LIVE")
    overlay = CandidateReplacementOverlay(
        adapter,
        config,
        logger,
        paths,
        observe_only=args.mode != "live")
    if args.once:
        overlay.cycle()
        return 0
    logger.info(
        "[DAYT-START] mode=%s normal-buyback-or-candidate fraction=%.0f%%",
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
