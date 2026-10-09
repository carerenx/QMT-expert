# -*- coding: utf-8 -*-
"""Pre-registered RedisQMT CSI1000 liquidity-shock repair study."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.A144.core.bridge_minutes import BridgeMinutes
from Stragety.RedisQMT.A144.core.bridge_minutes import _log
from Stragety.RedisQMT.A144.core.replay import replay
from Stragety.RedisQMT.A144.core.trend import TrendConfig
from Stragety.RedisQMT.A144.core.trend import features


OUT = ROOT / "analysis/a144_v8_csi1000_20260926"
PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
BENCHMARK = ROOT / "analysis/a144_v5_trend_20260926/benchmark_1d.parquet"


@dataclass(frozen=True)
class Candidate:
    name: str
    mode: str
    strict_market: bool = False


def configurations():
    return [Candidate("shock_turn", "turn"),
            Candidate("shock_turn_strict", "turn", True),
            Candidate("shock_ma20_repair", "ma20"),
            Candidate("shock_breakout", "breakout"),
            Candidate("neutral_shock_turn", "neutral"),
            Candidate("shock_dual_repair", "dual")]


def prepare():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    connection = configure(timeout_seconds=20)
    xtdata = connection[1]
    codes = sorted(set(xtdata.get_stock_list_in_sector("中证1000") or []))
    if len(codes) < 900:
        raise ValueError(f"incomplete RedisQMT CSI1000 universe: {len(codes)}")
    payload = {"source": "RedisQMT bridge sector API", "sector": "中证1000",
               "frozen_on": "20260926", "codes": codes}
    (OUT / "universe.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"froze {len(codes)} RedisQMT CSI1000 codes")


def load_frame():
    universe = json.loads((OUT / "universe.json").read_text(encoding="utf-8"))
    if universe.get("source") != "RedisQMT bridge sector API":
        raise ValueError("invalid universe provenance")
    columns = ["code", "time", "open", "high", "low", "close", "adj_open",
               "adj_high", "adj_low", "adj_close", "amount", "volume"]
    frame = pd.read_parquet(PANEL, columns=columns,
                            filters=[("code", "in", universe["codes"]),
                                     ("time", ">=", "20210101")])
    frame = frame.rename(columns={"time": "date"})
    benchmark = pd.read_parquet(BENCHMARK)
    frame = features(frame, benchmark)
    group = frame.groupby("code", sort=False)
    frame["ret5"] = group["adj_close"].pct_change(5, fill_method=None)
    frame["previous_ma20"] = group["ma20"].shift()
    frame["amount_median60"] = group["amount"].transform(lambda x: x.rolling(60).median())
    frame["neutral_impact"] = np.where(
        frame["ret"] < 0,
        -frame["ret"] * frame["amount_median60"] / frame["amount"].replace(0, np.nan), 0.0)
    frame["neutral_impact20"] = group["neutral_impact"].transform(lambda x: x.rolling(20).sum())
    frame["neutral_impact_rank"] = frame.groupby("date")["neutral_impact20"].rank(pct=True)
    return frame


def prepare_signals(frame, candidate):
    result = frame.copy()
    impact_rank = result["neutral_impact_rank"] if candidate.mode == "neutral" else result["impact20_rank"]
    shock = ((impact_rank >= 0.85) & result["ret5"].between(-0.15, -0.03))
    turn = ((result["ret"] > 0.01) & (result["adj_close"] > result["ma10"]))
    crossed_ma20 = ((result["previous"] <= result["previous_ma20"]) &
                    (result["adj_close"] > result["ma20"]) & (result["ret"] > 0))
    breakout = ((result["adj_close"] > result["high20"]) &
                (result["amount"] > result["amount5_previous"] * 1.1))
    modes = {"turn": turn, "neutral": turn, "ma20": crossed_ma20,
             "breakout": breakout, "dual": turn | crossed_ma20}
    market = result["market_strict"] if candidate.strict_market else result["market_ok"]
    result["entry"] = (shock & modes[candidate.mode] & market &
                       (result["age"] >= 130) & (result["amount20"] >= 3e7) &
                       result["atr_pct"].between(0.01, 0.10) & (result["ret"] < 0.095))
    result["score"] = impact_rank + result["ret"].clip(lower=0, upper=0.095)
    result["weight"] = np.minimum(0.20, 0.04 / (4.0 * result["atr_pct"]))
    result["market_exit"] = ~market
    return result


def strategy_config(name):
    return TrendConfig(name=name, entry="repair", trail_atr=4.0,
                       risk_per_position=0.04, max_weight=0.20,
                       hard_stop=0.10, cooldown=10)


def save(label, result):
    result[1].to_csv(OUT / f"{label}_equity.csv", index=False)
    result[2].to_csv(OUT / f"{label}_trades.csv", index=False)
    result[3].to_csv(OUT / f"{label}_fills.csv", index=False)
    (OUT / f"{label}_metrics.json").write_text(json.dumps(result[0], indent=2), encoding="utf-8")


def segment_metrics(equity):
    from Stragety.RedisQMT.A144.core.replay import metrics
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
    rows = []
    for candidate in configurations():
        prepared = prepare_signals(frame, candidate)
        result = replay(prepared, strategy_config(candidate.name), "20220104", "20241231")
        row = dict(result[0], name=candidate.name)
        rows.append(row)
        _log(json.dumps(row))
    eligible = [row for row in rows
                if row["max_drawdown"] >= -0.35 and row["trades"] >= 30]
    if not eligible:
        raise RuntimeError("no candidate passed drawdown and minimum-trade gates")
    selected = max(eligible, key=lambda row: row["annual_return"] / max(abs(row["max_drawdown"]), 0.05))
    candidate = next(item for item in configurations() if item.name == selected["name"])
    pd.DataFrame(rows).to_csv(OUT / "training.csv", index=False)
    (OUT / "selected.json").write_text(json.dumps(asdict(candidate), indent=2), encoding="utf-8")
    prepared = prepare_signals(frame, candidate)
    full = replay(prepared, strategy_config(candidate.name), "20220104", "20260918")
    save("daily_candidate", full)
    (OUT / "daily_candidate_segments.json").write_text(
        json.dumps(segment_metrics(full[1]), indent=2), encoding="utf-8")
    double = replay(prepared, strategy_config(candidate.name), "20220104", "20260918", cost_scale=2.0)
    save("daily_cost2", double)
    pnl = full[2]["pnl"].sort_values(ascending=False) if len(full[2]) else pd.Series(dtype=float)
    stress = {"double_cost": double[0], "realized_pnl": float(pnl.sum()),
              "realized_pnl_without_top": {str(n): float(pnl.iloc[n:].sum()) for n in (1, 5, 10)}}
    (OUT / "stress.json").write_text(json.dumps(stress, indent=2), encoding="utf-8")
    _log("selected " + json.dumps(asdict(candidate)))
    _log("full " + json.dumps(full[0]))


def validate(frame, refresh=False):
    candidate = Candidate(**json.loads((OUT / "selected.json").read_text(encoding="utf-8")))
    prepared = prepare_signals(frame, candidate)
    minute = BridgeMinutes(OUT / "minutes", "20220104", "20260918", refresh=refresh)
    summary = {}
    for label, scale in (("minute_candidate", 1.0), ("minute_cost2", 2.0)):
        try:
            result = replay(prepared, strategy_config(candidate.name), "20220104", "20260918",
                            execution=minute, cost_scale=scale)
            save(label, result)
            summary[label] = dict(result[0], segments=segment_metrics(result[1]))
        except (ValueError, TimeoutError, ConnectionError) as exc:
            summary[label] = {"blocked": True, "reason": str(exc)}
            _log(label + " blocked: " + str(exc))
    metrics_value = summary.get("minute_candidate", {})
    summary["claim_36_percent_validated"] = bool(
        not metrics_value.get("blocked") and metrics_value.get("annual_return", 0) >= 0.36 and
        metrics_value.get("max_drawdown", -1) >= -0.35)
    (OUT / "validation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "screen", "validate"])
    parser.add_argument("--refresh-minutes", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        prepare()
        return 0
    frame = load_frame()
    _log(f"loaded {len(frame)} RedisQMT CSI1000 daily rows")
    if args.command == "screen":
        screen(frame)
    else:
        validate(frame, args.refresh_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
