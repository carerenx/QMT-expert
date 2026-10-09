# -*- coding: utf-8 -*-
"""Pre-registered cross-sectional rotation study using RedisQMT data only."""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis.research_a144_v5_trend import load_features
from Stragety.RedisQMT.A144.core.bridge_minutes import BridgeMinutes
from Stragety.RedisQMT.A144.core.bridge_minutes import _log
from Stragety.RedisQMT.A144.core.replay import INITIAL_CASH
from Stragety.RedisQMT.A144.core.replay import buy_quantity
from Stragety.RedisQMT.A144.core.replay import executable
from Stragety.RedisQMT.A144.core.replay import fee
from Stragety.RedisQMT.A144.core.replay import metrics


OUT = ROOT / "analysis/a144_v7_rotation_20260926"


@dataclass(frozen=True)
class RotationConfig:
    name: str
    score: str
    rebalance_days: int
    positions: int
    require_breadth: bool = False


def configurations():
    return [
        RotationConfig("mom60_top5_r5", "mom60", 5, 5),
        RotationConfig("mom120_top5_r5", "mom120", 5, 5),
        RotationConfig("composite_top5_r5", "composite", 5, 5),
        RotationConfig("composite_top5_r10", "composite", 10, 5),
        RotationConfig("composite_top10_r10", "composite", 10, 10),
        RotationConfig("composite_top5_r5_breadth", "composite", 5, 5, True),
    ]


def prepare_features():
    frame = load_features().sort_values(["code", "date"]).copy()
    group = frame.groupby("code", sort=False)
    frame["vol20"] = group["ret"].transform(lambda x: x.rolling(20).std())
    daily = frame.groupby("date", sort=False)
    frame["low_vol_rank"] = daily["vol20"].rank(pct=True, ascending=False)
    frame["composite"] = (0.40 * frame["mom60_rank"] +
                          0.35 * frame["mom120_rank"] +
                          0.15 * frame["efficiency_rank"] +
                          0.10 * frame["low_vol_rank"])
    frame["composite_rank"] = daily["composite"].rank(pct=True)
    benchmark = pd.read_parquet(OUT.parent / "a144_v5_trend_20260926/benchmark_1d.parquet")
    benchmark.index = benchmark.index.astype(str).str[:8]
    benchmark = benchmark.sort_index()
    close = benchmark["close"]
    benchmark["ma120"] = close.rolling(120).mean()
    benchmark["market_rotation"] = close > benchmark["ma120"]
    frame["market_rotation"] = frame["date"].map(benchmark["market_rotation"]).fillna(False)
    return frame


def target_codes(rows, config):
    if rows.empty or not bool(rows.iloc[0]["market_rotation"]):
        return []
    if config.require_breadth and float(rows.iloc[0]["breadth"]) < 0.45:
        return []
    eligible = rows.loc[
        (rows["age"] >= 130) &
        (rows["amount20"] >= 3e7) &
        (rows["adj_close"] > rows["ma50"]) &
        (rows["mom60"] > 0) &
        rows["atr_pct"].between(0.01, 0.08) &
        (rows["ret"] < 0.095)
    ].copy()
    score = config.score + "_rank"
    return list(eligible.sort_values([score, "code"], ascending=[False, True])["code"].head(config.positions))


class DailyRotationExecution:
    label = "daily-rotation-prescreen"

    def quote(self, code, date, row):
        return {"open": float(row["open"]), "close": float(row["close"]), "complete": True}


def replay_rotation(frame, config, start, end, execution=None, cost_scale=1.0):
    execution = execution or DailyRotationExecution()
    data = frame.loc[(frame["date"] >= start) & (frame["date"] <= end)]
    cash = INITIAL_CASH
    positions = {}
    pending_targets = None
    equity_rows = []
    trades = []
    fills = []
    missing = []
    last_equity = INITIAL_CASH
    dates = []
    for day_index, daily in enumerate(data.groupby("date", sort=True)):
        date = str(daily[0])
        dates.append(date)
        rows = daily[1].set_index("code").to_dict("index")
        quotes = {}

        def quote(code):
            if code not in quotes:
                quotes[code] = execution.quote(code, date, rows[code])
                if not quotes[code]["complete"]:
                    raise ValueError(f"incomplete execution data: {code} {date}")
            return quotes[code]

        if pending_targets is not None:
            target_set = set(pending_targets)
            for code in sorted(set(positions) - target_set):
                if code not in rows:
                    continue
                row = rows[code]
                factor = float(row["adj_close"] / row["close"])
                price = float(quote(code)["open"])
                if not executable(price, float(row["previous"]), factor, code, "SELL"):
                    continue
                position = positions[code]
                notional = position["units"] * price * factor
                proceeds = notional - fee(notional, "SELL", date, cost_scale)
                cash += proceeds
                trades.append({"code": code, "entry_date": position["entry_date"],
                               "exit_date": date, "capital_in": position["cash_in"],
                               "pnl": proceeds - position["cash_in"],
                               "return": proceeds / position["cash_in"] - 1,
                               "reason": "rotation"})
                fills.append({"date": date, "code": code, "side": "SELL",
                              "price": price, "notional": notional})
                del positions[code]
            desired = max(1, len(pending_targets))
            target_budget = last_equity / desired
            for code in pending_targets:
                if code in positions or code not in rows:
                    continue
                row = rows[code]
                factor = float(row["adj_close"] / row["close"])
                price = float(quote(code)["open"])
                if not executable(price, float(row["previous"]), factor, code, "BUY"):
                    continue
                budget = min(cash, target_budget, float(row["amount20"]) * 0.001)
                quantity = buy_quantity(budget, price, code, cost_scale)
                if quantity <= 0:
                    continue
                notional = quantity * price
                cash_in = notional + fee(notional, "BUY", date, cost_scale)
                cash -= cash_in
                adjusted_price = price * factor
                positions[code] = {"units": quantity / factor,
                                   "cash_in": cash_in,
                                   "entry_date": date,
                                   "mark": adjusted_price}
                fills.append({"date": date, "code": code, "side": "BUY",
                              "price": price, "notional": notional})
            pending_targets = None
        market_value = 0.0
        for code, position in positions.items():
            if code not in rows:
                missing.append({"date": date, "code": code})
            else:
                row = rows[code]
                factor = float(row["adj_close"] / row["close"])
                position["mark"] = float(quote(code)["close"]) * factor
            market_value += position["units"] * position["mark"]
        last_equity = cash + market_value
        equity_rows.append({"date": date, "equity": last_equity, "cash": cash,
                            "exposure": market_value / last_equity,
                            "positions": len(positions)})
        if day_index % config.rebalance_days == 0:
            pending_targets = target_codes(daily[1], config)
    equity = pd.DataFrame(equity_rows)
    result = metrics(equity)
    result.update({"trades": len(trades), "open_positions": len(positions),
                   "missing_held_daily_rows": len(missing),
                   "execution": execution.label, "cost_scale": cost_scale})
    return result, equity, pd.DataFrame(trades), pd.DataFrame(fills)


