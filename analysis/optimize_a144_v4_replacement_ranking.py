"""Out-of-sample screen for A144 v4 replacement-candidate ranking.

The eligibility pool is kept identical to the current raw-Alpha144 strategy.
Only the order among already eligible candidates changes.  Variants are
selected on 2022-2024 and evaluated unchanged from 2025 onward.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.backtest_a144_v4_candidate_replacement import current_config
from analysis.backtest_a144_v4_candidate_replacement import ranked_codes_on
from analysis.research_a144_redisqmt import BUY_COST
from analysis.research_a144_redisqmt import add_features
from analysis.research_a144_redisqmt import entry_candidates
from analysis.research_a144_redisqmt import load_panel
from analysis.research_a144_redisqmt import market_allows_entry
from analysis.research_a144_redisqmt import sell_cost


OUTPUT_DIR = ROOT / "analysis" / "a144_v4_replacement_ranking_20260924"
TRAIN_END = "20241231"
HORIZONS = (5, 10, 20)


VARIANTS = {
    "alpha_raw": (1.0, 0.0, 0.0, 0.0),
    "alpha_quality20": (0.8, 0.0, 0.2, 0.0),
    "alpha_quality40": (0.6, 0.0, 0.4, 0.0),
    "alpha_mom20": (0.8, 0.2, 0.0, 0.0),
    "alpha_mom40": (0.6, 0.4, 0.0, 0.0),
    "balanced": (0.6, 0.15, 0.15, 0.10),
    "quality_momentum": (0.5, 0.2, 0.2, 0.1),
}


def variant_score(rows, weights):
    alpha_weight = weights[0]
    momentum_weight = weights[1]
    quality_weight = weights[2]
    short_momentum_weight = weights[3]
    short_momentum = rows["mom20"].rank(pct=True)
    return (
        rows["alpha_raw_pct"] * alpha_weight +
        rows["mom60_pct"] * momentum_weight +
        rows["quality_pct"] * quality_weight +
        short_momentum * short_momentum_weight)


def candidate_observations(frame):
    config = current_config()
    dates = sorted(frame.loc[
        frame["date"] >= config.refresh_anchor,
        "date"].unique())
    by_date = {
        date: rows.copy()
        for date, rows in frame.groupby("date", sort=False)
    }
    ranked_cache = {}
    observations = []
    for signal_index, signal_date in enumerate(dates):
        if signal_date < "20220104":
            continue
        if signal_index + max(HORIZONS) + 1 >= len(dates):
            continue
        rows = by_date[signal_date].copy()
        if rows.empty or not market_allows_entry(rows.iloc[0], config):
            continue
        refresh_index = signal_index - signal_index % config.refresh_days
        refresh_date = dates[refresh_index]
        if refresh_date not in ranked_cache:
            ranked_cache[refresh_date] = ranked_codes_on(
                frame,
                refresh_date,
                config)
        candidates = entry_candidates(
            rows,
            config,
            ranked_cache[refresh_date]).copy()
        if candidates.empty:
            continue
        entry_date = dates[signal_index + 1]
        entry_rows = by_date[entry_date].set_index("code")
        for variant, weights in VARIANTS.items():
            ranked = candidates.copy()
            ranked["replacement_score"] = variant_score(ranked, weights)
            ranked = ranked.sort_values(
                ["replacement_score", "alpha_raw_pct"],
                ascending=False)
            selected = None
            for candidate in ranked.itertuples(index=False):
                if candidate.code not in entry_rows.index:
                    continue
                entry_row = entry_rows.loc[candidate.code]
                if not bool(entry_row["tradable"]) or bool(entry_row["cannot_buy"]):
                    continue
                entry_price = float(entry_row["adj_open"])
                if entry_price <= 0:
                    continue
                selected = candidate
                break
            if selected is None:
                continue
            record = {
                "variant": variant,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "code": selected.code,
                "candidate_count": int(len(candidates)),
                "replacement_score": float(selected.replacement_score),
                "entry_price": entry_price,
            }
            for horizon in HORIZONS:
                exit_date = dates[signal_index + 1 + horizon]
                exit_rows = by_date[exit_date].set_index("code")
                if selected.code not in exit_rows.index:
                    record["return_{}d".format(horizon)] = np.nan
                    continue
                exit_row = exit_rows.loc[selected.code]
                if not bool(exit_row["tradable"]) or bool(exit_row["cannot_sell"]):
                    record["return_{}d".format(horizon)] = np.nan
                    continue
                exit_price = float(exit_row["adj_open"])
                net_return = (
                    exit_price * (1.0 - sell_cost(exit_date)) /
                    (entry_price * (1.0 + BUY_COST)) - 1.0)
                record["return_{}d".format(horizon)] = net_return
            observations.append(record)
    return pd.DataFrame(observations)


def metrics(rows):
    output = {"observations": int(len(rows))}
    for horizon in HORIZONS:
        values = rows["return_{}d".format(horizon)].dropna()
        prefix = "{}d_".format(horizon)
        output[prefix + "count"] = int(len(values))
        output[prefix + "mean"] = float(values.mean()) if len(values) else 0.0
        output[prefix + "median"] = float(values.median()) if len(values) else 0.0
        output[prefix + "win_rate"] = float((values > 0).mean()) if len(values) else 0.0
        output[prefix + "p10"] = float(values.quantile(0.10)) if len(values) else 0.0
        output[prefix + "worst"] = float(values.min()) if len(values) else 0.0
    return output


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading daily panel", flush=True)
    frame = add_features(load_panel())
    frame = frame.loc[
        (frame["date"] >= "20210101") &
        (frame["date"] <= "20260918")].copy()
    observations = candidate_observations(frame)
    observations.to_csv(
        OUTPUT_DIR / "daily_top_candidates.csv",
        index=False,
        encoding="utf-8-sig")
    rows = []
    for variant in VARIANTS:
        variant_rows = observations.loc[observations["variant"] == variant]
        train = variant_rows.loc[variant_rows["signal_date"] <= TRAIN_END]
        holdout = variant_rows.loc[variant_rows["signal_date"] > TRAIN_END]
        full = variant_rows
        row = {"variant": variant}
        for prefix, sample in (
                ("train", train),
                ("holdout", holdout),
                ("full", full)):
            for name, value in metrics(sample).items():
                row["{}_{}".format(prefix, name)] = value
        rows.append(row)
    comparison = pd.DataFrame(rows)
    baseline = comparison.loc[comparison["variant"] == "alpha_raw"].iloc[0]
    eligible = comparison.loc[
        (comparison["train_20d_p10"] >= baseline["train_20d_p10"]) &
        (comparison["train_20d_median"] > baseline["train_20d_median"])].copy()
    eligible = eligible.sort_values(
        ["train_20d_median", "train_20d_p10"],
        ascending=False)
    selected = eligible.iloc[0] if len(eligible) else baseline
    holdout_pass = bool(
        selected["holdout_20d_median"] >= baseline["holdout_20d_median"] and
        selected["holdout_20d_p10"] >= baseline["holdout_20d_p10"] and
        selected["holdout_20d_mean"] >= baseline["holdout_20d_mean"])
    comparison.to_csv(
        OUTPUT_DIR / "variant_comparison.csv",
        index=False,
        encoding="utf-8-sig")
    summary = {
        "method": "fixed raw-Alpha eligibility; train 2022-2024; holdout 2025+",
        "variants": len(VARIANTS),
        "baseline": baseline.to_dict(),
        "selected": selected.to_dict(),
        "selected_weights": VARIANTS[str(selected["variant"])],
        "selected_passes_holdout": holdout_pass,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
