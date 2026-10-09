"""Alpha144 v5 baseline reconstruction and walk-forward optimization.

The study uses the frozen RedisQMT daily panel and the current CSI500
constituent snapshot.  Signals are formed at the close and executed at the
next tradable open.  The implementation is intentionally independent of the
live strategy so the live code cannot silently change the research result.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PANEL_PATH = ROOT / "analysis" / "panel_20260920" / "panel.parquet"
UNIVERSE_PATH = ROOT / "analysis" / "panel_20260920" / "csi500_universe.json"
OUTPUT_DIR = ROOT / "analysis" / "a144_redisqmt_20260923"

BUY_COST = 0.00076
SELL_COST_BEFORE_20230828 = 0.00176
SELL_COST_AFTER_20230828 = 0.00126


@dataclass(frozen=True)
class Config:
    name: str
    score: str = "alpha_raw"
    alpha_weight: float = 1.0
    momentum_weight: float = 0.0
    quality_weight: float = 0.0
    factor_top_pct: float = 0.15
    refresh_days: int = 10
    refresh_offset: int = 0
    refresh_anchor: str = "20220104"
    rank_vote_window: int = 1
    rank_vote_min: int = 1
    breakout_days: int = 10
    breakout_strength: float = 0.005
    require_breakout: bool = True
    amount_ratio: float = 1.20
    trend_ma: int = 20
    require_price_above_ma: bool = True
    require_ma_slope: bool = True
    momentum_days: int = 3
    momentum_up_min: int = 2
    daily_return_limit: float | None = 0.098
    max_drop_window: int = 60
    max_drop_limit: float | None = -0.07
    max_consecutive_up: int | None = 3
    max_range_5: float | None = 0.10
    gap_down_limit: float | None = -0.03
    min_daily_amount: float = 3e7
    rank_min_daily_amount: float = 0.0
    entry_vote_min: int | None = None
    max_positions: int = 5
    max_entries_per_day: int = 2
    max_hold_days: int = 20
    early_stop: float = -0.12
    early_days: int = 3
    hard_stop: float = -0.18
    trailing_atr: float | None = None
    profit_lock_start: float | None = None
    profit_lock_keep: float = 0.50
    cooldown_days: int = 30
    stop_cooldown_days: int = 60
    market_mode: str = "v5"
    market_exit_mode: str = "legacy"
    market_severe_pct: float = 0.06
    market_weak_max_return: float = 0.0
    weak_exit_min_days: int | None = None
    weak_exit_max_return: float = -0.02
    weak_exit_require_below_ma: bool = True
    weak_exit_reserve_slot: bool = False
    risk_model: str = "v5"
    risk_budget_scale: float = 1.0
    risk_multiplier_min: float = 0.65
    risk_multiplier_max: float = 1.35


def load_panel() -> pd.DataFrame:
    universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))["codes"]
    columns = [
        "code", "time", "open", "high", "low", "close", "adj_open",
        "adj_high", "adj_low", "adj_close", "amount", "volume",
        "tradable", "cannot_buy", "cannot_sell", "isST", "listed_days",
    ]
    frame = pd.read_parquet(
        PANEL_PATH, columns=columns, filters=[("code", "in", universe)])
    frame = frame.rename(columns={"time": "date"})
    frame = frame[frame["date"] >= "20210101"].copy()
    frame = frame.sort_values(["code", "date"], kind="stable")
    return frame.reset_index(drop=True)


def add_features(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    grouped = frame.groupby("code", sort=False, group_keys=False)
    frame["ret"] = grouped["adj_close"].pct_change(fill_method=None)
    frame["prev_close"] = grouped["adj_close"].shift(1)

    negative_impact = np.where(
        frame["ret"] < 0,
        frame["ret"].abs() / frame["amount"].replace(0, np.nan),
        0.0,
    )
    frame["negative_impact"] = negative_impact
    frame["alpha_raw"] = grouped["negative_impact"].transform(
        lambda values: values.rolling(20, min_periods=20).sum())
    frame["alpha_smooth"] = grouped["alpha_raw"].transform(
        lambda values: values.rolling(10, min_periods=5).mean())

    frame["amount_median60"] = grouped["amount"].transform(
        lambda values: values.rolling(60, min_periods=40).median())
    frame["impact_normalized"] = np.where(
        frame["ret"] < 0,
        frame["ret"].abs() * frame["amount_median60"] /
        frame["amount"].replace(0, np.nan),
        0.0,
    )
    frame["alpha_normalized"] = grouped["impact_normalized"].transform(
        lambda values: values.rolling(20, min_periods=20).mean())

    frame["ma20"] = grouped["adj_close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean())
    frame["ma50"] = grouped["adj_close"].transform(
        lambda values: values.rolling(50, min_periods=50).mean())
    frame["ma20_lag5"] = grouped["ma20"].shift(5)
    frame["mom20"] = grouped["adj_close"].pct_change(20, fill_method=None)
    frame["mom60"] = grouped["adj_close"].pct_change(60, fill_method=None)
    frame["vol20"] = grouped["ret"].transform(
        lambda values: values.rolling(20, min_periods=20).std())

    previous = frame["prev_close"]
    true_range = pd.concat([
        frame["adj_high"] - frame["adj_low"],
        (frame["adj_high"] - previous).abs(),
        (frame["adj_low"] - previous).abs(),
    ], axis=1).max(axis=1)
    frame["atr20"] = true_range.groupby(frame["code"]).transform(
        lambda values: values.rolling(20, min_periods=20).mean())
    frame["atr_pct"] = frame["atr20"] / frame["adj_close"]

    frame["amount_prev5"] = grouped["amount"].transform(
        lambda values: values.shift(1).rolling(5, min_periods=5).mean())
    frame["amount_mean20"] = grouped["amount"].transform(
        lambda values: values.rolling(20, min_periods=20).mean())
    frame["range_pct"] = (
        (frame["adj_high"] - frame["adj_low"]) /
        frame["adj_close"].replace(0, np.nan))
    frame["max_range5"] = grouped["range_pct"].transform(
        lambda values: values.rolling(5, min_periods=5).max())
    frame["avg_range20"] = grouped["range_pct"].transform(
        lambda values: values.rolling(20, min_periods=20).mean())
    frame["min_ret20"] = grouped["ret"].transform(
        lambda values: values.rolling(20, min_periods=20).min())
    frame["min_ret60"] = grouped["ret"].transform(
        lambda values: values.rolling(60, min_periods=60).min())
    frame["up_prev1"] = grouped["ret"].shift(1) > 0
    frame["up_prev2"] = grouped["ret"].shift(2) > 0
    frame["up_prev3"] = grouped["ret"].shift(3) > 0
    frame["up_prev4"] = grouped["ret"].shift(4) > 0
    for days in (10, 15, 20, 30):
        frame["high{}".format(days)] = grouped["adj_close"].transform(
            lambda values, window=days: values.shift(1).rolling(
                window, min_periods=window).max())

    date_groups = frame.groupby("date", sort=False)
    frame["alpha_raw_pct"] = date_groups["alpha_raw"].rank(pct=True)
    frame["alpha_smooth_pct"] = date_groups["alpha_smooth"].rank(pct=True)
    frame["alpha_normalized_pct"] = date_groups["alpha_normalized"].rank(pct=True)
    frame["mom60_pct"] = date_groups["mom60"].rank(pct=True)
    frame["quality_pct"] = date_groups["vol20"].rank(
        pct=True, ascending=False)

    market = date_groups["ret"].mean().fillna(0.0)
    market_level = (1.0 + market).cumprod()
    market_ma20 = market_level.rolling(20, min_periods=20).mean()
    market_ma60 = market_level.rolling(60, min_periods=60).mean()
    breadth = date_groups.apply(
        lambda rows: float((rows["adj_close"] > rows["ma50"]).mean()),
        include_groups=False)
    market_frame = pd.DataFrame({
        "market_level": market_level,
        "market_ma20": market_ma20,
        "market_ma60": market_ma60,
        "breadth": breadth,
    })
    frame = frame.merge(market_frame, left_on="date", right_index=True,
                        how="left")
    return frame


def consecutive_up(rows: pd.DataFrame, limit: int | None) -> pd.Series:
    if limit is None:
        return pd.Series(False, index=rows.index)
    columns = ["up_prev{}".format(index) for index in range(1, limit + 1)]
    return rows[columns].all(axis=1)


def market_allows_entry(row: pd.Series, config: Config) -> bool:
    if config.market_mode == "off":
        return True
    if config.market_mode in ("v5", "v5_entry"):
        return bool(row["market_level"] >= row["market_ma20"] * 0.97)
    if config.market_mode == "regime":
        return bool(
            row["market_level"] >= row["market_ma60"] and
            row["breadth"] >= 0.45)
    if config.market_mode == "soft":
        return bool(
            row["market_level"] >= row["market_ma60"] * 0.98 and
            row["breadth"] >= 0.38)
    raise ValueError("unknown market mode: {}".format(config.market_mode))


def score_rows(rows: pd.DataFrame, config: Config) -> pd.Series:
    if config.score == "alpha_raw":
        return rows["alpha_raw_pct"]
    if config.score == "alpha_smooth":
        return rows["alpha_smooth_pct"]
    if config.score == "alpha_normalized":
        return rows["alpha_normalized_pct"]
    if config.score == "blend":
        return (
            rows["alpha_normalized_pct"] * config.alpha_weight +
            rows["mom60_pct"] * config.momentum_weight +
            rows["quality_pct"] * config.quality_weight)
    raise ValueError("unknown score: {}".format(config.score))


def factor_universe(rows: pd.DataFrame, config: Config) -> set[str]:
    rows = rows.copy()
    rows["score"] = score_rows(rows, config)
    valid = rows.dropna(subset=["score"])
    valid = valid[valid["listed_days"] >= 130]
    if config.rank_min_daily_amount > 0:
        valid = valid[valid["amount"] >= config.rank_min_daily_amount]
    if valid.empty:
        return set()
    score_cutoff = valid["score"].quantile(1.0 - config.factor_top_pct)
    return set(valid.loc[valid["score"] >= score_cutoff, "code"])


def voted_ranking(
        ranking_history: list[set[str]],
        minimum_votes: int) -> set[str]:
    if not ranking_history:
        return set()
    vote_counts = {}
    for ranked_set in ranking_history:
        for code in ranked_set:
            vote_counts[code] = vote_counts.get(code, 0) + 1
    required_votes = min(int(minimum_votes), len(ranking_history))
    return {
        code for code, votes in vote_counts.items()
        if votes >= required_votes
    }


def ranking_schedule(
        total_dates: int,
        refresh_days: int,
        refresh_offset: int,
        start_index: int,
        history_window: int) -> tuple[list[int], int]:
    if refresh_days <= 0:
        raise ValueError("refresh_days must be positive")
    if refresh_offset < 0 or refresh_offset >= refresh_days:
        raise ValueError("refresh_offset must be within refresh interval")
    scheduled = list(range(refresh_offset, total_dates, refresh_days))
    initial = [
        index for index in scheduled
        if index <= start_index
    ][-history_window:]
    future = [
        index for index in scheduled
        if index > start_index
    ]
    next_index = future[0] if future else total_dates + 1
    return initial, next_index


def entry_candidates(rows: pd.DataFrame, config: Config,
                     ranked_codes: set[str]) -> pd.DataFrame:
    if rows.empty:
        return rows
    rows = rows.copy()
    rows["score"] = score_rows(rows, config)
    mask = rows["code"].isin(ranked_codes)
    mask &= rows["listed_days"] >= 130
    mask &= rows["tradable"]
    mask &= rows["isST"] != "1"
    mask &= rows["amount_mean20"] >= config.min_daily_amount
    if config.daily_return_limit is not None:
        mask &= rows["ret"] < config.daily_return_limit
    breakout = rows["high{}".format(config.breakout_days)]
    breakout_ok = rows["adj_close"] > breakout * (
        1.0 + config.breakout_strength)
    volume_ok = rows["amount"] >= rows["amount_prev5"] * config.amount_ratio
    trend_ma = rows["ma20"] if config.trend_ma == 20 else rows["ma50"]
    if config.require_price_above_ma:
        mask &= rows["adj_close"] > trend_ma
    if config.trend_ma == 20 and config.require_ma_slope:
        mask &= rows["ma20"] > rows["ma20_lag5"]
    momentum_ok = pd.Series(True, index=rows.index)
    if config.momentum_days == 3 and config.momentum_up_min > 0:
        recent_up = rows[["up_prev1", "up_prev2"]].sum(axis=1)
        current_up = rows["ret"] > 0
        momentum_ok = (
            recent_up + current_up.astype(int) >= config.momentum_up_min)
    elif config.momentum_up_min > 0:
        momentum_ok = rows["mom20"] > 0
    drop_ok = pd.Series(True, index=rows.index)
    if config.max_drop_limit is not None:
        drop_column = "min_ret{}".format(config.max_drop_window)
        drop_ok = rows[drop_column] > config.max_drop_limit
    consecutive_ok = pd.Series(True, index=rows.index)
    if config.max_consecutive_up is not None:
        consecutive_ok = ~consecutive_up(rows, config.max_consecutive_up)
    range_ok = pd.Series(True, index=rows.index)
    if config.max_range_5 is not None:
        range_ok = rows["max_range5"] <= config.max_range_5
    if config.entry_vote_min is None:
        if config.require_breakout:
            mask &= breakout_ok
        mask &= volume_ok
        mask &= momentum_ok
        mask &= drop_ok
        mask &= consecutive_ok
        mask &= range_ok
    else:
        vote_columns = []
        if config.require_breakout:
            vote_columns.append(breakout_ok)
        vote_columns.append(volume_ok)
        if config.momentum_up_min > 0:
            vote_columns.append(momentum_ok)
        if config.max_drop_limit is not None:
            vote_columns.append(drop_ok)
        if config.max_consecutive_up is not None:
            vote_columns.append(consecutive_ok)
        if config.max_range_5 is not None:
            vote_columns.append(range_ok)
        available_votes = len(vote_columns)
        required_votes = int(config.entry_vote_min)
        if required_votes < 1 or required_votes > available_votes:
            raise ValueError(
                "entry_vote_min must be between 1 and {}".format(
                    available_votes))
        vote_count = pd.concat(vote_columns, axis=1).sum(axis=1)
        mask &= vote_count >= required_votes
    if config.gap_down_limit is not None:
        gap = rows["adj_open"] / rows["prev_close"] - 1.0
        mask &= gap > config.gap_down_limit
    return rows.loc[mask].sort_values("score", ascending=False)


def sell_cost(date: str) -> float:
    if date >= "20230828":
        return SELL_COST_AFTER_20230828
    return SELL_COST_BEFORE_20230828


def run_backtest(frame: pd.DataFrame, config: Config,
                 start: str, end: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    dates = sorted(frame.loc[
        (frame["date"] >= start) & (frame["date"] <= end), "date"].unique())
    by_date = {date: rows.set_index("code") for date, rows in
               frame[frame["date"].isin(dates)].groupby("date", sort=False)}
    if not dates:
        raise ValueError("empty backtest interval")

    cash = 1_000_000.0
    positions = {}
    cooldown_until = {}
    pending_entries = []
    pending_exits = set()
    reserved_slots = []
    global_dates = sorted(frame.loc[
        (frame["date"] >= config.refresh_anchor) &
        (frame["date"] <= end),
        "date"].unique())
    if dates[0] not in global_dates:
        raise ValueError("backtest start is before refresh anchor")
    start_global_index = global_dates.index(dates[0])
    ranking_history = []
    initial_indices, next_refresh_global = ranking_schedule(
        len(global_dates),
        config.refresh_days,
        config.refresh_offset,
        start_global_index,
        config.rank_vote_window)
    for index in initial_indices:
        ranking_date = global_dates[index]
        ranking_rows = frame.loc[
            frame["date"] == ranking_date].reset_index(drop=True)
        ranking_history.append(factor_universe(ranking_rows, config))
    ranking_cache = voted_ranking(
        ranking_history,
        config.rank_vote_min)
    equity_rows = []
    trades = []

    for day_index, date in enumerate(dates):
        global_day_index = start_global_index + day_index
        rows = by_date[date]
        reserved_slots = [
            release_index for release_index in reserved_slots
            if release_index > day_index
        ]

        for code in list(pending_exits):
            position = positions.get(code)
            if position is None or code not in rows.index:
                continue
            row = rows.loc[code]
            if not bool(row["tradable"]) or bool(row["cannot_sell"]):
                continue
            exit_price = float(row["adj_open"])
            proceeds = position["units"] * exit_price * (1.0 - sell_cost(date))
            cash += proceeds
            pnl = proceeds / position["cash_in"] - 1.0
            trades.append({
                "code": code,
                "entry_date": position["entry_date"],
                "exit_date": date,
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "capital_in": position["cash_in"],
                "capital_out": proceeds,
                "pnl_amount": proceeds - position["cash_in"],
                "return": pnl,
                "hold_days": position["days"],
                "reason": position.get("exit_reason", "signal"),
            })
            stop_days = config.stop_cooldown_days if pnl < 0 else config.cooldown_days
            cooldown_until[code] = day_index + stop_days
            if (position.get("exit_reason") == "weak-time" and
                    config.weak_exit_reserve_slot):
                remaining_days = max(
                    0, config.max_hold_days - int(position["days"]))
                if remaining_days > 0:
                    reserved_slots.append(day_index + remaining_days)
            del positions[code]
            pending_exits.remove(code)

        available_slots = (
            config.max_positions - len(positions) - len(reserved_slots))
        executable = []
        for code in pending_entries:
            if len(executable) >= min(config.max_entries_per_day, available_slots):
                break
            if code in positions or code not in rows.index:
                continue
            row = rows.loc[code]
            if not bool(row["tradable"]) or bool(row["cannot_buy"]):
                continue
            executable.append(code)
        if executable and cash > 1000:
            open_values = []
            for code, position in positions.items():
                if code in rows.index:
                    open_values.append(position["units"] * float(rows.loc[code, "adj_open"]))
            equity_open = cash + sum(open_values)
            base_allocation = equity_open / config.max_positions
            risk_values = []
            for code in executable:
                atr_pct = float(rows.loc[code, "atr_pct"] or 0.0)
                risk_values.append(1.0 / max(atr_pct, 0.02))
            risk_mean = float(np.mean(risk_values)) if risk_values else 1.0
            for code, risk_value in zip(executable, risk_values):
                if cash <= 1000:
                    break
                allocation = base_allocation
                if config.risk_model == "v5":
                    average_range = float(rows.loc[code, "avg_range20"] or 0.03)
                    multiplier = 3.0 / (1.0 + average_range * 100.0)
                    multiplier *= config.risk_budget_scale
                    multiplier = min(1.5, max(0.3, multiplier))
                    allocation *= multiplier
                elif config.risk_model == "inverse":
                    multiplier = risk_value / risk_mean
                    multiplier = min(
                        config.risk_multiplier_max,
                        max(config.risk_multiplier_min, multiplier))
                    allocation *= multiplier * config.risk_budget_scale
                elif config.risk_model != "equal":
                    raise ValueError("unknown risk model: {}".format(
                        config.risk_model))
                allocation = min(allocation, cash)
                entry_price = float(rows.loc[code, "adj_open"])
                cash_in = allocation
                units = allocation / (entry_price * (1.0 + BUY_COST))
                cash -= allocation
                positions[code] = {
                    "units": units,
                    "entry_price": entry_price,
                    "entry_date": date,
                    "cash_in": cash_in,
                    "days": 0,
                    "peak": entry_price,
                }
        pending_entries = []

        close_value = cash
        for code, position in positions.items():
            if code not in rows.index:
                continue
            close_price = float(rows.loc[code, "adj_close"])
            close_value += position["units"] * close_price
            position["days"] += 1
            position["peak"] = max(position["peak"], close_price)
        equity_rows.append({"date": date, "equity": close_value})

        market_row = rows.iloc[0]
        market_ok = market_allows_entry(market_row, config)
        for code, position in positions.items():
            if code not in rows.index or code in pending_exits:
                continue
            row = rows.loc[code]
            close_price = float(row["adj_close"])
            pnl = close_price / position["entry_price"] - 1.0
            stop = config.early_stop if position["days"] <= config.early_days else config.hard_stop
            reason = ""
            if pnl <= stop:
                reason = "stop"
            elif config.trailing_atr is not None:
                trailing = position["peak"] - float(row["atr20"]) * config.trailing_atr
                if position["days"] >= 5 and close_price < trailing:
                    reason = "atr-trail"
            if (not reason and config.profit_lock_start is not None and
                    pnl >= config.profit_lock_start):
                locked_price = position["entry_price"] * (
                    1.0 + pnl * config.profit_lock_keep)
                if close_price < locked_price:
                    reason = "profit-lock"
            if not reason and config.weak_exit_min_days is not None:
                weak_enough = pnl <= config.weak_exit_max_return
                old_enough = position["days"] >= config.weak_exit_min_days
                below_ma = close_price < float(row["ma20"])
                ma_condition = (
                    below_ma if config.weak_exit_require_below_ma else True)
                if old_enough and weak_enough and ma_condition:
                    reason = "weak-time"
            if not reason and position["days"] >= config.max_hold_days:
                reason = "max-hold"
            if not reason and not market_ok:
                if (config.market_exit_mode == "legacy" and
                        config.market_mode == "v5"):
                    reason = "market"
                elif config.market_exit_mode == "all":
                    reason = "market"
                elif config.market_exit_mode == "tiered":
                    severe_level = float(row["market_ma20"]) * (
                        1.0 - config.market_severe_pct)
                    severe_market = float(row["market_level"]) < severe_level
                    weak_position = (
                        pnl <= config.market_weak_max_return and
                        close_price < float(row["ma20"]))
                    if severe_market:
                        reason = "market-severe"
                    elif weak_position:
                        reason = "market-weak"
                elif config.market_exit_mode != "none":
                    raise ValueError("unknown market exit mode: {}".format(
                        config.market_exit_mode))
            if reason:
                position["exit_reason"] = reason
                pending_exits.add(code)

        flat_rows = rows.reset_index()
        if global_day_index >= next_refresh_global:
            latest_ranking = factor_universe(flat_rows, config)
            ranking_history.append(latest_ranking)
            ranking_history = ranking_history[-config.rank_vote_window:]
            ranking_cache = voted_ranking(
                ranking_history,
                config.rank_vote_min)
            next_refresh_global += config.refresh_days
        candidates = entry_candidates(flat_rows, config, ranking_cache or set())
        if market_ok and not candidates.empty:
            active_after_exits = (
                len(positions) - len(pending_exits) + len(reserved_slots))
            slots = config.max_positions - active_after_exits
            selected = []
            for code in candidates["code"]:
                if len(selected) >= min(slots, config.max_entries_per_day):
                    break
                if code in positions:
                    continue
                if cooldown_until.get(code, -1) > day_index:
                    continue
                selected.append(code)
            pending_entries = selected

    equity = pd.DataFrame(equity_rows).set_index("date")["equity"]
    returns = equity.pct_change().dropna()
    years = max((pd.Timestamp(equity.index[-1]) -
                 pd.Timestamp(equity.index[0])).days / 365.25, 1.0 / 252.0)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    annual_return = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0
    drawdown = equity / equity.cummax() - 1.0
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = returns.mean() / returns.std() * math.sqrt(252.0)
    trade_frame = pd.DataFrame(trades)
    metrics = {
        "name": config.name,
        "start": start,
        "end": end,
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(sharpe),
        "trades": int(len(trade_frame)),
        "win_rate": float((trade_frame["return"] > 0).mean()) if len(trade_frame) else 0.0,
        "avg_trade": float(trade_frame["return"].mean()) if len(trade_frame) else 0.0,
        "final_equity": float(equity.iloc[-1]),
    }
    return metrics, equity.rename(config.name).to_frame(), trade_frame


def candidate_configs() -> list[Config]:
    baseline = Config(name="v5_reconstructed")
    normalized_risk = replace(
        baseline,
        name="v1_normalized_inverse_atr_budget",
        risk_model="inverse",
    )
    candidates = [baseline, normalized_risk]
    candidates.extend([
        replace(baseline, name="v5_no_gap_history", max_drop_limit=None),
        replace(baseline, name="v5_no_path_filters", max_drop_limit=None,
                max_consecutive_up=None, max_range_5=None),
        replace(baseline, name="v5_refresh5", refresh_days=5),
        replace(baseline, name="v5_positions8", max_positions=8),
        replace(baseline, name="v5_hold40", max_hold_days=40),
        replace(baseline, name="v5_hold60", max_hold_days=60),
        replace(baseline, name="v5_trail3_hold60", max_hold_days=60,
                trailing_atr=3.0),
        replace(baseline, name="v5_trail4_hold60", max_hold_days=60,
                trailing_atr=4.0),
        replace(baseline, name="v5_stop8_trail3", max_hold_days=60,
                early_stop=-0.06, hard_stop=-0.08, trailing_atr=3.0),
        replace(baseline, name="v5_breakout20_hold40", breakout_days=20,
                breakout_strength=0.002, max_hold_days=40),
        replace(baseline, name="v5_soft_market", market_mode="soft"),
        replace(baseline, name="v5_market_off", market_mode="off"),
        replace(baseline, name="v5_no_filters_hold40", max_drop_limit=None,
                max_consecutive_up=None, max_range_5=None, max_hold_days=40),
        replace(baseline, name="v5_no_filters_trail3", max_drop_limit=None,
                max_consecutive_up=None, max_range_5=None, max_hold_days=60,
                early_stop=-0.06, hard_stop=-0.10, trailing_atr=3.0),
        replace(baseline, name="v5_diversified_trail3", max_drop_limit=None,
                max_consecutive_up=None, max_range_5=None, max_positions=8,
                max_hold_days=60, early_stop=-0.06, hard_stop=-0.10,
                trailing_atr=3.0, cooldown_days=10,
                stop_cooldown_days=30),
    ])
    score_variants = [
        ("alpha_normalized", 1.0, 0.0, 0.0),
        ("blend", 0.50, 0.35, 0.15),
        ("blend", 0.35, 0.50, 0.15),
        ("blend", 0.25, 0.60, 0.15),
    ]
    for score, alpha_weight, momentum_weight, quality_weight in score_variants:
        for breakout_days in (15, 20, 30):
            for market_mode in ("soft", "regime"):
                weight_tag = "a{:02d}m{:02d}q{:02d}".format(
                    int(alpha_weight * 100),
                    int(momentum_weight * 100),
                    int(quality_weight * 100),
                )
                name = "{}_{}_b{}_{}".format(
                    score, weight_tag, breakout_days, market_mode)
                candidates.append(Config(
                    name=name,
                    score=score,
                    alpha_weight=alpha_weight,
                    momentum_weight=momentum_weight,
                    quality_weight=quality_weight,
                    factor_top_pct=0.20,
                    refresh_days=5,
                    breakout_days=breakout_days,
                    breakout_strength=0.002,
                    amount_ratio=1.05,
                    trend_ma=50,
                    momentum_days=20,
                    momentum_up_min=1,
                    max_drop_window=20,
                    max_drop_limit=-0.095,
                    max_consecutive_up=None,
                    max_range_5=0.14,
                    max_positions=8,
                    max_entries_per_day=2,
                    max_hold_days=60,
                    early_stop=-0.06,
                    early_days=3,
                    hard_stop=-0.10,
                    trailing_atr=3.0,
                    profit_lock_start=None,
                    cooldown_days=10,
                    stop_cooldown_days=30,
                    market_mode=market_mode,
                    risk_model="inverse",
                ))
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="run the baseline and a small representative subset")
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("loading RedisQMT panel...", flush=True)
    frame = add_features(load_panel())
    configs = candidate_configs()
    if args.quick:
        configs = configs[:2]

    train_rows = []
    results = {}
    for index, config in enumerate(configs, 1):
        print("train {}/{} {}".format(index, len(configs), config.name), flush=True)
        metrics, equity, trades = run_backtest(
            frame, config, "20220104", "20241231")
        train_rows.append(metrics)
        results[(config.name, "train")] = (equity, trades)
    train = pd.DataFrame(train_rows).sort_values(
        ["annual_return", "sharpe"], ascending=False)
    train.to_csv(OUTPUT_DIR / "train_sweep.csv", index=False, encoding="utf-8-sig")

    baseline_name = "v5_reconstructed"
    chosen_names = [baseline_name]
    for name in train["name"]:
        if name not in chosen_names:
            chosen_names.append(name)
        if len(chosen_names) >= 6:
            break
    by_name = {config.name: config for config in configs}
    test_rows = []
    for name in chosen_names:
        config = by_name[name]
        print("holdout {}".format(name), flush=True)
        metrics, equity, trades = run_backtest(
            frame, config, "20250102", "20260918")
        test_rows.append(metrics)
        results[(name, "holdout")] = (equity, trades)
    holdout = pd.DataFrame(test_rows).sort_values(
        ["annual_return", "sharpe"], ascending=False)
    holdout.to_csv(OUTPUT_DIR / "holdout.csv", index=False, encoding="utf-8-sig")

    # Freeze from the training interval only.  Holdout is confirmation, never
    # a selector; otherwise the apparent out-of-sample result is data-mined.
    robust = train[train["name"] != baseline_name].copy()
    baseline_train = train[train["name"] == baseline_name].iloc[0]
    robust = robust[
        robust["max_drawdown"] >= baseline_train["max_drawdown"] - 0.07]
    if robust.empty:
        selected_name = baseline_name
    else:
        robust["score"] = (
            robust["annual_return"] + robust["sharpe"] * 0.03 +
            robust["max_drawdown"] * 0.20)
        selected_name = str(robust.sort_values("score", ascending=False).iloc[0]["name"])
    selected = by_name[selected_name]
    full_metrics, full_equity, full_trades = run_backtest(
        frame, selected, "20220104", "20260918")
    baseline_metrics, baseline_equity, baseline_trades = run_backtest(
        frame, by_name[baseline_name], "20220104", "20260918")
    pd.concat([baseline_equity, full_equity], axis=1).to_csv(
        OUTPUT_DIR / "equity.csv", encoding="utf-8-sig")
    full_trades.to_csv(OUTPUT_DIR / "selected_trades.csv", index=False,
                       encoding="utf-8-sig")
    baseline_trades.to_csv(OUTPUT_DIR / "baseline_trades.csv", index=False,
                           encoding="utf-8-sig")
    payload = {
        "data": {
            "panel": str(PANEL_PATH.relative_to(ROOT)),
            "universe": "current CSI500 snapshot as of 20260918",
            "train": ["20220104", "20241231"],
            "holdout": ["20250102", "20260918"],
            "execution": "T signal at close, T+1 tradable open",
            "costs": {
                "buy": BUY_COST,
                "sell_before_20230828": SELL_COST_BEFORE_20230828,
                "sell_after_20230828": SELL_COST_AFTER_20230828,
            },
        },
        "selected": asdict(selected),
        "baseline_full": baseline_metrics,
        "selected_full": full_metrics,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
