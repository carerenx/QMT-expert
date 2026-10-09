# -*- coding: utf-8 -*-
"""Pre-registered periodic Alpha144 factor rotation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.research_a144_v7_rotation import OUT as UNUSED_OUT
from analysis.research_a144_v7_rotation import RotationConfig
from analysis.research_a144_v7_rotation import prepare_features
from analysis.research_a144_v7_rotation import replay_rotation
from analysis.research_a144_v7_rotation import save
from analysis.research_a144_v7_rotation import period_metrics
from Stragety.RedisQMT.A144.core.bridge_minutes import BridgeMinutes
from Stragety.RedisQMT.A144.core.bridge_minutes import _log


OUT = ROOT / "analysis/a144_v10_factor_rotation_20260926"


def configurations():
    return [
        RotationConfig("impact_top5_r10", "impact20", 10, 5),
        RotationConfig("impact_top5_r20", "impact20", 20, 5),
        RotationConfig("impact_top10_r10", "impact20", 10, 10),
        RotationConfig("neutral_impact_top5_r10", "neutral_impact20", 10, 5),
        RotationConfig("impact_momentum_top5_r10", "factor_blend", 10, 5),
        RotationConfig("impact_top5_r10_breadth", "impact20", 10, 5, True),
    ]


def build_frame():
    frame = prepare_features()
    group = frame.groupby("code", sort=False)
    frame["amount_median60"] = group["amount"].transform(lambda x: x.rolling(60).median())
    frame["neutral_impact"] = frame["impact"] * frame["amount_median60"]
    frame["neutral_impact20"] = group["neutral_impact"].transform(lambda x: x.rolling(20).sum())
    daily = frame.groupby("date", sort=False)
    frame["neutral_impact20_rank"] = daily["neutral_impact20"].rank(pct=True)
    frame["factor_blend"] = 0.60 * frame["impact20_rank"] + 0.40 * frame["mom60_rank"]
    frame["factor_blend_rank"] = daily["factor_blend"].rank(pct=True)
    return frame


def save_local(label, result):
    result[1].to_csv(OUT / f"{label}_equity.csv", index=False)
    result[2].to_csv(OUT / f"{label}_trades.csv", index=False)
    result[3].to_csv(OUT / f"{label}_fills.csv", index=False)
    (OUT / f"{label}_metrics.json").write_text(json.dumps(result[0], indent=2), encoding="utf-8")


def screen(frame):
    rows = []
    for config in configurations():
        result = replay_rotation(frame, config, "20220104", "20241231")
        row = dict(result[0], name=config.name)
        rows.append(row)
        _log(json.dumps(row))
    eligible = [row for row in rows if row["max_drawdown"] >= -0.35 and row["trades"] >= 30]
    selected = max(eligible, key=lambda row: row["annual_return"] / max(abs(row["max_drawdown"]), 0.05))
    config = next(item for item in configurations() if item.name == selected["name"])
    pd.DataFrame(rows).to_csv(OUT / "training.csv", index=False)
    (OUT / "selected.json").write_text(json.dumps(config.__dict__, indent=2), encoding="utf-8")
    full = replay_rotation(frame, config, "20220104", "20260918")
    save_local("daily_candidate", full)
    (OUT / "daily_candidate_segments.json").write_text(
        json.dumps(period_metrics(full[1]), indent=2), encoding="utf-8")
    double = replay_rotation(frame, config, "20220104", "20260918", cost_scale=2.0)
    save_local("daily_cost2", double)
    pnl = full[2]["pnl"].sort_values(ascending=False)
    (OUT / "stress.json").write_text(json.dumps({
        "double_cost": double[0], "realized_pnl": float(pnl.sum()),
        "realized_pnl_without_top": {str(n): float(pnl.iloc[n:].sum()) for n in (1, 5, 10)}
    }, indent=2), encoding="utf-8")
    _log("selected " + json.dumps(config.__dict__))
    _log("full " + json.dumps(full[0]))


def validate(frame, refresh=False):
    config = RotationConfig(**json.loads((OUT / "selected.json").read_text(encoding="utf-8")))
    minute = BridgeMinutes(OUT / "minutes", "20220104", "20260918", refresh=refresh)
    summary = {}
    try:
        result = replay_rotation(frame, config, "20220104", "20260918", minute)
        save_local("minute_candidate", result)
        summary["minute_candidate"] = dict(result[0], segments=period_metrics(result[1]))
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
    frame = build_frame()
    _log(f"loaded {len(frame)} RedisQMT daily rows")
    if args.command == "screen":
        screen(frame)
    else:
        validate(frame, args.refresh_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
