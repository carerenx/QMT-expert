"""Reproduce the old CoreT independent-daily-reset method with RedisQMT data.

This is intentionally *not* a continuous-account simulation.  It recreates
the old report's daily cash/position reset, 40,000 yuan target leg and 40%
base fraction so its ranking can be compared on the same methodology.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import output.analysis.backtest_coreT_cycle_report_20260919 as legacy

OUT = ROOT / "analysis/dayt_coreT_old_method_redis_20260101_20260911"
SYMBOLS = ("600584.SH", "600105.SH", "601869.SH")
START = "20260101"
END = "20260911"
WARMUP = "20250701"


def digest(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch_symbol(data, symbol: str, folder: Path) -> None:
    fields = ["open", "high", "low", "close", "volume", "amount"]
    daily = data.get_market_data_ex(
        fields, [symbol], period="1d", start_time=WARMUP, end_time="20260912",
        count=-1, dividend_type="front", fill_data=False, timeout_seconds=120,
    )[symbol].sort_index()
    chunks = []
    for month in pd.period_range("2026-01", "2026-09", freq="M"):
        begin = month.start_time.strftime("%Y%m%d")
        finish = min((month.end_time + pd.Timedelta(days=1)).strftime("%Y%m%d"), "20260912")
        if begin >= finish:
            continue
        chunks.append(data.get_market_data_ex(
            fields, [symbol], period="1m", start_time=begin, end_time=finish,
            count=-1, dividend_type="none", fill_data=False, timeout_seconds=120,
        )[symbol])
    minute = pd.concat(chunks).loc[lambda x: ~x.index.duplicated(keep="last")]
    minute = minute.sort_index()
    minute = minute.loc[(minute.index.str[:8] >= START) & (minute.index.str[:8] <= END)]
    if minute.empty or minute.index.max()[:8] != END:
        raise RuntimeError(f"{symbol}: Redis minute coverage ends at {minute.index.max() if not minute.empty else 'none'}")
    folder.mkdir(parents=True, exist_ok=True)
    daily.to_csv(folder / "1d.csv", index_label="time")
    minute.to_csv(folder / "1m.csv", index_label="time")


def money(value: float) -> str:
    return f"{value:,.2f}"


def report(payload: dict, aggregate: dict) -> str:
    totals = legacy.totals(aggregate)
    lines = [
        "# CoreT：旧版每日独立重置口径的 RedisQMT 复跑",
        "",
        "## 口径",
        "",
        "- 本报告刻意复刻旧报告的方法，而非连续账户：每个交易日重新给策略 100,000 元现金和 1,000 股；日末未闭合腿仅按收盘价标记，次日不带入账户。",
        "- 标的：600584.SH、600105.SH、601869.SH；窗口：2026-01-01—2026-09-11。",
        "- 单边费用 0.05%，滑点 0；新腿为 40,000 元目标上限和 40%可配对底仓，按100股取整。",
        "- 行情仅由 RedisQMT 桥接取得：前复权日线用于历史信号、不复权一分钟线用于撮合。[Python API, p.70–71]",
        "",
        "## 汇总结果",
        "",
        "| 口径 | 超额净收益 | 手续费 | 综合T达成率 | 已闭合反T周期 | 已闭合正T周期 | 日末未闭合天数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in legacy.ORDER:
        row = totals[key]
        lines.append(
            f"| {legacy.VARIANTS[key]['name']} | {money(row['excess_net'])} | "
            f"{money(row['fees'])} | {row['t_rate']:.2%} | "
            f"{row['REV-T_cycles']} | {row['FWD-T_cycles']} | {row['unclosed_days']} |")
    winner = max(legacy.ORDER, key=lambda key: totals[key]["excess_net"])
    lines += ["", f"按这一旧口径，排名第一的是 **{legacy.VARIANTS[winner]['name']}**。",
              "这只说明独立单日评分的结果；它不能替代持仓、现金和残腿连续的真实账户结果。", "",
              "## 分标的超额净收益", "",
              "| 标的 | 口径 | 超额净收益 | 反T已实现毛收益 | 正T已实现毛收益 | 日末未闭合标记损益 | 手续费 |", "|---|---|---:|---:|---:|---:|---:|"]
    for symbol in SYMBOLS:
        for key in legacy.ORDER:
            row = aggregate[symbol][key]
            lines.append(
                f"| {symbol} | {legacy.VARIANTS[key]['name']} | {money(row['excess_net'])} | "
                f"{money(row['REV-T_realized'])} | {money(row['FWD-T_realized'])} | "
                f"{money(row['REV-T_unrealized'] + row['FWD-T_unrealized'])} | {money(row['fees'])} |")
    lines += ["", "## 可复现性", "",
              "数据快照和哈希位于 `data/`；逐笔闭合与未闭合腿见 `trades.md`。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure

    OUT.mkdir(parents=True, exist_ok=True)
    _, data = configure(account_id="8890145315", timeout_seconds=30)
    datasets = {}
    for symbol in SYMBOLS:
        folder = OUT / "data" / symbol
        if not ((folder / "1d.csv").exists() and (folder / "1m.csv").exists()):
            fetch_symbol(data, symbol, folder)
        datasets[symbol] = folder
        print(f"loaded {symbol}", flush=True)
    legacy.OUT = OUT
    legacy.START = START
    legacy.END = END
    legacy.DATASETS = datasets
    days, hashes = legacy.run_all(False)
    aggregate = legacy.aggregate_all(days)
    payload = {
        "method": "independent_daily_reset",
        "range": [START, END],
        "source": "RedisQMT bridge",
        "initial_cash_each_day": legacy.INITIAL_CASH,
        "initial_shares_each_day": legacy.INITIAL_SHARES,
        "target_value": legacy.COMMON["T_TARGET_VALUE"],
        "position_fraction": 0.40,
        "data_hashes": hashes,
        "summary": {symbol: {key: {name: value for name, value in row.items() if name != "all_cycles"}
                             for key, row in variants.items()} for symbol, variants in aggregate.items()},
        "days": days,
    }
    (OUT / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "README.md").write_text(report(payload, aggregate), encoding="utf-8")
    (OUT / "trades.md").write_text(legacy.build_trades(days, aggregate), encoding="utf-8")
    print(OUT / "README.md", flush=True)


if __name__ == "__main__":
    main()
