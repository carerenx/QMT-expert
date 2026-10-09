"""Paired-horizon backtest for A144 v4 candidate replacement.

The source sell events come from the frozen v3 5-minute sensitivity replay.
For every event that v3 closed through forced/emergency original buyback, v4
selects the highest-ranked eligible A144 reserve as of the prior completed day.
Candidate entry uses the next 5-minute open after 14:50 for a same-day match,
or the first 5-minute open when an unmatched leg retries on a later day.  A
source holding stops producing reverse-T events after its first replacement,
matching the live replacement-group lock.  Both the candidate and the
original baseline leg are valued at the source position's scheduled exit open.
This isolates the first replacement decision; it is not a full continuous-
account simulation of subsequent candidate exits or DayT cycles.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.research_a144_redisqmt import BUY_COST
from analysis.research_a144_redisqmt import Config
from analysis.research_a144_redisqmt import add_features
from analysis.research_a144_redisqmt import entry_candidates
from analysis.research_a144_redisqmt import load_panel
from analysis.research_a144_redisqmt import market_allows_entry
from analysis.research_a144_redisqmt import sell_cost
from Stragety.RedisQMT.A144 import config_v4
from Stragety.RedisQMT.A144.replacement_t_overlay import replacement_buy_shares


OUTPUT_DIR = ROOT / "analysis" / "a144_v4_candidate_backtest_20260924"
V3_RESULTS = (
    ROOT / "analysis" / "a144_v3_reverse_t_backtest_20260923" /
    "sensitivity_5m" / "session_results.csv")
BASE_TRADES = (
    ROOT / "analysis" / "a144_v3_reverse_t_backtest_20260923" /
    "sensitivity_5m" / "base_trades.csv")
CANDIDATE_DATA = OUTPUT_DIR / "candidate_5m"


def current_config() -> Config:
    return Config(
        name="current_d12_r04",
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True)


def active_codes(trades, date):
    mask = (
        (trades["entry_date"] <= date) &
        (trades["exit_date"] > date))
    return set(trades.loc[mask, "code"])


def cooling_codes(trades, date, dates, date_to_index):
    current_index = date_to_index[date]
    exited = trades.loc[trades["exit_date"] < date]
    blocked = set()
    for trade in exited.itertuples(index=False):
        exit_date = str(trade.exit_date)
        exit_index = date_to_index.get(exit_date)
        if exit_index is None:
            continue
        cooldown = 60 if str(trade.reason) == "stop" else 30
        if current_index - exit_index < cooldown:
            blocked.add(str(trade.code))
    return blocked


def ranked_codes_on(frame, date, config):
    rows = frame.loc[frame["date"] == date].copy()
    valid = rows.dropna(subset=["alpha_raw"])
    valid = valid.loc[valid["listed_days"] >= 130]
    count = max(1, int(len(valid) * config.factor_top_pct)) if len(valid) else 0
    return set(valid.nlargest(count, "alpha_raw")["code"])


def choose_candidate(
        frame,
        event_date,
        trades,
        dates,
        date_to_index,
        selected_until,
        config):
    event_index = date_to_index[event_date]
    signal_index = event_index - 1
    if signal_index < 0:
        return None
    signal_date = dates[signal_index]
    refresh_index = signal_index - signal_index % config.refresh_days
    refresh_date = dates[refresh_index]
    ranked = ranked_codes_on(frame, refresh_date, config)
    rows = frame.loc[frame["date"] == signal_date].reset_index(drop=True)
    if rows.empty or not market_allows_entry(rows.iloc[0], config):
        return None
    candidates = entry_candidates(rows, config, ranked)
    excluded = active_codes(trades, event_date)
    excluded.update(cooling_codes(
        trades,
        event_date,
        dates,
        date_to_index))
    excluded.update(
        code for code, until in selected_until.items()
        if until >= event_date)
    candidates = candidates.loc[~candidates["code"].isin(excluded)]
    if candidates.empty:
        return None
    first = candidates.iloc[0]
    return {
        "candidate_code": str(first["code"]),
        "candidate_factor": float(first["alpha_raw"]),
        "candidate_signal_date": signal_date,
        "candidate_refresh_date": refresh_date,
    }


def build_events():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(
        V3_RESULTS,
        dtype={"date": str, "entry_date": str, "exit_date": str})
    source = source.loc[
        (source["status"] == "completed") &
        (source["buy_reason"].isin([
            "force-buyback",
            "emergency-buyback"]))].copy()
    source = source.sort_values(["date", "code"]).reset_index(drop=True)
    source = source.drop_duplicates(
        subset=["code", "entry_date", "exit_date"],
        keep="first").reset_index(drop=True)
    trades = pd.read_csv(
        BASE_TRADES,
        dtype={"entry_date": str, "exit_date": str})
    print("loading frozen daily panel", flush=True)
    frame = add_features(load_panel())
    frame = frame.loc[
        (frame["date"] >= "20220104") &
        (frame["date"] <= "20260918")].copy()
    dates = sorted(frame["date"].unique())
    date_to_index = {date: index for index, date in enumerate(dates)}
    config = current_config()
    selected_until = {}
    pending = []
    output = []
    source_by_date = {}
    for event in source.to_dict(orient="records"):
        source_by_date.setdefault(str(event["date"]), []).append(event)
    first_date = min(source_by_date)
    last_date = max(str(value) for value in source["exit_date"])
    simulation_dates = [
        date for date in dates
        if first_date <= date <= last_date]
    for current_date in simulation_dates:
        attempts = list(pending)
        pending = []
        attempts.extend(source_by_date.get(current_date, []))
        attempts.sort(key=lambda item: (str(item["date"]), str(item["code"])))
        for event in attempts:
            row = dict(event)
            if current_date > str(event["exit_date"]):
                row["candidate_code"] = ""
                row["candidate_status"] = "no-eligible-candidate"
                output.append(row)
                continue
            choice = choose_candidate(
                frame,
                current_date,
                trades,
                dates,
                date_to_index,
                selected_until,
                config)
            if choice is None:
                pending.append(event)
                continue
            row.update(choice)
            row["candidate_status"] = "selected"
            row["replacement_date"] = current_date
            row["replacement_session"] = (
                "same-day-close" if current_date == str(event["date"])
                else "overnight-open")
            selected_until[choice["candidate_code"]] = str(event["exit_date"])
            output.append(row)
    for event in pending:
        row = dict(event)
        row["candidate_code"] = ""
        row["candidate_status"] = "no-eligible-candidate"
        output.append(row)
    events = pd.DataFrame(output)
    events = events.sort_values(["date", "code"]).reset_index(drop=True)
    events.to_csv(
        OUTPUT_DIR / "replacement_events.csv",
        index=False,
        encoding="utf-8-sig")
    requests = events.loc[
        events["candidate_status"] == "selected",
        ["candidate_code", "replacement_date", "exit_date"]].copy()
    if len(requests):
        grouped = requests.groupby("candidate_code").agg(
            start=("replacement_date", "min"),
            end=("replacement_date", "max"),
            horizon_end=("exit_date", "max"),
            events=("replacement_date", "size")).reset_index()
    else:
        grouped = pd.DataFrame(columns=[
            "candidate_code", "start", "end", "horizon_end", "events"])
    grouped.to_csv(
        OUTPUT_DIR / "candidate_data_requests.csv",
        index=False,
        encoding="utf-8-sig")
    print(events[[
        "date",
        "code",
        "buy_reason",
        "exit_date",
        "candidate_code",
        "candidate_status",
        "replacement_date",
        "replacement_session",
    ]].to_string(index=False), flush=True)
    print("candidate requests:", flush=True)
    print(grouped.to_string(index=False), flush=True)
    return events


def candidate_bar_path(code):
    return CANDIDATE_DATA / code / "1m.csv"


def five_minute_entry(code, date, earliest_time):
    path = candidate_bar_path(code)
    if not path.exists():
        return None
    frame = pd.read_csv(path, dtype={"time": str})
    frame = frame.set_index("time").sort_index()
    after = frame.loc[
        (frame.index.str[:8] == date) &
        (frame.index.str[8:14] >= earliest_time)]
    if after.empty:
        return None
    return float(after.iloc[0]["open"])


def panel_row(panel, code, date):
    key = (code, date)
    if key not in panel.index:
        return None
    row = panel.loc[key]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    return row


def evaluate():
    events_path = OUTPUT_DIR / "replacement_events.csv"
    if not events_path.exists():
        raise RuntimeError("run prepare before evaluate")
    events = pd.read_csv(
        events_path,
        dtype={"date": str, "exit_date": str})
    daily = load_panel().set_index(["code", "date"]).sort_index()
    rows = []
    for event in events.to_dict(orient="records"):
        row = dict(event)
        if event.get("candidate_status") != "selected":
            row["evaluation_status"] = "no-eligible-candidate"
            rows.append(row)
            continue
        candidate = str(event.get("candidate_code", "") or "")
        replacement_date = str(event["replacement_date"])
        earliest_time = (
            "145500" if event["replacement_session"] == "same-day-close"
            else "093500")
        entry = five_minute_entry(
            candidate,
            replacement_date,
            earliest_time) if candidate else None
        source_exit = panel_row(
            daily,
            str(event["code"]),
            str(event["exit_date"]))
        candidate_exit = panel_row(
            daily,
            candidate,
            str(event["exit_date"])) if candidate else None
        if entry is None:
            row["evaluation_status"] = "missing-candidate-5m"
            rows.append(row)
            continue
        if source_exit is None or candidate_exit is None:
            row["evaluation_status"] = "missing-exit-daily"
            rows.append(row)
            continue
        source_factor = float(source_exit["adj_close"]) / float(source_exit["close"])
        candidate_factor = (
            float(candidate_exit["adj_close"]) / float(candidate_exit["close"]))
        source_entry_row = panel_row(daily, str(event["code"]), str(event["date"]))
        candidate_entry_row = panel_row(daily, candidate, replacement_date)
        source_entry_factor = (
            float(source_entry_row["adj_close"]) / float(source_entry_row["close"]))
        candidate_entry_factor = (
            float(candidate_entry_row["adj_close"]) / float(candidate_entry_row["close"]))
        if (abs(source_factor / source_entry_factor - 1.0) > 0.0005 or
                abs(candidate_factor / candidate_entry_factor - 1.0) > 0.0005):
            row["evaluation_status"] = "corporate-action-in-horizon"
            rows.append(row)
            continue
        shares = int(event["sell_shares"])
        sell_price = float(event["sell_price"])
        gross_budget = sell_price * shares
        candidate_shares = replacement_buy_shares(
            gross_budget,
            gross_budget,
            entry,
            config_v4.DAYT_REPLACEMENT_CASH_USAGE,
            config_v4.TRADE_LOT_SIZE)
        if candidate_shares < config_v4.TRADE_LOT_SIZE:
            row["evaluation_status"] = "candidate-sub-lot"
            rows.append(row)
            continue
        source_sale_net = gross_budget * (1.0 - sell_cost(str(event["date"])))
        candidate_buy_cost = candidate_shares * entry * (1.0 + BUY_COST)
        cash_residual = source_sale_net - candidate_buy_cost
        candidate_exit_price = float(candidate_exit["open"])
        candidate_exit_net = (
            candidate_shares * candidate_exit_price *
            (1.0 - sell_cost(str(event["exit_date"]))))
        source_exit_price = float(source_exit["open"])
        source_baseline_net = (
            shares * source_exit_price *
            (1.0 - sell_cost(str(event["exit_date"]))))
        replacement_value = cash_residual + candidate_exit_net
        delta = replacement_value - source_baseline_net
        row.update({
            "evaluation_status": "evaluated",
            "candidate_entry_price": entry,
            "candidate_shares": candidate_shares,
            "candidate_exit_price": candidate_exit_price,
            "source_exit_price": source_exit_price,
            "cash_residual": cash_residual,
            "replacement_value": replacement_value,
            "source_baseline_value": source_baseline_net,
            "v4_delta_vs_v2": delta,
            "v3_delta_vs_v2": float(event["net_pnl"]),
            "v4_minus_v3": delta - float(event["net_pnl"]),
        })
        rows.append(row)
    result = pd.DataFrame(rows)
    result.to_csv(
        OUTPUT_DIR / "evaluated_events.csv",
        index=False,
        encoding="utf-8-sig")
    valid = result.loc[result["evaluation_status"] == "evaluated"].copy()
    matched = result.loc[result["candidate_status"] == "selected"].copy()
    top_delta = 0.0
    delta_without_top = 0.0
    improvement_without_top = 0.0
    if len(valid):
        top_index = valid["v4_delta_vs_v2"].idxmax()
        top_delta = float(valid.loc[top_index, "v4_delta_vs_v2"])
        without_top = valid.drop(index=top_index)
        delta_without_top = float(without_top["v4_delta_vs_v2"].sum())
        improvement_without_top = float(without_top["v4_minus_v3"].sum())
    summary = {
        "method": (
            "first replacement per source holding; retry until source exit; "
            "paired source-exit horizon; 5-minute candidate entry"),
        "events_total": int(len(result)),
        "status_counts": result["evaluation_status"].value_counts().to_dict(),
        "candidate_matched": int(len(matched)),
        "candidate_match_rate": float(len(matched) / len(result)) if len(result) else 0.0,
        "same_day_matches": int(
            (matched["replacement_session"] == "same-day-close").sum()),
        "overnight_matches": int(
            (matched["replacement_session"] == "overnight-open").sum()),
        "evaluated": int(len(valid)),
        "first_event_v3_delta_vs_v2_all": float(result["net_pnl"].sum()),
        "v3_delta_vs_v2": float(valid["v3_delta_vs_v2"].sum()) if len(valid) else 0.0,
        "v4_delta_vs_v2": float(valid["v4_delta_vs_v2"].sum()) if len(valid) else 0.0,
        "v4_minus_v3": float(valid["v4_minus_v3"].sum()) if len(valid) else 0.0,
        "v4_win_rate_vs_v2": float((valid["v4_delta_vs_v2"] > 0).mean()) if len(valid) else 0.0,
        "v4_better_than_v3_rate": float((valid["v4_minus_v3"] > 0).mean()) if len(valid) else 0.0,
        "v4_average_delta": float(valid["v4_delta_vs_v2"].mean()) if len(valid) else 0.0,
        "v4_median_delta": float(valid["v4_delta_vs_v2"].median()) if len(valid) else 0.0,
        "largest_single_v4_delta": top_delta,
        "v4_delta_without_largest_winner": delta_without_top,
        "v4_minus_v3_without_largest_winner": improvement_without_top,
    }
    (OUTPUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "evaluate"))
    args = parser.parse_args()
    if args.action == "prepare":
        build_events()
    else:
        evaluate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
