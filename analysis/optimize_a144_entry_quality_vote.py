# -*- coding: utf-8 -*-
"""Bounded robustness study for A144 entry-filter simplification and voting."""

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


OUTPUT_DIR = ROOT / "analysis" / "a144_entry_vote_optimization_20260923"


def baseline_config() -> Config:
    return Config(
        name="v2_live_parity",
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True,
    )


def candidate_configs(base: Config) -> list[Config]:
    return [
        base,
        replace(
            base,
            name="no_large_drop",
            max_drop_limit=None),
        replace(
            base,
            name="no_momentum",
            momentum_up_min=0),
        replace(
            base,
            name="no_drop_no_momentum",
            max_drop_limit=None,
            momentum_up_min=0),
        replace(
            base,
            name="vote_5_of_6",
            entry_vote_min=5),
        replace(
            base,
            name="vote_4_of_6",
            entry_vote_min=4),
        replace(
            base,
            name="vote_3_of_6",
            entry_vote_min=3),
        replace(
            base,
            name="vote_4_of_5_no_drop",
            max_drop_limit=None,
            entry_vote_min=4),
        replace(
            base,
            name="rank_union_2",
            rank_vote_window=2,
            rank_vote_min=1),
        replace(
            base,
            name="rank_intersection_2",
            rank_vote_window=2,
            rank_vote_min=2),
        replace(
            base,
            name="rank_majority_2_of_3",
            rank_vote_window=3,
            rank_vote_min=2),
        replace(
            base,
            name="rank_majority_2_of_3_no_drop",
            rank_vote_window=3,
            rank_vote_min=2,
            max_drop_limit=None),
    ]


