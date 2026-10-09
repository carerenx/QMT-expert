# -*- coding: utf-8 -*-
"""Ablation study for A144 v2 exits and portfolio-risk controls."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
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


OUTPUT_DIR = ROOT / "analysis" / "a144_v2_20260923"
TRAIN_START = "20220104"
TRAIN_END = "20241231"
HOLDOUT_START = "20250102"
HOLDOUT_END = "20260918"
FULL_END = "20260918"


def base_config() -> Config:
    return Config(
        name="v1_normalized_inverse_atr_budget",
        risk_model="inverse",
    )


def objective(metrics: dict) -> float:
    return (
        float(metrics["annual_return"]) +
        float(metrics["sharpe"]) * 0.04 +
        float(metrics["max_drawdown"]) * 0.25
    )


def run_configs(frame: pd.DataFrame, configs: list[Config],
                start: str, end: str) -> tuple[pd.DataFrame, dict]:
    rows = []
    details = {}
    for index, config in enumerate(configs, 1):
        print("{}/{} {} {}-{}".format(
            index, len(configs), config.name, start, end), flush=True)
        metrics, equity, trades = run_backtest(frame, config, start, end)
        metrics["objective"] = objective(metrics)
        rows.append(metrics)
        details[config.name] = (equity, trades)
    table = pd.DataFrame(rows).sort_values(
        ["objective", "annual_return"], ascending=False)
    return table, details


def select_best(table: pd.DataFrame, prefix: str) -> str:
    matches = table[table["name"].str.startswith(prefix)]
    if matches.empty:
        raise RuntimeError("no candidates for {}".format(prefix))
    return str(matches.iloc[0]["name"])


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base = base_config()

    weak_candidates = []
    for minimum_days in (8, 10, 12, 15):
        for maximum_return in (0.0, -0.02, -0.04):
            weak_candidates.append(replace(
                base,
                name="weak_d{}_r{:02d}".format(
                    minimum_days, int(abs(maximum_return) * 100)),
                weak_exit_min_days=minimum_days,
                weak_exit_max_return=maximum_return,
            ))

    market_candidates = []
    for severe_pct in (0.04, 0.05, 0.06, 0.07):
        for maximum_return in (0.0, -0.02):
            market_candidates.append(replace(
                base,
                name="tiered_s{:02d}_r{:02d}".format(
                    int(severe_pct * 100),
                    int(abs(maximum_return) * 100)),
                market_exit_mode="tiered",
                market_severe_pct=severe_pct,
                market_weak_max_return=maximum_return,
            ))

    risk_candidates = []
    for scale in (0.80, 0.85, 0.90, 0.95):
        risk_candidates.append(replace(
            base,
            name="risk_{:02d}".format(int(scale * 100)),
            risk_budget_scale=scale,
        ))

    stage_one_configs = [base] + weak_candidates + market_candidates + risk_candidates
    stage_one, _ = run_configs(
        frame, stage_one_configs, TRAIN_START, TRAIN_END)
    stage_one.to_csv(
        OUTPUT_DIR / "train_stage1_ablation.csv",
        index=False,
        encoding="utf-8-sig")

    config_by_name = {config.name: config for config in stage_one_configs}
    best_weak = config_by_name[select_best(stage_one, "weak_")]
    best_market = config_by_name[select_best(stage_one, "tiered_")]

    combined_candidates = []
    for scale in (0.80, 0.85, 0.90, 0.95, 1.00):
        combined_candidates.append(replace(
            base,
            name="v2_combined_risk_{:03d}".format(int(scale * 100)),
            weak_exit_min_days=best_weak.weak_exit_min_days,
            weak_exit_max_return=best_weak.weak_exit_max_return,
            market_exit_mode="tiered",
            market_severe_pct=best_market.market_severe_pct,
            market_weak_max_return=best_market.market_weak_max_return,
            risk_budget_scale=scale,
        ))
    combined, _ = run_configs(
        frame, combined_candidates, TRAIN_START, TRAIN_END)
    combined.to_csv(
        OUTPUT_DIR / "train_stage2_combined.csv",
        index=False,
        encoding="utf-8-sig")

    selected_name = str(combined.iloc[0]["name"])
    selected = next(
        config for config in combined_candidates if config.name == selected_name)
    confirmation_configs = [
        base,
        replace(
            base,
            name="ablation_weak_only",
            weak_exit_min_days=selected.weak_exit_min_days,
            weak_exit_max_return=selected.weak_exit_max_return,
        ),
        replace(
            base,
            name="ablation_market_only",
            market_exit_mode=selected.market_exit_mode,
            market_severe_pct=selected.market_severe_pct,
            market_weak_max_return=selected.market_weak_max_return,
        ),
        replace(
            base,
            name="ablation_risk_only",
            risk_budget_scale=selected.risk_budget_scale,
        ),
        selected,
    ]
    holdout, _ = run_configs(
        frame, confirmation_configs, HOLDOUT_START, HOLDOUT_END)
    holdout.to_csv(
        OUTPUT_DIR / "holdout_confirmation.csv",
        index=False,
        encoding="utf-8-sig")

    full_rows = []
    full_details = {}
    for config in confirmation_configs:
        metrics, equity, trades = run_backtest(
            frame, config, TRAIN_START, FULL_END)
        metrics["objective"] = objective(metrics)
        full_rows.append(metrics)
        full_details[config.name] = (equity, trades)
    full = pd.DataFrame(full_rows).sort_values(
        ["objective", "annual_return"], ascending=False)
    full.to_csv(
        OUTPUT_DIR / "full_period_ablation.csv",
        index=False,
        encoding="utf-8-sig")

    equity_frames = [full_details[config.name][0] for config in confirmation_configs]
    pd.concat(equity_frames, axis=1).to_csv(
        OUTPUT_DIR / "equity.csv", encoding="utf-8-sig")
    full_details[selected.name][1].to_csv(
        OUTPUT_DIR / "selected_trades.csv",
        index=False,
        encoding="utf-8-sig")
    full_details[base.name][1].to_csv(
        OUTPUT_DIR / "baseline_trades.csv",
        index=False,
        encoding="utf-8-sig")

    payload = {
        "selection_rule": (
            "training-only objective = annual_return + 0.04*Sharpe + "
            "0.25*max_drawdown; holdout is confirmation only"),
        "best_single_weak": asdict(best_weak),
        "best_single_market": asdict(best_market),
        "selected": asdict(selected),
        "train_selected": combined.loc[
            combined["name"] == selected.name].iloc[0].to_dict(),
        "holdout_baseline": holdout.loc[
            holdout["name"] == base.name].iloc[0].to_dict(),
        "holdout_selected": holdout.loc[
            holdout["name"] == selected.name].iloc[0].to_dict(),
        "full_baseline": full.loc[
            full["name"] == base.name].iloc[0].to_dict(),
        "full_selected": full.loc[
            full["name"] == selected.name].iloc[0].to_dict(),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
