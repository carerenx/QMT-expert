# -*- coding: utf-8 -*-
"""Validate shorter Alpha144 ranking refresh intervals."""

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


OUTPUT_DIR = ROOT / "analysis" / "a144_refresh_optimization_20260923"


def base_config() -> Config:
    return Config(
        name="refresh_10",
        refresh_days=10,
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading panel", flush=True)
    frame = add_features(load_panel())
    base = base_config()
    intervals = (1, 2, 5, 20)
    configs = [base]
    for interval in intervals:
        configs.append(replace(
            base,
            name="refresh_{}".format(interval),
            refresh_days=interval))
    periods = [
        ("train", "20220104", "20241231"),
        ("holdout", "20250102", "20260918"),
        ("full", "20220104", "20260918"),
    ]
    rows = []
    for period, start, end in periods:
        for config in configs:
            print("{} {}".format(period, config.name), flush=True)
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
            deltas["full"]["sharpe"] >= 0)
        gate_rows.append({
            "name": config.name,
            "refresh_days": config.refresh_days,
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
    selected = base.name if passing.empty else str(passing.iloc[0]["name"])

    raw.to_csv(
        OUTPUT_DIR / "period_results.csv",
        index=False,
        encoding="utf-8-sig")
    gate.to_csv(
        OUTPUT_DIR / "robustness_gate.csv",
        index=False,
        encoding="utf-8-sig")
    payload = {
        "selected": selected,
        "gate": gate.to_dict(orient="records"),
        "metrics": raw.to_dict(orient="records"),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("selected={}".format(selected), flush=True)
    print(gate.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
