# -*- coding: utf-8 -*-
"""Read-only end-of-day scanner using RedisQMT; no broker order capability."""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .bridge_minutes import ROOT
from .bridge_minutes import _log
from .trend import features
from .trend import signals


def _pad_column(value, width, right=False):
    text = str(value)
    display_width = sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)
    padding = " " * max(0, width - display_width)
    return padding + text if right else text + padding


def run(config, argv=None, signal_builder=signals):
    parser = argparse.ArgumentParser(description="RedisQMT completed-day research signals; no live orders")
    parser.add_argument("--asof", required=True, help="completed Shanghai date YYYYMMDD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    asof = datetime.strptime(args.asof, "%Y%m%d").date()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if asof > now.date() or (asof == now.date() and now.hour < 16):
        parser.error("asof must be a completed day (same-day requests after 16:00 Shanghai)")
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    connection = configure(timeout_seconds=30)
    xtdata = connection[1]
    codes = sorted(set(xtdata.get_stock_list_in_sector("中证500") or []))
    _log(f"[Screen] requested_date={args.asof}, CSI500 universe={len(codes)}, loading daily bars")
    if len(codes) < 400:
        raise ValueError("incomplete universe")
    requested = codes + ["000905.SH"]
    common = dict(stock_list=requested, period="1d", end_time=args.asof,
                  count=260, fill_data=False, chunk_size=40,
                  timeout_seconds=30, bypass_formula=True)
    adjusted = xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close", "volume", "amount"],
        dividend_type="front", **common)
    raw = xtdata.get_market_data_ex(
        field_list=["open", "high", "low", "close"], dividend_type="none", **common)
    benchmark = raw.get("000905.SH")
    if benchmark is None or benchmark.empty:
        raise ValueError("missing benchmark")
    benchmark.index = benchmark.index.astype(str).str[:8]
    benchmark = benchmark.loc[(benchmark.index <= args.asof) & (benchmark["close"] > 0)]
    if len(benchmark) < 130:
        raise ValueError("insufficient benchmark history")
    latest = str(benchmark.index.max())
    parts = []
    for code in codes:
        if code not in adjusted or code not in raw:
            continue
        prices = raw[code].copy()
        adj = adjusted[code].copy()
        prices.index = prices.index.astype(str).str[:8]
        adj.index = adj.index.astype(str).str[:8]
        adj = adj.rename(columns={name: "adj_" + name for name in ("open", "high", "low", "close")})
        frame = prices.join(adj[["adj_open", "adj_high", "adj_low", "adj_close", "amount", "volume"]])
        frame = frame.loc[(frame.index <= latest) & (frame["close"] > 0) & (frame["adj_close"] > 0)].dropna()
        if len(frame) < 130 or latest not in frame.index:
            continue
        frame["date"] = frame.index
        frame["code"] = code
        parts.append(frame.reset_index(drop=True))
    if len(parts) < len(codes) * 0.98:
        raise ValueError(f"incomplete current data: {len(parts)}/{len(codes)}; no signals published")
    prepared = features(pd.concat(parts, ignore_index=True), benchmark)
    current = prepared["date"] == latest
    breadth = prepared.loc[current, "breadth"].iloc[0]
    _log(f"[Screen] data_date={latest}, valid_stocks={len(parts)}/{len(codes)}, breadth={breadth:.1%}")
    if config.entry == "leader":
        _log(f"[Screen] Leader: {config.leader_horizon}-day momentum in top 5%, close>MA20")

    def log_filter(name, mask, remaining):
        _log(f"[Screen] {name}: passed={int(mask.loc[current].sum())}, remaining={int(remaining.loc[current].sum())}")
        if name.startswith("Entry mode="):
            rank_field = f"mom{config.leader_horizon}_rank" if config.entry == "leader" else "mom60_rank"
            preliminary = prepared.loc[current & remaining].sort_values(
                [rank_field, "code"], ascending=[False, True])
            _log(f"[PreMarket] trend_and_entry_passed={len(preliminary)}; market, breadth and subsequent filters pending")
            for _, row in preliminary.iterrows():
                code = row["code"]
                try:
                    detail = xtdata.get_instrument_detail(code) or {}
                    stock_name = detail.get("InstrumentName") or "Unknown"
                except Exception as exc:
                    stock_name = "Unknown"
                    _log(f"[PreMarket] {code} name lookup failed: {exc}")
                _log(
                    f"[PreMarket] {_pad_column(code, 9)} {_pad_column(stock_name, 10)}"
                    f" | Close={row['close']:8.2f} CNY"
                    f" | AdjReturn={row['ret']:+8.2%}"
                    f" | Momentum{config.leader_horizon if config.entry == 'leader' else 60}Rank={row[rank_field]:6.1%}"
                    f" | AvgTurnover20={row['amount20'] / 1e8:7.2f} x100M CNY"
                    f" | ATRRatio={row['atr_pct']:6.2%}")

    prepared = signal_builder(prepared, config, log_filter=log_filter)
    selected = prepared.loc[(prepared["date"] == latest) & prepared["entry"]].sort_values(
        ["score", "code"], ascending=[False, True])
    _log(f"[Screen] final_passed={len(selected)}, output_limit=20, output_count={min(len(selected), 20)}, sorted by score")
    output = {"strategy": config.name, "config": asdict(config), "asof": latest,
              "source": "RedisQMT bridge", "mode": "research-signals-only", "live_enabled": False,
              "requires": ["position-aware portfolio planning", "historical validation", "current ST check"],
              "candidates": selected[["code", "score", "weight", "close", "atr_pct"]].head(20).to_dict("records")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"Wrote {len(output['candidates'])} research candidates to {args.output}")
    return 0