def evaluate(
        frame: pd.DataFrame,
        configs: list[Config],
        periods: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for period, start, end in periods:
        for index, config in enumerate(configs, 1):
            print(
                "{} {}/{} {}".format(
                    period,
                    index,
                    len(configs),
                    config.name),
                flush=True)
            metrics, _, _ = run_backtest(frame, config, start, end)
            metrics["period"] = period
            metrics["refresh_offset"] = config.refresh_offset
            rows.append(metrics)
    return pd.DataFrame(rows)


def choose_candidate(raw: pd.DataFrame, base: Config) -> tuple[str, pd.DataFrame]:
    indexed = raw.set_index(["period", "name"])
    baseline_train = indexed.loc[("train", base.name)]
    baseline_holdout = indexed.loc[("holdout", base.name)]
    baseline_full = indexed.loc[("full", base.name)]
    candidates = []
    for name in raw["name"].drop_duplicates():
        if name == base.name:
            continue
        train = indexed.loc[("train", name)]
        holdout = indexed.loc[("holdout", name)]
        full = indexed.loc[("full", name)]
        train_annual_delta = (
            train["annual_return"] - baseline_train["annual_return"])
        holdout_annual_delta = (
            holdout["annual_return"] - baseline_holdout["annual_return"])
        full_annual_delta = (
            full["annual_return"] - baseline_full["annual_return"])
        train_sharpe_delta = train["sharpe"] - baseline_train["sharpe"]
        holdout_sharpe_delta = holdout["sharpe"] - baseline_holdout["sharpe"]
        full_sharpe_delta = full["sharpe"] - baseline_full["sharpe"]
        train_drawdown_delta = (
            train["max_drawdown"] - baseline_train["max_drawdown"])
        holdout_drawdown_delta = (
            holdout["max_drawdown"] - baseline_holdout["max_drawdown"])
        full_drawdown_delta = (
            full["max_drawdown"] - baseline_full["max_drawdown"])
        passes = bool(
            train_annual_delta > 0 and
            holdout_annual_delta > 0 and
            full_annual_delta > 0 and
            full_sharpe_delta >= 0 and
            train_drawdown_delta >= -0.02 and
            holdout_drawdown_delta >= -0.02 and
            full_drawdown_delta >= -0.02)
        candidates.append({
            "name": name,
            "passes_two_period_gate": passes,
            "train_annual_delta": train_annual_delta,
            "holdout_annual_delta": holdout_annual_delta,
            "full_annual_delta": full_annual_delta,
            "worst_annual_delta": min(
                train_annual_delta,
                holdout_annual_delta,
                full_annual_delta),
            "train_sharpe_delta": train_sharpe_delta,
            "holdout_sharpe_delta": holdout_sharpe_delta,
            "full_sharpe_delta": full_sharpe_delta,
            "worst_sharpe_delta": min(
                train_sharpe_delta,
                holdout_sharpe_delta,
                full_sharpe_delta),
            "train_drawdown_delta": train_drawdown_delta,
            "holdout_drawdown_delta": holdout_drawdown_delta,
            "full_drawdown_delta": full_drawdown_delta,
        })
    comparison = pd.DataFrame(candidates).sort_values(
        [
            "passes_two_period_gate",
            "worst_annual_delta",
            "worst_sharpe_delta",
        ],
        ascending=[False, False, False])
    passing = comparison[comparison["passes_two_period_gate"]]
    selected = base.name if passing.empty else str(passing.iloc[0]["name"])
    return selected, comparison


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base = baseline_config()
    configs = candidate_configs(base)
    periods = [
        ("train", "20220104", "20241231"),
        ("holdout", "20250102", "20260918"),
        ("full", "20220104", "20260918"),
    ]
    raw = evaluate(frame, configs, periods)
    selected_name, comparison = choose_candidate(raw, base)
    selected = next(
        config for config in configs if config.name == selected_name)

    yearly_periods = [
        ("year_2022", "20220104", "20221230"),
        ("year_2023", "20230103", "20231229"),
        ("year_2024", "20240102", "20241231"),
        ("year_2025", "20250102", "20251231"),
        ("year_2026", "20260105", "20260918"),
    ]
    yearly = evaluate(frame, [base, selected], yearly_periods)

    phase_configs = []
    for offset in range(10):
        phase_configs.append(replace(
            base,
            name="{}_phase_{}".format(base.name, offset),
            refresh_offset=offset))
        phase_configs.append(replace(
            selected,
            name="{}_phase_{}".format(selected.name, offset),
            refresh_offset=offset))
    phases = evaluate(
        frame,
        phase_configs,
        [("full", "20220104", "20260918")])
    phases["family"] = phases["name"].str.replace(
        r"_phase_\d+$",
        "",
        regex=True)

    raw.to_csv(
        OUTPUT_DIR / "candidate_periods.csv",
        index=False,
        encoding="utf-8-sig")
    comparison.to_csv(
        OUTPUT_DIR / "candidate_gate.csv",
        index=False,
        encoding="utf-8-sig")
    yearly.to_csv(
        OUTPUT_DIR / "yearly.csv",
        index=False,
        encoding="utf-8-sig")
    phases.to_csv(
        OUTPUT_DIR / "refresh_phases.csv",
        index=False,
        encoding="utf-8-sig")

    phase_summary = phases.groupby("family").agg({
        "annual_return": ["min", "median", "max"],
        "max_drawdown": ["min", "median", "max"],
        "sharpe": ["min", "median", "max"],
    })
    phase_summary.columns = [
        "_".join(values) for values in phase_summary.columns]
    phase_summary = phase_summary.reset_index()
    phase_summary.to_csv(
        OUTPUT_DIR / "refresh_phase_summary.csv",
        index=False,
        encoding="utf-8-sig")

    payload = {
        "selection_rule": (
            "positive annual-return delta in train, holdout, and continuous "
            "full period; nonnegative full-period Sharpe delta; no more than "
            "2 percentage points of drawdown deterioration in any period; "
            "maximize the worse annual-return delta"),
        "selected": selected_name,
        "candidate_gate": comparison.to_dict(orient="records"),
        "period_results": raw.to_dict(orient="records"),
        "yearly_results": yearly.to_dict(orient="records"),
        "phase_summary": phase_summary.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("selected={}".format(selected_name), flush=True)
    print(comparison.to_string(index=False), flush=True)
    print(phase_summary.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
