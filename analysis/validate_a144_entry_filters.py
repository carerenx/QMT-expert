# -*- coding: utf-8 -*-
"""Leave-one-filter-out validation for A144 v2 entry conditions."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.research_a144_redisqmt import Config
from analysis.research_a144_redisqmt import add_features
from analysis.research_a144_redisqmt import load_panel
from analysis.research_a144_redisqmt import run_backtest


OUTPUT_DIR = ROOT / "analysis" / "a144_filter_validation_20260923"


def live_v2_config() -> Config:
    return Config(
        name="v2_live_parity",
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True,
    )


def variants(base: Config) -> list[tuple[str, str, Config]]:
    return [
        ("alpha_top15", "Alpha144前15%", replace(
            base, name="without_alpha_top15", factor_top_pct=1.0)),
        ("breakout", "突破10日高点0.5%", replace(
            base, name="without_breakout", require_breakout=False)),
        ("volume_surge", "成交额达到前5日均值1.2倍", replace(
            base, name="without_volume_surge", amount_ratio=0.0)),
        ("above_ma20", "收盘价高于MA20", replace(
            base, name="without_above_ma20", require_price_above_ma=False)),
        ("ma20_slope", "MA20高于5日前", replace(
            base, name="without_ma20_slope", require_ma_slope=False)),
        ("momentum_2_of_3", "最近3日至少2日上涨", replace(
            base, name="without_momentum", momentum_up_min=0)),
        ("no_large_drop", "近60日无超过7%单日跌幅", replace(
            base, name="without_large_drop", max_drop_limit=None)),
        ("not_three_up", "此前未连续上涨3日", replace(
            base, name="without_consecutive_up", max_consecutive_up=None)),
        ("range_cap", "近5日最大振幅不超过10%", replace(
            base, name="without_range_cap", max_range_5=None)),
        ("gap_floor", "当日开盘跌幅大于-3%", replace(
            base, name="without_gap_floor", gap_down_limit=None)),
        ("liquidity", "20日平均成交额至少3000万", replace(
            base, name="without_liquidity", min_daily_amount=0.0)),
        ("daily_limit", "当日涨幅低于9.8%", replace(
            base, name="without_daily_limit", daily_return_limit=None)),
        ("market_filter", "中证500市场过滤", replace(
            base, name="without_market_filter", market_mode="off")),
    ]


def classify(train_delta: pd.Series, holdout_delta: pd.Series) -> str:
    tested = [
        train_delta["annual_return"],
        holdout_delta["annual_return"],
        train_delta["sharpe"],
        holdout_delta["sharpe"],
        train_delta["max_drawdown"],
        holdout_delta["max_drawdown"],
    ]
    if all(abs(float(value)) < 1e-12 for value in tested):
        return "边际冗余"
    return_help_train = train_delta["annual_return"] > 0
    return_help_holdout = holdout_delta["annual_return"] > 0
    sharpe_help_train = train_delta["sharpe"] > 0
    sharpe_help_holdout = holdout_delta["sharpe"] > 0
    drawdown_help_train = train_delta["max_drawdown"] > 0
    drawdown_help_holdout = holdout_delta["max_drawdown"] > 0
    if all([
            return_help_train,
            return_help_holdout,
            sharpe_help_train,
            sharpe_help_holdout]):
        return "稳定有效"
    if all([
            drawdown_help_train,
            drawdown_help_holdout,
            sharpe_help_train,
            sharpe_help_holdout]):
        return "风险控制有效"
    if all([
            not return_help_train,
            not return_help_holdout,
            not sharpe_help_train,
            not sharpe_help_holdout]):
        return "疑似有害"
    return "阶段不稳定"


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base = live_v2_config()
    definitions = variants(base)
    configs = [base] + [config for _, _, config in definitions]
    periods = [
        ("train", "20220104", "20241231"),
        ("holdout", "20250102", "20260918"),
        ("full", "20220104", "20260918"),
    ]
    rows = []
    for period, start, end in periods:
        for index, config in enumerate(configs, 1):
            print("{} {}/{} {}".format(
                period, index, len(configs), config.name), flush=True)
            metrics, _, _ = run_backtest(frame, config, start, end)
            metrics["period"] = period
            rows.append(metrics)
    raw = pd.DataFrame(rows)
    raw.to_csv(
        OUTPUT_DIR / "ablation_raw.csv",
        index=False,
        encoding="utf-8-sig")

    index = raw.set_index(["period", "name"])
    result_rows = []
    metric_columns = [
        "annual_return",
        "max_drawdown",
        "sharpe",
        "trades",
        "win_rate",
    ]
    for key, label, config in definitions:
        deltas = {}
        for period, _, _ in periods:
            baseline = index.loc[(period, base.name), metric_columns]
            without = index.loc[(period, config.name), metric_columns]
            deltas[period] = baseline - without
        result_rows.append({
            "filter": key,
            "label": label,
            "classification": classify(deltas["train"], deltas["holdout"]),
            "train_annual_contribution": deltas["train"]["annual_return"],
            "holdout_annual_contribution": deltas["holdout"]["annual_return"],
            "full_annual_contribution": deltas["full"]["annual_return"],
            "train_sharpe_contribution": deltas["train"]["sharpe"],
            "holdout_sharpe_contribution": deltas["holdout"]["sharpe"],
            "full_sharpe_contribution": deltas["full"]["sharpe"],
            "train_drawdown_contribution": deltas["train"]["max_drawdown"],
            "holdout_drawdown_contribution": deltas["holdout"]["max_drawdown"],
            "full_drawdown_contribution": deltas["full"]["max_drawdown"],
            "full_trade_reduction": deltas["full"]["trades"],
            "full_win_rate_contribution": deltas["full"]["win_rate"],
        })
    results = pd.DataFrame(result_rows).sort_values(
        ["classification", "full_sharpe_contribution"],
        ascending=[True, False])
    results.to_csv(
        OUTPUT_DIR / "filter_effectiveness.csv",
        index=False,
        encoding="utf-8-sig")

    payload = {
        "method": (
            "leave one filter out; positive contribution means the complete "
            "v2 is better than the same strategy without that filter"),
        "live_parity_corrections": [
            "momentum uses current day plus previous two days",
            "Alpha144 rank pool is not pre-filtered by current amount",
            "20-day average amount is the liquidity condition",
        ],
        "baseline": {
            period: index.loc[(period, base.name)].to_dict()
            for period, _, _ in periods
        },
        "results": results.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(results.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
