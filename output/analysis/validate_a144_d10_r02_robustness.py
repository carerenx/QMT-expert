# -*- coding: utf-8 -*-
"""Year and refresh-phase validation for the A144 d10/r02 weak exit."""

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


OUTPUT_DIR = ROOT / "analysis" / "a144_d10_r02_validation_20260923"


def configs() -> tuple[Config, Config]:
    base = Config(
        name="current_d12_r04",
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True)
    candidate = replace(
        base,
        name="candidate_d10_r02",
        weak_exit_min_days=10,
        weak_exit_max_return=-0.02)
    return base, candidate


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base, candidate = configs()
    yearly_periods = [
        ("year_2022", "20220104", "20221230"),
        ("year_2023", "20230103", "20231229"),
        ("year_2024", "20240102", "20241231"),
        ("year_2025", "20250102", "20251231"),
        ("year_2026", "20260105", "20260918"),
    ]
    yearly_rows = []
    for period, start, end in yearly_periods:
        for config in (base, candidate):
            print("{} {}".format(period, config.name), flush=True)
            metrics, _, _ = run_backtest(frame, config, start, end)
            metrics["period"] = period
            yearly_rows.append(metrics)
    yearly = pd.DataFrame(yearly_rows)

    phase_rows = []
    for offset in range(10):
        for config in (base, candidate):
            print("phase {} {}".format(offset, config.name), flush=True)
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
    phase_summary = phases.groupby("family").agg({
        "annual_return": ["min", "median", "max"],
        "max_drawdown": ["min", "median", "max"],
        "sharpe": ["min", "median", "max"],
    })
    phase_summary.columns = [
        "_".join(values) for values in phase_summary.columns]
    phase_summary = phase_summary.reset_index()

    indexed = phases.set_index(["refresh_offset", "family"])
    phase_deltas = []
    for offset in range(10):
        base_row = indexed.loc[(offset, base.name)]
        candidate_row = indexed.loc[(offset, candidate.name)]
        phase_deltas.append({
            "refresh_offset": offset,
            "annual_delta": (
                candidate_row["annual_return"] - base_row["annual_return"]),
            "drawdown_delta": (
                candidate_row["max_drawdown"] - base_row["max_drawdown"]),
            "sharpe_delta": candidate_row["sharpe"] - base_row["sharpe"],
        })
    deltas = pd.DataFrame(phase_deltas)
    pass_count = int((deltas["annual_delta"] > 0).sum())

    yearly.to_csv(
        OUTPUT_DIR / "yearly.csv",
        index=False,
        encoding="utf-8-sig")
    phases.to_csv(
        OUTPUT_DIR / "refresh_phases.csv",
        index=False,
        encoding="utf-8-sig")
    deltas.to_csv(
        OUTPUT_DIR / "refresh_phase_deltas.csv",
        index=False,
        encoding="utf-8-sig")
    phase_summary.to_csv(
        OUTPUT_DIR / "refresh_phase_summary.csv",
        index=False,
        encoding="utf-8-sig")
    payload = {
        "annual_return_phase_wins": pass_count,
        "annual_return_phase_total": 10,
        "phase_summary": phase_summary.to_dict(orient="records"),
        "phase_deltas": deltas.to_dict(orient="records"),
        "yearly": yearly.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("annual_return_phase_wins={}/10".format(pass_count), flush=True)
    print(phase_summary.to_string(index=False), flush=True)
    print(deltas.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
