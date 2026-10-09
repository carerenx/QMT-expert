# -*- coding: utf-8 -*-
"""Validate weak exits that reserve their original portfolio slot."""

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

from output.analysis.research_a144_redisqmt import Config
from output.analysis.research_a144_redisqmt import add_features
from output.analysis.research_a144_redisqmt import load_panel
from output.analysis.research_a144_redisqmt import run_backtest


OUTPUT_DIR = ROOT / "analysis" / "a144_v2_reserved_exit_20260923"


def score(metrics: dict) -> float:
    return (
        float(metrics["annual_return"]) +
        float(metrics["sharpe"]) * 0.04 +
        float(metrics["max_drawdown"]) * 0.25
    )


def evaluate(frame: pd.DataFrame, config: Config,
             start: str, end: str) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    metrics, equity, trades = run_backtest(frame, config, start, end)
    metrics["objective"] = score(metrics)
    return metrics, equity, trades


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = add_features(load_panel())
    baseline = Config(
        name="v1_normalized_inverse_atr_budget",
        risk_model="inverse")
    candidates = []
    for minimum_days in (8, 10, 12, 15):
        for maximum_return in (0.0, -0.02, -0.04):
            candidates.append(replace(
                baseline,
                name="reserved_d{}_r{:02d}".format(
                    minimum_days, int(abs(maximum_return) * 100)),
                weak_exit_min_days=minimum_days,
                weak_exit_max_return=maximum_return,
                weak_exit_reserve_slot=True,
            ))

    train_rows = []
    for index, config in enumerate([baseline] + candidates, 1):
        print("train {}/{} {}".format(
            index, len(candidates) + 1, config.name), flush=True)
        metrics, _, _ = evaluate(
            frame, config, "20220104", "20241231")
        train_rows.append(metrics)
    train = pd.DataFrame(train_rows).sort_values(
        ["objective", "annual_return"], ascending=False)
    train.to_csv(
        OUTPUT_DIR / "train_sweep.csv", index=False, encoding="utf-8-sig")

    baseline_train = train.loc[
        train["name"] == baseline.name].iloc[0]
    eligible = train[
        (train["name"] != baseline.name) &
        (train["annual_return"] > baseline_train["annual_return"]) &
        (train["max_drawdown"] >= baseline_train["max_drawdown"])
    ]
    if eligible.empty:
        selected = baseline
    else:
        selected_name = str(eligible.iloc[0]["name"])
        selected = next(
            config for config in candidates if config.name == selected_name)

    comparison_rows = []
    comparison_details = {}
    periods = [
        ("train", "20220104", "20241231"),
        ("holdout", "20250102", "20260918"),
        ("full", "20220104", "20260918"),
    ]
    for period_name, start, end in periods:
        for config in (baseline, selected):
            metrics, equity, trades = evaluate(frame, config, start, end)
            metrics["period"] = period_name
            comparison_rows.append(metrics)
            comparison_details[(period_name, config.name)] = (equity, trades)
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(
        OUTPUT_DIR / "period_comparison.csv",
        index=False,
        encoding="utf-8-sig")

    phase_rows = []
    for offset in range(10):
        for config in (baseline, selected):
            phase_config = replace(
                config,
                name="{}_phase{}".format(config.name, offset),
                refresh_offset=offset)
            print("phase {} {}".format(offset, config.name), flush=True)
            metrics, _, _ = evaluate(
                frame, phase_config, "20220104", "20260918")
            metrics["version"] = config.name
            metrics["refresh_offset"] = offset
            phase_rows.append(metrics)
    phases = pd.DataFrame(phase_rows)
    phases.to_csv(
        OUTPUT_DIR / "refresh_phase_comparison.csv",
        index=False,
        encoding="utf-8-sig")

    baseline_full_equity, baseline_full_trades = comparison_details[
        ("full", baseline.name)]
    selected_full_equity, selected_full_trades = comparison_details[
        ("full", selected.name)]
    pd.concat([baseline_full_equity, selected_full_equity], axis=1).to_csv(
        OUTPUT_DIR / "equity.csv", encoding="utf-8-sig")
    baseline_full_trades.to_csv(
        OUTPUT_DIR / "baseline_trades.csv",
        index=False,
        encoding="utf-8-sig")
    selected_full_trades.to_csv(
        OUTPUT_DIR / "selected_trades.csv",
        index=False,
        encoding="utf-8-sig")

    phase_summary = {}
    for version, group in phases.groupby("version"):
        phase_summary[str(version)] = {
            "annual_return": {
                "min": float(group["annual_return"].min()),
                "median": float(group["annual_return"].median()),
                "max": float(group["annual_return"].max()),
            },
            "max_drawdown": {
                "worst": float(group["max_drawdown"].min()),
                "median": float(group["max_drawdown"].median()),
                "best": float(group["max_drawdown"].max()),
            },
            "sharpe": {
                "min": float(group["sharpe"].min()),
                "median": float(group["sharpe"].median()),
                "max": float(group["sharpe"].max()),
            },
        }
    payload = {
        "selection": (
            "training-only; annual return above v1, max drawdown no worse "
            "than v1, then highest risk-adjusted objective"),
        "selected": asdict(selected),
        "period_comparison": comparison.to_dict(orient="records"),
        "phase_summary": phase_summary,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps({
        "selected": asdict(selected),
        "comparison": comparison.to_dict(orient="records"),
    }, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
