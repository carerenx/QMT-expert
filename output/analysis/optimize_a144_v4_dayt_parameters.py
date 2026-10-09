"""Walk-forward parameter screen for the A144 v4 reverse-T entry.

This research replays the cached five-minute sessions used by the v3 study.
The old forced/emergency close remains in the simulator only as a conservative
penalty for an unresolved leg.  Parameters are selected on the early sample
and reported unchanged on the later sample.  No live state or order API is
used.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis import backtest_a144_v3_reverse_t as replay
from output.analysis.research_a144_redisqmt import add_features
from output.analysis.research_a144_redisqmt import load_panel
from output.analysis.research_a144_redisqmt import run_backtest
from Stragety.RedisQMT.A144 import config_v3


OUTPUT_DIR = ROOT / "analysis" / "a144_v4_dayt_parameter_screen_20260924"
TRAIN_END = "20260814"


PARAMETER_GRID = {
    "DAYT_SELL_ATR_MULT": (0.40, 0.60, 0.80),
    "DAYT_SELL_RISE_MIN_PCT": (0.012, 0.016, 0.020),
    "DAYT_ARM_EXTENSION_PCT": (0.0015, 0.0030, 0.0050),
    "DAYT_SELL_PULLBACK_PCT": (0.0020, 0.0040, 0.0060),
    "DAYT_ARM_MIN_SAMPLES": (2, 3),
}


def prepare_inputs():
    frame = add_features(load_panel())
    config = replay.current_v2_config()
    _, _, trades = run_backtest(frame, config, "20220104", "20260918")
    date_mask = (frame["date"] >= replay.START) & (frame["date"] <= replay.END)
    trading_dates = sorted(frame.loc[date_mask, "date"].unique())
    sessions = replay.eligible_trade_sessions(trades, trading_dates)
    cache = replay.cached_paths("sensitivity_5m")
    sessions = sessions.loc[sessions["code"].isin(cache)].copy()
    panel_lookup = frame.loc[date_mask].set_index(["code", "date"])
    loaded = {}
    for code, paths in cache.items():
        minute = pd.read_csv(paths["minute"], dtype={"time": str})
        minute = minute.set_index("time").sort_index()
        raw = pd.read_csv(paths["raw"], dtype={"time": str})
        raw = raw.set_index("time").sort_index()
        loaded[code] = (minute, raw)
    return sessions.reset_index(drop=True), panel_lookup, loaded


def summarize(results, start, end):
    subset = results.loc[
        (results["date"] >= start) &
        (results["date"] <= end) &
        (results["status"] == "completed")].copy()
    normal = subset.loc[subset["buy_reason"] == "normal-buyback"]
    bad = subset.loc[subset["buy_reason"] != "normal-buyback"]
    return {
        "cycles": int(len(subset)),
        "normal_cycles": int(len(normal)),
        "bad_cycles": int(len(bad)),
        "bad_rate": float(len(bad) / len(subset)) if len(subset) else 0.0,
        "net_pnl": float(subset["net_pnl"].sum()) if len(subset) else 0.0,
        "normal_net_pnl": float(normal["net_pnl"].sum()) if len(normal) else 0.0,
        "bad_net_pnl": float(bad["net_pnl"].sum()) if len(bad) else 0.0,
        "win_rate": float((subset["net_pnl"] > 0).mean()) if len(subset) else 0.0,
    }


def replay_parameters(sessions, panel_lookup, loaded, parameters):
    original = {name: getattr(config_v3, name) for name in parameters}
    try:
        for name, value in parameters.items():
            setattr(config_v3, name, value)
        rows = []
        for session in sessions.to_dict(orient="records"):
            code = str(session["code"])
            key = (code, str(session["date"]))
            if key not in panel_lookup.index:
                continue
            minute = loaded[code][0]
            raw = loaded[code][1]
            rows.append(replay.replay_session(
                pd.Series(session),
                minute,
                raw,
                panel_lookup.loc[key]))
        return pd.DataFrame(rows)
    finally:
        for name, value in original.items():
            setattr(config_v3, name, value)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sessions, panel_lookup, loaded = prepare_inputs()
    names = list(PARAMETER_GRID)
    combinations = itertools.product(*(PARAMETER_GRID[name] for name in names))
    rows = []
    detailed = {}
    for index, values in enumerate(combinations, start=1):
        parameters = dict(zip(names, values))
        results = replay_parameters(
            sessions,
            panel_lookup,
            loaded,
            parameters)
        train = summarize(results, replay.START, TRAIN_END)
        validation = summarize(results, "20260815", replay.END)
        full = summarize(results, replay.START, replay.END)
        row = dict(parameters)
        for prefix, metrics in (
                ("train", train),
                ("validation", validation),
                ("full", full)):
            for key, value in metrics.items():
                row["{}_{}".format(prefix, key)] = value
        rows.append(row)
        detailed[index] = results
    screen = pd.DataFrame(rows)
    baseline_mask = pd.Series(True, index=screen.index)
    baseline_values = {
        "DAYT_SELL_ATR_MULT": 0.40,
        "DAYT_SELL_RISE_MIN_PCT": 0.012,
        "DAYT_ARM_EXTENSION_PCT": 0.0015,
        "DAYT_SELL_PULLBACK_PCT": 0.0020,
        "DAYT_ARM_MIN_SAMPLES": 2,
    }
    for name, value in baseline_values.items():
        baseline_mask &= screen[name] == value
    baseline = screen.loc[baseline_mask].iloc[0]
    eligible = screen.loc[
        (screen["train_cycles"] >= 8) &
        (screen["train_normal_cycles"] >= 5) &
        (screen["train_net_pnl"] > baseline["train_net_pnl"])].copy()
    eligible = eligible.sort_values(
        ["train_net_pnl", "train_bad_rate"],
        ascending=[False, True])
    selected = eligible.iloc[0] if len(eligible) else baseline
    selected_index = int(selected.name) + 1
    selected_results = detailed[selected_index]
    screen.to_csv(
        OUTPUT_DIR / "parameter_screen.csv",
        index=False,
        encoding="utf-8-sig")
    selected_results.to_csv(
        OUTPUT_DIR / "selected_session_results.csv",
        index=False,
        encoding="utf-8-sig")
    summary = {
        "method": "train-select through 2026-08-14; frozen validation afterward",
        "bar_period": "5-minute sensitivity",
        "grid_size": int(len(screen)),
        "baseline": baseline.to_dict(),
        "selected": selected.to_dict(),
        "selected_passes_validation": bool(
            selected["validation_net_pnl"] > baseline["validation_net_pnl"] and
            selected["validation_bad_rate"] <= baseline["validation_bad_rate"]),
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
