# -*- coding: utf-8 -*-
"""Robustness validation for A144 risk-budget utilization."""

from __future__ import annotations

import json
import sys
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


OUTPUT_DIR = ROOT / "analysis" / "a144_risk_budget_optimization_20260923"


def base_config() -> Config:
    return Config(
        name="risk_scale_100",
        risk_model="inverse",
        risk_budget_scale=1.0,
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True,
    )


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base = base_config()
    scales = (0.80, 0.90, 1.05, 1.10, 1.15, 1.20)
    configs = [base]
    for scale in scales:
        configs.append(replace(
            base,
            name="risk_scale_{:03d}".format(round(scale * 100)),
            risk_budget_scale=scale))
    periods = [
        ("train", "20220104", "20241231"),
        ("holdout", "20250102", "20260918"),
        ("full", "20220104", "20260918"),
    ]
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
            rows.append(metrics)
    raw = pd.DataFrame(rows)
    indexed = raw.set_index(["period", "name"])
    gate_rows = []
    for config in configs[1:]:
        deltas = {}
        for period, _, _ in periods:
            candidate = indexed.loc[(period, config.name)]
            baseline = indexed.loc[(period, base.name)]
            deltas[period] = {
                "annual": (
                    candidate["annual_return"] - baseline["annual_return"]),
                "drawdown": (
                    candidate["max_drawdown"] - baseline["max_drawdown"]),
                "sharpe": candidate["sharpe"] - baseline["sharpe"],
            }
        passes = bool(
            all(deltas[period]["annual"] > 0 for period, _, _ in periods) and
            all(deltas[period]["drawdown"] >= -0.02
                for period, _, _ in periods) and
            deltas["full"]["sharpe"] >= -0.01)
        gate_rows.append({
            "name": config.name,
            "risk_budget_scale": config.risk_budget_scale,
            "passes": passes,
            "train_annual_delta": deltas["train"]["annual"],
            "holdout_annual_delta": deltas["holdout"]["annual"],
            "full_annual_delta": deltas["full"]["annual"],
            "worst_annual_delta": min(
                deltas[period]["annual"] for period, _, _ in periods),
            "train_drawdown_delta": deltas["train"]["drawdown"],
            "holdout_drawdown_delta": deltas["holdout"]["drawdown"],
            "full_drawdown_delta": deltas["full"]["drawdown"],
            "full_sharpe_delta": deltas["full"]["sharpe"],
        })
    gate = pd.DataFrame(gate_rows).sort_values(
        ["passes", "worst_annual_delta", "full_sharpe_delta"],
        ascending=[False, False, False])
    passing = gate[gate["passes"]]
    selected_name = base.name if passing.empty else str(passing.iloc[0]["name"])
    selected = next(
        config for config in configs if config.name == selected_name)

    phase_rows = []
    if selected.name != base.name:
        for offset in range(10):
            for config in (base, selected):
                phase_config = replace(
                    config,
                    name="{}_phase_{}".format(config.name, offset),
                    refresh_offset=offset)
                metrics, _, _ = run_backtest(
                    frame,
                    phase_config,
                    "20220104",
                    "20260918")
                metrics["family"] = config.name
                metrics["refresh_offset"] = offset
                phase_rows.append(metrics)
    phases = pd.DataFrame(phase_rows)
    phase_summary = pd.DataFrame()
    if not phases.empty:
        phase_summary = phases.groupby("family").agg({
            "annual_return": ["min", "median", "max"],
            "max_drawdown": ["min", "median", "max"],
            "sharpe": ["min", "median", "max"],
        })
        phase_summary.columns = [
            "_".join(values) for values in phase_summary.columns]
        phase_summary = phase_summary.reset_index()

    raw.to_csv(
        OUTPUT_DIR / "period_results.csv",
        index=False,
        encoding="utf-8-sig")
    gate.to_csv(
        OUTPUT_DIR / "robustness_gate.csv",
        index=False,
        encoding="utf-8-sig")
    phases.to_csv(
        OUTPUT_DIR / "refresh_phases.csv",
        index=False,
        encoding="utf-8-sig")
    phase_summary.to_csv(
        OUTPUT_DIR / "refresh_phase_summary.csv",
        index=False,
        encoding="utf-8-sig")
    payload = {
        "selected": selected.name,
        "selected_scale": selected.risk_budget_scale,
        "selection_rule": (
            "annual return improves in train, holdout and continuous full; "
            "drawdown deterioration <=2pct in each; full Sharpe delta >=-0.01"),
        "gate": gate.to_dict(orient="records"),
        "metrics": raw.to_dict(orient="records"),
        "phase_summary": phase_summary.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("selected={}".format(selected.name), flush=True)
    print(gate.to_string(index=False), flush=True)
    print(phase_summary.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
