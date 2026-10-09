# -*- coding: utf-8 -*-
"""RedisQMT-only raw minute cache with reproducible request provenance."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[4]


def _log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


class BridgeMinutes:
    label = "RedisQMT-1m-0932"

    def __init__(self, directory: Path, start: str, end: str, refresh=False):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.start = start
        self.end = end
        self.daily = {}
        self.xtdata = None
        self.refresh = refresh

    def _load(self, code):
        path = self.directory / f"{code}_{self.start}_{self.end}.parquet"
        manifest_path = path.with_suffix(".json")
        if self.refresh or not path.exists() or not manifest_path.exists():
            if self.xtdata is None:
                sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
                from bigqmt_signal_trader.xtquant_compat import configure
                connection = configure(timeout_seconds=20)
                self.xtdata = connection[1]
            if self.refresh:
                self.xtdata.client.call("download_history_data2", {
                    "stock_list": [code], "period": "1m", "start_time": self.start + "000000",
                    "end_time": self.end + "150000"}, timeout_seconds=45)
            data = self.xtdata.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=[code], period="1m", start_time=self.start + "000000",
                end_time=self.end + "150000", count=-1, dividend_type="none",
                fill_data=False, timeout_seconds=45, bypass_formula=True)
            frame = (data or {}).get(code)
            if frame is None or frame.empty:
                raise ValueError(f"RedisQMT returned no 1m data: {code}")
            frame = frame.copy()
            frame.index = frame.index.astype(str)
            frame = frame.sort_index()
            frame.to_parquet(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps({
                "source": "RedisQMT RPC", "period": "1m", "dividend_type": "none",
                "bypass_formula": True, "fill_data": False, "code": code,
                "start": self.start, "end": self.end, "rows": len(frame),
                "sha256": digest}, indent=2), encoding="utf-8")
            _log(f"cached RedisQMT 1m {code}: {len(frame)} bars")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source"] != "RedisQMT RPC" or manifest["period"] != "1m":
            raise ValueError("invalid minute provenance")
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("minute cache checksum mismatch")
        frame = pd.read_parquet(path)
        frame.index = frame.index.astype(str)
        if frame.index.duplicated().any():
            raise ValueError("duplicate minute timestamps")
        daily = {}
        for date, bars in frame.groupby(frame.index.str[:8]):
            times = bars.index.str[8:14]
            active = bars.loc[((times >= "093100") & (times <= "113000")) |
                              ((times >= "130100") & (times <= "150000"))]
            execution = bars.loc[bars.index == date + "093200"]
            close = bars.loc[bars.index == date + "150000"]
            valid_prices = (active[["open", "high", "low", "close"]] > 0).all(axis=1)
            complete = (len(active) == 240 and valid_prices.all() and
                        len(execution) == 1 and len(close) == 1)
            daily[date] = {"complete": complete,
                           "positive_bars": int(valid_prices.sum()),
                           "open": float(execution.iloc[0]["open"]) if len(execution) else 0,
                           "close": float(close.iloc[0]["close"]) if len(close) else 0}
        self.daily[code] = daily

    def quote(self, code: str, date: str, row: dict) -> dict:
        if code not in self.daily:
            self._load(code)
        result = self.daily[code].get(date)
        if result is None:
            raise ValueError(f"missing RedisQMT minute session: {code} {date}")
        if not result["complete"]:
            raise ValueError(f"incomplete RedisQMT minute session: {code} {date}; "
                             f"positive bars={result['positive_bars']}/240")
        # Same source, same raw close: reject minute/daily scale mismatches.
        if result["complete"] and abs(result["close"] / row["close"] - 1) > 0.002:
            raise ValueError(f"minute/daily close mismatch: {code} {date}")
        return result
