# -*- coding: utf-8 -*-
"""Pre-registered trend alternatives: screen, then strict RedisQMT minute replay."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Stragety.RedisQMT.A144.core.bridge_minutes import BridgeMinutes
from Stragety.RedisQMT.A144.core.bridge_minutes import _log
from Stragety.RedisQMT.A144.core.replay import metrics
from Stragety.RedisQMT.A144.core.replay import replay
from Stragety.RedisQMT.A144.core.trend import TrendConfig
from Stragety.RedisQMT.A144.core.trend import features
from Stragety.RedisQMT.A144.core.trend import signals


OUT = ROOT / "analysis/a144_v5_trend_20260926"
PANEL = ROOT / "analysis/panel_20260920/panel.parquet"


def candidates():
    base = TrendConfig()
    return [replace(base, name="breakout", entry="breakout"),
            replace(base, name="breakout_impact", entry="breakout", impact_weight=0.3),
            replace(base, name="pullback", entry="pullback"),
            base,
            replace(base, name="dual_wide_trail", trail_atr=4.0),
            replace(base, name="dual_strict_market", strict_market=True)]


def leader_candidates():
    base = TrendConfig(entry="leader", trail_atr=5.0, risk_per_position=0.03)
    return [replace(base, name="leader20", leader_horizon=20),
            replace(base, name="leader60", leader_horizon=60),
            replace(base, name="leader120", leader_horizon=120),
            replace(base, name="leader60_strict", leader_horizon=60, strict_market=True)]


def load_features():
    universe = json.loads((OUT / "universe.json").read_text(encoding="utf-8"))
    if len(universe["codes"]) < 400 or "bridge" not in universe["source"]:
        raise ValueError("missing RedisQMT universe provenance")
    columns = ["code", "time", "open", "high", "low", "close", "adj_open",
               "adj_high", "adj_low", "adj_close", "amount", "volume"]
    frame = pd.read_parquet(PANEL, columns=columns,
                            filters=[("code", "in", universe["codes"]), ("time", ">=", "20210101")])
    frame = frame.rename(columns={"time": "date"})
    if frame.duplicated(["code", "date"]).any():
        raise ValueError("duplicate daily key")
    benchmark = pd.read_parquet(OUT / "benchmark_1d.parquet")
    return features(frame, benchmark)


def prepare():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    connection = configure(timeout_seconds=20)
    xtdata = connection[1]
    codes = sorted(set(xtdata.get_stock_list_in_sector("中证500") or []))
    if len(codes) < 400:
        raise ValueError("incomplete RedisQMT universe")
    universe = {"source": "RedisQMT bridge sector API", "asof": pd.Timestamp.now().strftime("%Y%m%d"),
                "codes": codes}
    data = xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close", "volume", "amount"],
        stock_list=["000905.SH"], period="1d", start_time="20210101", end_time="20260918",
        count=-1, dividend_type="none", fill_data=False, timeout_seconds=30, bypass_formula=True)
    benchmark = (data or {}).get("000905.SH")
    if benchmark is None or benchmark.empty or (benchmark["close"] <= 0).any():
        raise ValueError("invalid benchmark")
    (OUT / "universe.json").write_text(json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8")
    benchmark.to_parquet(OUT / "benchmark_1d.parquet")
    _log("prepared RedisQMT universe and daily benchmark")


def baseline_signals(frame):
    frame = frame.copy()
    frame["entry"] = False
    ranked = set()
    grouped = frame.loc[frame["date"] >= "20220104"].groupby("date", sort=True)
    for day_index, daily in enumerate(grouped):
        rows = daily[1]
        if day_index % 10 == 0:
            valid = rows.loc[rows["age"] >= 130].dropna(subset=["impact20"])
            ranked = set(valid.nlargest(max(1, int(len(valid) * 0.15)), "impact20")["code"])
        mask = (rows["code"].isin(ranked) & (rows["age"] >= 130) & rows["market_ok"] &
                (rows["amount20"] >= 3e7) & (rows["ret"] < 0.098) &
                (rows["adj_close"] > rows["high10close"] * 1.005) &
                (rows["amount"] >= rows["amount5_previous"] * 1.2) &
                (rows["adj_close"] > rows["ma20"]) & (rows["ma20"] > rows["ma20_lag5"]) &
                ((rows["ret"] > 0).astype(int) + rows["up1"].astype(int) + rows["up2"].astype(int) >= 2) &
                (rows["minret60"] > -0.07) & (~rows[["up1", "up2", "up3"]].all(axis=1)) &
                (rows["maxrange5"] <= 0.1) & (rows["adj_open"] / rows["previous"] > 0.97))
        frame.loc[rows.index, "entry"] = mask
    frame["score"] = frame["impact20_rank"]
    frame["weight"] = 0.2
    frame["market_exit"] = ~frame["market_ok"]
    return frame


def save_run(prefix, result):
    result[1].to_csv(OUT / f"{prefix}_equity.csv", index=False)
    result[2].to_csv(OUT / f"{prefix}_trades.csv", index=False)
    result[3].to_csv(OUT / f"{prefix}_fills.csv", index=False)
    (OUT / f"{prefix}_metrics.json").write_text(json.dumps(result[0], indent=2), encoding="utf-8")


def segments(equity):
    output = {}
    previous = 1_000_000.0
    for year, rows in equity.groupby(equity["date"].str[:4]):
        output[year] = metrics(rows, previous)
        previous = float(rows.iloc[-1]["equity"])
    holdout = equity.loc[equity["date"] >= "20250101"]
    before = equity.loc[equity["date"] < "20250101"]
    if not holdout.empty and not before.empty:
        output["2025_plus_continuous"] = metrics(holdout, float(before.iloc[-1]["equity"]))
    return output


def screen(frame, leadership=False):
    choices = leader_candidates() if leadership else candidates()
    prefix = "leader_" if leadership else ""
    rows = []
    for config in choices:
        result = replay(signals(frame, config), config, "20220104", "20241231")
        row = dict(result[0], name=config.name)
        rows.append(row)
        _log(json.dumps(row))
    eligible = [row for row in rows if row["max_drawdown"] >= -0.35]
    if not eligible:
        eligible = rows
    selected = max(eligible, key=lambda row: row["annual_return"] / max(abs(row["max_drawdown"]), 0.05))
    config = next(item for item in choices if item.name == selected["name"])
    pd.DataFrame(rows).to_csv(OUT / (prefix + "training.csv"), index=False)
    (OUT / (prefix + "selected.json")).write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    for label, settings, prepared, baseline in [
        ("daily_candidate", config, signals(frame, config), False),
        ("daily_baseline", TrendConfig(name="v4_reconstructed"), baseline_signals(frame), True)]:
        result = replay(prepared, settings, "20220104", "20260918", baseline=baseline)
        label = prefix + label
        save_run(label, result)
        _log(label + " " + json.dumps(result[0]))
        (OUT / f"{label}_segments.json").write_text(json.dumps(segments(result[1]), indent=2), encoding="utf-8")


def validate(frame, leadership=False, refresh=False):
    prefix = "leader_" if leadership else ""
    config = TrendConfig(**json.loads((OUT / (prefix + "selected.json")).read_text(encoding="utf-8")))
    minute = BridgeMinutes(OUT / "minutes", "20220104", "20260918", refresh=refresh)
    summary = {}
    for label, settings, prepared, baseline, costs in [
        ("minute_candidate", config, signals(frame, config), False, 1.0),
        ("minute_candidate_cost2", config, signals(frame, config), False, 2.0),
        ("minute_baseline", TrendConfig(name="v4_reconstructed"), baseline_signals(frame), True, 1.0)]:
        label = prefix + label
        try:
            result = replay(prepared, settings, "20220104", "20260918",
                            execution=minute, baseline=baseline, cost_scale=costs)
            save_run(label, result)
            summary[label] = dict(result[0], segments=segments(result[1]))
            pnl = result[2]["pnl"].sort_values(ascending=False) if len(result[2]) else pd.Series(dtype=float)
            summary[label]["realized_pnl_without_top_winners"] = {
                str(count): float(pnl.iloc[count:].sum()) for count in (1, 5, 10)}
            _log(label + " " + json.dumps(result[0]))
        except (ValueError, TimeoutError, ConnectionError) as exc:
            summary[label] = {"blocked": True, "reason": str(exc)}
            _log(label + " blocked: " + str(exc))
    summary["claim_36_percent_validated"] = False
    summary["limitations"] = ["current constituent survivorship bias", "historical ST not available",
                               "corporate actions use synthetic total-return units",
                               "time split already used in earlier repository research",
                               "no order-book queue or market-impact simulation"]
    (OUT / (prefix + "validation.json")).write_text(json.dumps(summary, indent=2), encoding="utf-8")


def stress(frame):
    output = {}
    for prefix in ("", "leader_"):
        config = TrendConfig(**json.loads((OUT / (prefix + "selected.json")).read_text(encoding="utf-8")))
        result = replay(signals(frame, config), config, "20220104", "20260918", cost_scale=2.0)
        label = prefix + "daily_cost2"
        save_run(label, result)
        trades = pd.read_csv(OUT / (prefix + "daily_candidate_trades.csv"))
        pnl = trades["pnl"].sort_values(ascending=False)
        output[config.name] = {
            "double_cost": result[0],
            "realized_pnl": float(pnl.sum()),
            "realized_pnl_without_top": {str(n): float(pnl.iloc[n:].sum()) for n in (1, 5, 10)},
            "tail_test_note": "static realized PnL attribution only; not a rerun of compound account"}
        _log(label + " " + json.dumps(output[config.name]))
    (OUT / "daily_stress.json").write_text(json.dumps(output, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "screen", "validate", "leader-screen", "leader-validate", "stress"])
    parser.add_argument("--refresh-minutes", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        prepare()
        return 0
    frame = load_features()
    _log(f"loaded {len(frame)} daily rows from frozen RedisQMT cache")
    if args.command == "stress":
        stress(frame)
    elif args.command.endswith("screen"):
        screen(frame, args.command.startswith("leader-"))
    else:
        validate(frame, args.command.startswith("leader-"), args.refresh_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
