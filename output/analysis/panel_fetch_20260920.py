"""通过 redisQMT 桥接拉取全市场日线，落盘为按批的 parquet。

## 为什么是桥接（试过三条路的结论）

| 数据源 | 结果 |
|---|---|
| **redisQMT 桥接** | ✅ **唯一可用**。100 只/114 秒，4200 根日线 |
| baostock | ❌ 账号被锁（`10001011 当前用户没有权限或者已被锁定`）。单会话 18 秒/只，并发上限约 7，起 20 分片触发封禁 |
| akshare 东财 | ❌ `push2his.eastmoney.com` 被系统级 HTTP 代理拦截（关沙箱也一样） |

baostock 字段最全（交易所口径的 `isST` / `turn` / `pctChg` / `tradestatus`），
但拿不到。桥接的字段缺口在 `panel_build_20260920.py` 的 docstring 里逐条说明。

## 拉什么

每只股票拉**两套**价格：

* `dividend_type="none"` —— 不复权原始价。**成交与涨跌停判定用这套**
  （涨跌停价是按原始价算的），落盘为 `open/high/low/close/volume/amount`
* `dividend_type="front"` —— 前复权。**收益与指标用这套**，
  落盘为 `f_open/f_high/f_low/f_close`

实测前复权是对的：`000007.SZ` 2017-07-17（10 送 5 + 一字跌停）
前复权序列给出 **−10.04%**，官方涨跌幅 **−10.02%**（差 0.02pp，
来自复权因子的舍入），而不复权价给的是 −40.03% 的除权跳空。

**注意**：前复权序列的起点可能晚于不复权序列（实测 `600000.SH`
晚 47 个交易日）—— 序列长度都 4200，但起点不同。合成面板时按日期
内连接，早于前复权起点的那几天没有总收益，回退用原始价算。

## 停牌日的表示

桥接用的是 `fill_data=False`，所以**停牌日缺行**，不像 baostock 会
给一行「价格冻结、volume=0」。面板里因此只包含**实际有成交的行**，
停牌由「该交易日没有这一行」体现。

这对引擎的含义：持仓股停牌时 `close` 缺值，盯市必须**前向填充**，
否则持仓会被算成 0 市值。引擎里已按此处理（`close_mx.ffill()`），
但**开盘价不填充** —— 停牌日下不了单。

## 续跑

按批落盘 `raw/batch_{shard}_{序号}.parquet`（每批 100 只）。
批文件存在即跳过，所以中断重跑只补缺的。

## 并行

桥接是服务端，可多进程并发。每个进程自己 `configure()`。

    bash analysis/panel_fetch_all.sh 3

用法：
    python analysis/panel_fetch_20260920.py --limit 200     # 冒烟
    python analysis/panel_fetch_20260920.py --shard 0/3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "analysis/panel_20260920"
RAW = OUT / "raw"

START = "20100101"
END = "20260918"
BATCH = 100
COUNT = 4200          # 覆盖 2009-02 ~ 2026-09，足够 2010 起的样本 + 250 日预热

PRICE_FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def connect():
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure
    _, data = configure(account_id="8890145315", timeout_seconds=120)
    return data


def to_frame(block: pd.DataFrame, code: str, prefix: str = "") -> pd.DataFrame:
    """桥接返回的 `date × field` 表 → 规整长表。"""
    frame = block.reset_index()
    frame.columns = ["time"] + [f"{prefix}{c}" for c in frame.columns[1:]]
    frame["time"] = frame["time"].astype(str).str.replace("-", "")
    frame["code"] = code
    return frame


def fetch_batch(data, codes: list[str], verbose: bool = False) -> pd.DataFrame:
    """拉一批股票的两套价格并合成。"""
    data.download_history_data2(codes, "1d", start_time=START, end_time=END,
                                download_timeout_seconds=300)

    # 两套价格各自先按行拼接，再按 (code, time) 外连接。
    # 不能把同一套价格里不同 code 的帧拿去 merge —— 那会产生
    # open_x/open_y 之类的重复列（MergeError）。
    panels = {}
    for dividend_type, prefix in (("none", ""), ("front", "f_")):
        raw = data.get_market_data_ex(
            PRICE_FIELDS, codes, period="1d", count=COUNT,
            dividend_type=dividend_type, fill_data=False, timeout_seconds=180)
        if verbose:
            print(f"    {dividend_type}: {len(raw or {})} 只", flush=True)
        parts = [to_frame(block, code, prefix)
                 for code, block in (raw or {}).items()
                 if block is not None and len(block) > 0]
        panels[prefix] = (pd.concat(parts, ignore_index=True)
                          if parts else pd.DataFrame())

    plain = panels[""]
    front = panels["f_"]
    if plain.empty:
        return pd.DataFrame()
    if front.empty:
        return plain
    # 两套价格的起点可能不同（实测 600000.SH 的前复权晚 47 个交易日），
    # 所以用外连接，早于前复权起点的那几天 f_* 为 NaN
    return plain.merge(front, on=["code", "time"], how="outer")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--shard", default=None, help="形如 0/3")
    parser.add_argument("--batch-size", type=int, default=BATCH)
    args = parser.parse_args()

    universe_path = OUT / "universe.json"
    if not universe_path.exists():
        raise SystemExit(f"先跑 panel_universe_20260920.py 生成 {universe_path}")
    codes = json.loads(universe_path.read_text(encoding="utf-8"))["codes"]

    if args.shard:
        index, total = (int(x) for x in args.shard.split("/"))
        codes = [c for i, c in enumerate(codes) if i % total == index]
        print(f"分片 {index}/{total}: {len(codes)} 只", flush=True)
    if args.limit:
        codes = codes[:args.limit]

    RAW.mkdir(parents=True, exist_ok=True)
    batches = [codes[i:i + args.batch_size]
               for i in range(0, len(codes), args.batch_size)]

    data = connect()
    done = skipped = empty = 0
    rows_total = 0
    t0 = time.time()
    for i, batch in enumerate(batches):
        tag = args.shard.replace("/", "_") if args.shard else "0_1"
        path = RAW / f"batch_{tag}_{i:04d}.parquet"
        if path.exists():
            skipped += 1
            continue

        try:
            frame = fetch_batch(data, batch)
        except Exception as error:
            print(f"  [fail] batch {i}: {type(error).__name__}: {error}",
                  flush=True)
            time.sleep(2.0)
            continue

        if frame.empty:
            empty += 1
            continue
        frame.to_parquet(path, index=False)
        done += 1
        rows_total += len(frame)

        if done % 5 == 0 or i == len(batches) - 1:
            elapsed = time.time() - t0
            processed = done + empty
            rate = processed / elapsed if elapsed > 0 else 0
            remain = len(batches) - i - 1
            eta = remain / rate / 60 if rate > 0 else 0
            print(f"  [{i + 1}/{len(batches)}] 批次 {done} 行 {rows_total:,} "
                  f"空 {empty} 跳过 {skipped} | {rate:.2f} 批/秒 | "
                  f"剩余约 {eta:.1f} 分钟", flush=True)

    print()
    print(f"完成: 新拉 {done} 批、跳过 {skipped}、空 {empty}")
    print(f"新增 {rows_total:,} 行，用时 {(time.time() - t0) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()
