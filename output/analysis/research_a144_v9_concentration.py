# -*- coding: utf-8 -*-
"""Pre-registered A144 concentration study using the corrected causal replay."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis.research_a144_v5_trend import baseline_signals
from output.analysis.research_a144_v5_trend import load_features
from Stragety.RedisQMT.A144.core.bridge_minutes import BridgeMinutes
from Stragety.RedisQMT.A144.core.bridge_minutes import _log
from Stragety.RedisQMT.A144.core.replay import metrics
from Stragety.RedisQMT.A144.core.replay import replay
from Stragety.RedisQMT.A144.core.trend import TrendConfig


OUT = ROOT / "analysis/a144_v9_concentration_20260926"


def configs():
    base = TrendConfig(name="a144_max5", max_positions=5)
    return [replace(base, name=f"a144_max{count}", max_positions=count)
            for count in (2, 3, 4, 5)]


def save(label, result):
    result[1].to_csv(OUT / f"{label}_equity.csv", index=False)
    result[2].to_csv(OUT / f"{label}_trades.csv", index=False)
    result[3].to_csv(OUT / f"{label}_fills.csv", index=False)
    (OUT / f"{label}_metrics.json").write_text(json.dumps(result[0], indent=2), encoding="utf-8")


def segments(equity):
    output = {}
    previous = 1_000_000.0
    for year, rows in equity.groupby(equity["date"].str[:4]):
        output[year] = metrics(rows, previous)
        previous = float(rows.iloc[-1]["equity"])
    before = equity.loc[equity["date"] < "20250101"]
    after = equity.loc[equity["date"] >= "20250101"]
    output["2025_plus_continuous"] = metrics(after, float(before.iloc[-1]["equity"]))
    return output


def screen(frame):
    prepared = baseline_signals(frame)
    training = []
    for config in configs():
        result = replay(prepared, config, "20220104", "20241231", baseline=True)
        row = dict(result[0], name=config.name, max_positions=config.max_positions)
        training.append(row)
        _log(json.dumps(row))
    eligible = [row for row in training
                if row["max_drawdown"] >= -0.35 and row["trades"] >= 30]
    selected = max(eligible, key=lambda row: row["annual_return"] / max(abs(row["max_drawdown"]), 0.05))
    config = next(item for item in configs() if item.name == selected["name"])
    pd.DataFrame(training).to_csv(OUT / "training.csv", index=False)
    (OUT / "selected.json").write_text(json.dumps({"name": config.name,
                                                     "max_positions": config.max_positions}, indent=2),
                                        encoding="utf-8")
    full = replay(prepared, config, "20220104", "20260918", baseline=True)
    save("daily_candidate", full)
    (OUT / "daily_candidate_segments.json").write_text(
        json.dumps(segments(full[1]), indent=2), encoding="utf-8")
    double = replay(prepared, config, "20220104", "20260918", baseline=True, cost_scale=2.0)
    save("daily_cost2", double)
    pnl = full[2]["pnl"].sort_values(ascending=False)
    (OUT / "stress.json").write_text(json.dumps({
        "double_cost": double[0], "realized_pnl": float(pnl.sum()),
        "realized_pnl_without_top": {str(n): float(pnl.iloc[n:].sum()) for n in (1, 5, 10)}
    }, indent=2), encoding="utf-8")
    _log("selected " + json.dumps({"name": config.name, "max_positions": config.max_positions}))
    _log("full " + json.dumps(full[0]))


def validate(frame, refresh=False):
    chosen = json.loads((OUT / "selected.json").read_text(encoding="utf-8"))
    config = TrendConfig(name=chosen["name"], max_positions=chosen["max_positions"])
    prepared = baseline_signals(frame)
    minute = BridgeMinutes(OUT / "minutes", "20220104", "20260918", refresh=refresh)
    summary = {}
    try:
        result = replay(prepared, config, "20220104", "20260918", execution=minute, baseline=True)
        save("minute_candidate", result)
        summary["minute_candidate"] = dict(result[0], segments=segments(result[1]))
    except (ValueError, TimeoutError, ConnectionError) as exc:
        summary["minute_candidate"] = {"blocked": True, "reason": str(exc)}
    value = summary["minute_candidate"]
    summary["claim_36_percent_validated"] = bool(
        not value.get("blocked") and value.get("annual_return", 0) >= 0.36 and
        value.get("max_drawdown", -1) >= -0.35)
    (OUT / "validation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["screen", "validate"])
    parser.add_argument("--refresh-minutes", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    frame = load_features()
    _log(f"loaded {len(frame)} RedisQMT daily rows")
    if args.command == "screen":
        screen(frame)
    else:
        validate(frame, args.refresh_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
