"""Cached 1-minute backtest for the A144 v3 reverse-T overlay.

The daily A144 v2 portfolio is reconstructed on the frozen research panel.
Reverse-T signals are evaluated only where genuine cached 1-minute bars exist.
Signals use each minute close and fill at the next minute open, which avoids
same-bar look-ahead.  No live or Redis order interface is called.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis.research_a144_redisqmt import BUY_COST
from output.analysis.research_a144_redisqmt import Config
from output.analysis.research_a144_redisqmt import add_features
from output.analysis.research_a144_redisqmt import load_panel
from output.analysis.research_a144_redisqmt import run_backtest
from output.analysis.research_a144_redisqmt import sell_cost
from Stragety.RedisQMT.A144 import config_v3
from Stragety.RedisQMT.A144.reverse_t_overlay import atr_percent
from Stragety.RedisQMT.A144.reverse_t_overlay import new_symbol_state
from Stragety.RedisQMT.A144.reverse_t_overlay import next_action
from Stragety.RedisQMT.A144.reverse_t_overlay import record_buy_fill
from Stragety.RedisQMT.A144.reverse_t_overlay import record_sell_fill
from Stragety.RedisQMT.A144.reverse_t_overlay import reverse_t_sell_shares


OUTPUT_ROOT = ROOT / "analysis" / "a144_v3_reverse_t_backtest_20260923"
MINUTE_ROOT = ROOT / "analysis" / "profit_priority_cross_stock_20"
START = "20250915"
END = "20260915"


def current_v2_config() -> Config:
    return Config(
        name="current_d12_r04",
        risk_model="inverse",
        weak_exit_min_days=12,
        weak_exit_max_return=-0.04,
        weak_exit_reserve_slot=True,
    )


def cached_paths(bar_mode: str) -> dict[str, dict[str, Path]]:
    if bar_mode == "strict_1m":
        folder = OUTPUT_ROOT / "sina_000031"
        return {
            "000031.SZ": {
                "minute": folder / "1m.csv",
                "raw": folder / "1d.csv",
            }
        }
    if bar_mode == "sensitivity_5m":
        paths = {}
        for folder in sorted(OUTPUT_ROOT.glob("sina5_*")):
            code = folder.name.split("_")[-1]
            suffix = ".SH" if code.startswith("6") else ".SZ"
            paths[code + suffix] = {
                "minute": folder / "1m.csv",
                "raw": folder / "1d.csv",
            }
        return paths
    paths = {}
    for symbol_dir in sorted(MINUTE_ROOT.iterdir()):
        if not symbol_dir.is_dir():
            continue
        nested = symbol_dir / symbol_dir.name
        minute = nested / "minute.csv"
        raw = nested / "raw.csv"
        if minute.exists() and raw.exists():
            paths[symbol_dir.name] = {"minute": minute, "raw": raw}
    return paths


def eligible_trade_sessions(
        trades: pd.DataFrame,
        trading_dates: list[str]) -> pd.DataFrame:
    rows = []
    date_set = set(trading_dates)
    for trade_index, trade in trades.reset_index(drop=True).iterrows():
        entry = str(trade["entry_date"])
        exit_date = str(trade["exit_date"])
        sessions = [
            date for date in trading_dates
            if entry < date < exit_date and date in date_set
        ]
        for date in sessions:
            rows.append({
                "trade_index": trade_index,
                "code": str(trade["code"]),
                "date": date,
                "entry_date": entry,
                "exit_date": exit_date,
                "entry_price": float(trade["entry_price"]),
                "capital_in": float(trade["capital_in"]),
            })
    return pd.DataFrame(rows)


def previous_raw_history(raw: pd.DataFrame, date: str) -> pd.DataFrame:
    return raw.loc[raw.index < date, ["high", "low", "close"]].tail(
        config_v3.DAYT_HISTORY_BARS)


def estimated_owned_shares(session: pd.Series, daily_row: pd.Series) -> int:
    adjusted_units = float(session["capital_in"]) / (
        float(session["entry_price"]) * (1.0 + BUY_COST))
    raw_close = float(daily_row["close"])
    adjusted_close = float(daily_row["adj_close"])
    if raw_close <= 0 or adjusted_close <= 0:
        return 0
    raw_shares = adjusted_units * adjusted_close / raw_close
    return int(raw_shares / config_v3.TRADE_LOT_SIZE) * config_v3.TRADE_LOT_SIZE


def replay_session(
        session: pd.Series,
        minute: pd.DataFrame,
        raw: pd.DataFrame,
        daily_row: pd.Series) -> dict:
    date = str(session["date"])
    bars = minute.loc[minute.index.str[:8] == date].copy()
    bar_times = bars.index.str[8:14]
    bars = bars.loc[(bar_times >= "093000") & (bar_times <= "150000")]
    if len(bars) < 2:
        return {"status": "missing-minute", "code": session["code"], "date": date}
    first_hms = str(bars.index[0])[8:14]
    last_hms = str(bars.index[-1])[8:14]
    if first_hms > "093500" or last_hms < "150000":
        return {"status": "partial-session", "code": session["code"], "date": date}
    history = previous_raw_history(raw, date)
    atr_pct = atr_percent(history, config_v3.DAYT_ATR_PERIOD)
    if atr_pct is None:
        return {"status": "missing-atr", "code": session["code"], "date": date}
    owned = estimated_owned_shares(session, daily_row)
    shares = reverse_t_sell_shares(
        owned,
        owned,
        config_v3.DAYT_SELL_FRACTION,
        config_v3.TRADE_LOT_SIZE)
    if shares <= 0:
        return {"status": "sub-lot", "code": session["code"], "date": date}

    open_price = float(bars.iloc[0]["open"])
    state = new_symbol_state(date, open_price, atr_pct, config_v3)
    pending = None
    fills = []
    for timestamp, bar in bars.iterrows():
        if pending is not None:
            fill_price = float(bar["open"])
            if pending["side"] == "SELL":
                record_sell_fill(
                    state,
                    shares,
                    fill_price,
                    "backtest-sell",
                    config_v3)
            else:
                record_buy_fill(
                    state,
                    int(state["sold_shares"]),
                    fill_price,
                    "backtest-buy")
            fills.append({
                "side": pending["side"],
                "reason": pending["reason"],
                "time": str(timestamp),
                "price": fill_price,
            })
            pending = None
        action = next_action(
            state,
            float(bar["close"]),
            "{}:{}:{}".format(
                str(timestamp)[8:10],
                str(timestamp)[10:12],
                str(timestamp)[12:14]),
            config_v3)
        if action is not None:
            pending = action

    if int(state.get("sold_shares", 0)) > 0:
        close_price = float(bars.iloc[-1]["close"])
        reason = "close-fallback"
        record_buy_fill(
            state,
            int(state["sold_shares"]),
            close_price,
            "backtest-close")
        fills.append({
            "side": "BUY",
            "reason": reason,
            "time": str(bars.index[-1]),
            "price": close_price,
        })
    if not fills:
        return {
            "status": "no-trigger",
            "code": session["code"],
            "date": date,
            "owned_shares": owned,
            "sell_shares": shares,
            "atr_pct": atr_pct,
            "open": open_price,
            "sell_trigger": state["sell_trigger"],
        }
    if len(fills) != 2 or fills[0]["side"] != "SELL" or fills[1]["side"] != "BUY":
        raise RuntimeError("unexpected fill sequence: {}".format(fills))
    sell_fill = fills[0]
    buy_fill = fills[1]
    gross = (sell_fill["price"] - buy_fill["price"]) * shares
    sell_fees = sell_fill["price"] * shares * sell_cost(date)
    buy_fees = buy_fill["price"] * shares * BUY_COST
    net = gross - sell_fees - buy_fees
    return {
        "status": "completed",
        "code": session["code"],
        "date": date,
        "entry_date": session["entry_date"],
        "exit_date": session["exit_date"],
        "owned_shares": owned,
        "sell_shares": shares,
        "atr_pct": atr_pct,
        "open": open_price,
        "sell_trigger": state["sell_trigger"],
        "sell_time": sell_fill["time"],
        "sell_price": sell_fill["price"],
        "buy_time": buy_fill["time"],
        "buy_price": buy_fill["price"],
        "buy_reason": buy_fill["reason"],
        "gross_pnl": gross,
        "fees": sell_fees + buy_fees,
        "net_pnl": net,
        "return_on_t_notional": net / (sell_fill["price"] * shares),
    }


def performance(equity: pd.Series) -> dict:
    returns = equity.pct_change().dropna()
    drawdown = equity / equity.cummax() - 1.0
    years = max(
        (pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])).days / 365.25,
        1.0 / 252.0)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    annual_return = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = returns.mean() / returns.std() * math.sqrt(252.0)
    return {
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(sharpe),
        "final_equity": float(equity.iloc[-1]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bar-mode",
        choices=("strict_1m", "sensitivity_5m"),
        default="strict_1m")
    args = parser.parse_args()
    output_dir = OUTPUT_ROOT / args.bar_mode
    output_dir.mkdir(parents=True, exist_ok=True)
    print("loading and enriching frozen daily panel", flush=True)
    frame = add_features(load_panel())
    config = current_v2_config()
    print("reconstructing current v2 portfolio", flush=True)
    base_metrics, base_equity_frame, trades = run_backtest(
        frame, config, "20220104", "20260918")
    trades.to_csv(output_dir / "base_trades.csv", index=False, encoding="utf-8-sig")

    date_mask = (frame["date"] >= START) & (frame["date"] <= END)
    trading_dates = sorted(frame.loc[date_mask, "date"].unique())
    sessions = eligible_trade_sessions(trades, trading_dates)
    cache = cached_paths(args.bar_mode)
    covered = sessions.loc[sessions["code"].isin(cache)].copy()
    panel_lookup = frame.loc[date_mask].set_index(["code", "date"])
    results = []
    loaded = {}
    for index, session in covered.reset_index(drop=True).iterrows():
        code = str(session["code"])
        if code not in loaded:
            minute = pd.read_csv(cache[code]["minute"], dtype={"time": str})
            minute = minute.set_index("time").sort_index()
            raw = pd.read_csv(cache[code]["raw"], dtype={"time": str})
            raw = raw.set_index("time").sort_index()
            loaded[code] = (minute, raw)
        minute = loaded[code][0]
        raw = loaded[code][1]
        key = (code, str(session["date"]))
        if key not in panel_lookup.index:
            result = {"status": "missing-daily", "code": code, "date": session["date"]}
        else:
            result = replay_session(
                session,
                minute,
                raw,
                panel_lookup.loc[key])
        results.append(result)
        if (index + 1) % 100 == 0:
            print("replayed {}/{} sessions".format(index + 1, len(covered)), flush=True)

    result_frame = pd.DataFrame(results)
    result_frame.to_csv(
        output_dir / "session_results.csv", index=False, encoding="utf-8-sig")
    if "status" not in result_frame:
        result_frame = pd.DataFrame(columns=["status"])
    completed = result_frame.loc[result_frame["status"] == "completed"].copy()
    daily_t = completed.groupby("date")["net_pnl"].sum() if len(completed) else pd.Series(dtype=float)
    base_equity = base_equity_frame.iloc[:, 0]
    window_base = base_equity.loc[(base_equity.index >= START) & (base_equity.index <= END)]
    cumulative_t = daily_t.reindex(window_base.index, fill_value=0.0).cumsum()
    v3_equity = window_base + cumulative_t
    comparison = {
        "v2_base_window": performance(window_base),
        "v3_additive_window": performance(v3_equity),
    }
    equity_output = pd.DataFrame({
        "v2_base": window_base,
        "dayt_daily_net": daily_t.reindex(window_base.index, fill_value=0.0),
        "dayt_cumulative_net": cumulative_t,
        "v3_additive": v3_equity,
    })
    equity_output.to_csv(output_dir / "equity_comparison.csv", encoding="utf-8-sig")

    status_counts = result_frame["status"].value_counts().to_dict()
    total_sessions = int(len(sessions))
    covered_sessions = int(len(covered))
    summary = {
        "method": {
            "signal": "minute close",
            "fill": "next minute open",
            "data": (
                "Sina cached 1-minute bars"
                if args.bar_mode == "strict_1m"
                else "Sina cached 5-minute bars; sensitivity only"),
            "bar_mode": args.bar_mode,
            "period": [START, END],
            "costs": {
                "buy": BUY_COST,
                "sell_before_20230828": 0.00176,
                "sell_after_20230828": 0.00126,
            },
            "integration": "T PnL added to frozen v2 equity; no sizing feedback",
        },
        "base_full_metrics": base_metrics,
        "coverage": {
            "cached_symbols": len(cache),
            "portfolio_symbols": int(sessions["code"].nunique()),
            "intersected_symbols": int(covered["code"].nunique()),
            "eligible_position_sessions": total_sessions,
            "covered_position_sessions": covered_sessions,
            "session_coverage": covered_sessions / total_sessions if total_sessions else 0.0,
            "complete_data_sessions": int(status_counts.get("completed", 0) + status_counts.get("no-trigger", 0) + status_counts.get("sub-lot", 0)),
            "status_counts": status_counts,
        },
        "cycles": {
            "completed": int(len(completed)),
            "win_rate": float((completed["net_pnl"] > 0).mean()) if len(completed) else 0.0,
            "gross_pnl": float(completed["gross_pnl"].sum()) if len(completed) else 0.0,
            "fees": float(completed["fees"].sum()) if len(completed) else 0.0,
            "net_pnl": float(completed["net_pnl"].sum()) if len(completed) else 0.0,
            "average_net_pnl": float(completed["net_pnl"].mean()) if len(completed) else 0.0,
            "median_net_pnl": float(completed["net_pnl"].median()) if len(completed) else 0.0,
            "average_return_on_t_notional": float(completed["return_on_t_notional"].mean()) if len(completed) else 0.0,
            "buy_reason_counts": completed["buy_reason"].value_counts().to_dict() if len(completed) else {},
        },
        "comparison": comparison,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
