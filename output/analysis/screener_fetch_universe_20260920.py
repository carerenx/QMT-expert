"""从 RedisQMT 桥接拉取沪深A股全市场的日线，供选股器研究使用。

为什么要拉全市场：v4 在 13 个标的上**无法验证任何东西**（有效样本数只有 3.6）。
要判断「什么样的股票适合 v4」，需要的是
「几千只股票 × 十几年」的横截面面板，而不是十几个样本。

数据源：integrations/bigqmt 的 xtquant_compat（QMT 桥接，只读行情）。
输出：`analysis/screener_universe_20260920/daily.csv`
     列 = time, code, open, high, low, close, volume, amount

用法：
    python analysis/screener_fetch_universe_20260920.py            # 全市场
    python analysis/screener_fetch_universe_20260920.py --limit 200  # 冒烟
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/screener_universe_20260920"
FIELDS = ["open", "high", "low", "close", "volume", "amount"]
COUNT = 2500                 # 约 10 年日线
BATCH = 100
START, END = "20100101", "20260918"
SECTOR = "沪深A股"


def connect():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    return configure(account_id='8890145315', timeout_seconds=60)


def is_common_stock(code: str) -> bool:
    """沪深主板 / 创业板 / 科创板的正股，排除指数、ETF、退市。"""
    num, _, market = code.partition(".")
    if market not in ("SH", "SZ") or len(num) != 6 or not num.isdigit():
        return False
    if num.startswith(("60", "00", "30", "68")):        # 主板 / 中小 / 创业 / 科创
        return True
    return False


def main() -> None:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    conn, data = connect()
    codes = [c for c in data.get_stock_list_in_sector(SECTOR) if is_common_stock(c)]
    codes.sort()
    if limit:
        codes = codes[:limit]
    print(f"universe: {len(codes)} 只", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    frames, failed, t0 = [], 0, time.time()
    for i in range(0, len(codes), BATCH):
        batch = codes[i:i + BATCH]
        try:
            # 桥接的下载是异步的；download_history_data2 会等到 finished
            data.download_history_data2(
                batch, "1d", start_time=START, end_time=END,
                download_timeout_seconds=180)
            got = data.get_market_data_ex(
                FIELDS, batch, period="1d", count=COUNT,
                dividend_type="none", fill_data=False, timeout_seconds=60)
        except Exception as error:
            print(f"  batch {i} failed: {type(error).__name__}: {error}", flush=True)
            failed += len(batch)
            continue
        for code in batch:
            frame = got.get(code)
            if frame is None or len(frame) < 250:
                failed += 1
                continue
            frame = frame.copy()
            frame["code"] = code
            frame.index.name = "time"
            frames.append(frame.reset_index())
        done = min(i + BATCH, len(codes))
        if done % 500 == 0 or done == len(codes):
            print(f"  {done}/{len(codes)}  ok={len(frames)} fail={failed} "
                  f"{time.time() - t0:.0f}s", flush=True)

    panel = pd.concat(frames, ignore_index=True)
    panel = panel[["time", "code", "open", "high", "low", "close",
                   "volume", "amount"]]
    panel.to_csv(OUT / "daily.csv", index=False)
    (OUT / "meta.json").write_text(json.dumps({
        "sector": SECTOR, "count": COUNT, "range": [START, END],
        "symbols": len(frames), "failed": failed,
        "rows": int(len(panel)),
        "first": str(panel["time"].min()), "last": str(panel["time"].max()),
        "codes": sorted({f["code"].iloc[0] for f in frames}),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {OUT / 'daily.csv'}  rows={len(panel)} symbols={len(frames)}",
          flush=True)


if __name__ == "__main__":
    main()