def save(label, result):
    result[1].to_csv(OUT / f"{label}_equity.csv", index=False)
    result[2].to_csv(OUT / f"{label}_trades.csv", index=False)
    result[3].to_csv(OUT / f"{label}_fills.csv", index=False)
    (OUT / f"{label}_metrics.json").write_text(json.dumps(result[0], indent=2), encoding="utf-8")


def period_metrics(equity):
    output = {}
    previous = INITIAL_CASH
    for year, rows in equity.groupby(equity["date"].str[:4]):
        output[year] = metrics(rows, previous)
        previous = float(rows.iloc[-1]["equity"])
    before = equity.loc[equity["date"] < "20250101"]
    after = equity.loc[equity["date"] >= "20250101"]
    output["2025_plus_continuous"] = metrics(after, float(before.iloc[-1]["equity"]))
    return output


def screen(frame):
    rows = []
    for config in configurations():
        result = replay_rotation(frame, config, "20220104", "20241231")
        row = dict(result[0], name=config.name)
        rows.append(row)
        _log(json.dumps(row))
    eligible = [row for row in rows if row["max_drawdown"] >= -0.35]
    selected = max(eligible, key=lambda row: row["annual_return"] / max(abs(row["max_drawdown"]), 0.05))
    config = next(item for item in configurations() if item.name == selected["name"])
    pd.DataFrame(rows).to_csv(OUT / "training.csv", index=False)
    (OUT / "selected.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
    full = replay_rotation(frame, config, "20220104", "20260918")
    save("daily_candidate", full)
    (OUT / "daily_candidate_segments.json").write_text(
        json.dumps(period_metrics(full[1]), indent=2), encoding="utf-8")
    double = replay_rotation(frame, config, "20220104", "20260918", cost_scale=2.0)
    save("daily_cost2", double)
    pnl = full[2]["pnl"].sort_values(ascending=False)
    stress = {"double_cost": double[0], "realized_pnl": float(pnl.sum()),
              "realized_pnl_without_top": {str(n): float(pnl.iloc[n:].sum()) for n in (1, 5, 10)}}
    (OUT / "stress.json").write_text(json.dumps(stress, indent=2), encoding="utf-8")
    _log("selected " + json.dumps(asdict(config)))
    _log("full " + json.dumps(full[0]))


def validate(frame, refresh=False):
    config = RotationConfig(**json.loads((OUT / "selected.json").read_text(encoding="utf-8")))
    minute = BridgeMinutes(OUT / "minutes", "20220104", "20260918", refresh=refresh)
    summary = {}
    for label, costs in (("minute_candidate", 1.0), ("minute_cost2", 2.0)):
        try:
            result = replay_rotation(frame, config, "20220104", "20260918", minute, costs)
            save(label, result)
            summary[label] = dict(result[0], segments=period_metrics(result[1]))
        except (ValueError, TimeoutError, ConnectionError) as exc:
            summary[label] = {"blocked": True, "reason": str(exc)}
            _log(label + " blocked: " + str(exc))
    candidate = summary.get("minute_candidate", {})
    summary["claim_36_percent_validated"] = bool(
        not candidate.get("blocked") and candidate.get("annual_return", 0) >= 0.36 and
        candidate.get("max_drawdown", -1) >= -0.35)
    (OUT / "validation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["screen", "validate"])
    parser.add_argument("--refresh-minutes", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    frame = prepare_features()
    _log(f"loaded {len(frame)} RedisQMT daily rows")
    if args.command == "screen":
        screen(frame)
    else:
        validate(frame, args.refresh_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
