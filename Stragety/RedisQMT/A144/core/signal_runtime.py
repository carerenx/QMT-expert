# -*- coding: utf-8 -*-
"""Read-only end-of-day scanner using RedisQMT; no broker order capability."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .bridge_minutes import ROOT
from .bridge_minutes import _log
from .trend import features
from .trend import signals


def run(config, argv=None):
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
    prepared = signals(features(pd.concat(parts, ignore_index=True), benchmark), config)
    selected = prepared.loc[(prepared["date"] == latest) & prepared["entry"]].sort_values(
        ["score", "code"], ascending=[False, True])
    output = {"strategy": config.name, "config": asdict(config), "asof": latest,
              "source": "RedisQMT bridge", "mode": "research-signals-only", "live_enabled": False,
              "requires": ["position-aware portfolio planning", "historical validation", "current ST check"],
              "candidates": selected[["code", "score", "weight", "close", "atr_pct"]].head(20).to_dict("records")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"wrote {len(output['candidates'])} research candidates to {args.output}")
    return 0
