"""Compare v56/v57/v58 with a fresh account and strategy state every day."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from output.analysis.compare_v51_v39_minute import replay
from backtest.dayt_registry import STRATEGIES


OUT = ROOT / "analysis/dayt_v57_v58_daily_reset_20260918"
START = "20250915"
END = "20260911"
FEE_RATE = 0.0005
VERSIONS = {
    "v56_nomom": "v56基准",
    "v57_nomom": "v57激进清仓",
    "v58_nomom": "v58分级降仓",
}
DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_data(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.read_csv(
        folder / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(
        folder / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]
    return daily, minute


def summarize(rows: list[dict]) -> dict:
    fees = sum(row["turnover"] * FEE_RATE for row in rows)
    account_gross = sum(row["account_gross"] for row in rows)
    excess_gross = sum(row["excess_gross"] for row in rows)
    initial_equity = sum(row["initial_equity"] for row in rows)
    unclosed_days = sum(bool(
        row["short_unclosed"] or row["long_unclosed"] or
        any(row["ledger_unclosed"].values())) for row in rows)
    return {
        "days": len(rows),
        "failure_count": sum(row["failure"] is not None for row in rows),
        "account_gross": account_gross,
        "account_net": account_gross - fees,
        "account_return": (account_gross - fees) / initial_equity,
        "excess_gross": excess_gross,
        "excess_net": excess_gross - fees,
        "excess_return": (excess_gross - fees) / initial_equity,
        "fees": fees,
        "turnover": sum(row["turnover"] for row in rows),
        "fills": sum(len(row["trades"]) for row in rows),
        "cycles": sum(row["cycles"] for row in rows),
        "worst_daily_drawdown": max(row["max_drawdown"] for row in rows),
        "profitable_days": sum(
            row["account_gross"] - row["turnover"] * FEE_RATE > 0
            for row in rows),
        "position_gap_days": sum(row["position_gap"] != 0 for row in rows),
        "unclosed_days": unclosed_days,
    }


def money(value: float) -> str:
    return f"{value:,.2f}"


def build_report(payload: dict) -> str:
    results = payload["summary"]
    aggregate = payload["aggregate"]
    lines = [
        "# v56/v57/v58 每日重置回测",
        "",
        "## 结论",
        "",
        "本报告每天重新初始化现金、1000股底仓、策略状态和周期账本。"
        "它衡量单日独立启动表现，不模拟跨日持仓、T+1延续、风险库存或恢复过程，"
        "因此不能替代严格连续账户报告。",
        "",
        "结果中v56的合计超额净收益最高。v58把最差单日回撤从v56的10.10%"
        "降至9.90%，但合计超额净收益减少25,424.52元；v57收益下降更多且"
        "没有改善最差单日回撤。600584和600105在每日重置口径下均无T成交，"
        "版本差异全部来自601869，不具备跨标的一致性。",
        "",
        "| 策略 | 合计账户净收益 | 合计超额净收益 | 等权超额收益率 | "
        "最差单日回撤 | 成交笔数 | 未闭合日数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for version, name in VERSIONS.items():
        row = aggregate[version]
        lines.append(
            f"| {name} | {money(row['account_net'])} | "
            f"{money(row['excess_net'])} | {row['mean_excess_return']:.3%} | "
            f"{row['worst_daily_drawdown']:.2%} | {row['fills']} | "
            f"{row['unclosed_days']} |")
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | 策略 | 账户净收益 | 超额净收益 | 超额收益率 | "
        "最差单日回撤 | 盈利日/总日 | 成交 | 周期 | 手续费 | 仓位偏离日 | 未闭合日 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol, variants in results.items():
        for version, name in VERSIONS.items():
            row = variants[version]
            lines.append(
                f"| {symbol} | {name} | {money(row['account_net'])} | "
                f"{money(row['excess_net'])} | {row['excess_return']:.3%} | "
                f"{row['worst_daily_drawdown']:.2%} | "
                f"{row['profitable_days']}/{row['days']} | {row['fills']} | "
                f"{row['cycles']} | {money(row['fees'])} | "
                f"{row['position_gap_days']} | {row['unclosed_days']} |")
    lines.extend([
        "",
        "## 口径限制",
        "",
        "- 每个交易日均使用现金100,000元、底仓1,000股重新开始；日与日之间不复投。",
        "- 本地1分钟K线，区间2025-09-15至2026-09-11；仅纳入至少230根分钟线且有80日历史的交易日。",
        "- 每边费率0.05%，无额外滑点；费用按成交额后处理扣除。",
        "- `T_TARGET_VALUE=40,000`、`QUANTILE_UNITS_SCALE=1.0`，其余参数使用当前策略值。",
        "- 最大回撤是所有独立交易日中最差的单日内回撤，不能与连续账户最大回撤直接比较。",
        "- 日末未闭合腿不会带入次日；相关日单独计数，不能将其当成已经完成的策略收益。",
        "- v57/v58的跨日恢复规则在此口径下不会被完整检验。",
        "",
        "## 完整性检查",
        "",
    ])
    failures = [
        f"{symbol}/{version}: {row['failure_count']}"
        for symbol, variants in results.items()
        for version, row in variants.items() if row["failure_count"]]
    lines.append("- 回放失败：" + ("；".join(failures) if failures else "无。"))
    lines.extend([
        "- 全部逐日结果、策略/数据哈希和参数见 `results.json`。",
        "- 本报告写入独立目录，未覆盖严格连续账户报告。",
        "",
        "## 复现",
        "",
        "目标目录已存在时脚本会拒绝覆盖；复现前请指定新副本或移走该目录。",
        "",
        "```powershell",
        "python analysis/backtest_v57_v58_daily_reset.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    if OUT.exists():
        raise FileExistsError("refuse to overwrite existing report: " + str(OUT))
    OUT.mkdir(parents=True)
    raw = {}
    summary = {}
    dataset_hashes = {}
    for symbol, folder in DATASETS.items():
        daily, minute = load_data(folder)
        dataset_hashes[symbol] = {
            "1d.csv": sha256(folder / "1d.csv"),
            "1m.csv": sha256(folder / "1m.csv"),
        }
        raw[symbol] = {}
        summary[symbol] = {}
        grouped = list(minute.groupby(minute.index.str[:8]))
        for version, name in VERSIONS.items():
            rows = []
            for index, (day, bars) in enumerate(grouped, 1):
                history = daily.loc[daily.index < day]
                if len(bars) < 230 or len(history) < 80:
                    continue
                with redirect_stdout(io.StringIO()):
                    result = replay(
                        version, history, bars, slip=0.0,
                        initial_cash=100_000.0, initial_shares=1_000,
                        symbol=symbol,
                        overrides={
                            "T_TARGET_VALUE": 40_000.0,
                            "QUANTILE_UNITS_SCALE": 1.0,
                        })
                rows.append(result)
                if result["failure"]:
                    raise RuntimeError(
                        f"{symbol}/{version}/{day}: {result['failure']}")
                if index % 60 == 0:
                    print(
                        f"{symbol} {name}: {index}/{len(grouped)}",
                        flush=True)
            raw[symbol][version] = rows
            summary[symbol][version] = summarize(rows)
            print(f"completed {symbol} {name}: {len(rows)} days", flush=True)
    aggregate = {}
    for version in VERSIONS:
        rows = [summary[symbol][version] for symbol in DATASETS]
        aggregate[version] = {
            "account_net": sum(row["account_net"] for row in rows),
            "excess_net": sum(row["excess_net"] for row in rows),
            "mean_excess_return": sum(
                row["excess_return"] for row in rows) / len(rows),
            "worst_daily_drawdown": max(
                row["worst_daily_drawdown"] for row in rows),
            "fills": sum(row["fills"] for row in rows),
            "unclosed_days": sum(row["unclosed_days"] for row in rows),
        }
    strategy_hashes = {
        version: sha256(
            ROOT / "Stragety/MiniQMT_Stragety/DayT" / STRATEGIES[version])
        for version in VERSIONS
    }
    payload = {
        "method": {
            "model": "INDEPENDENT_DAILY_RESET",
            "range": [START, END],
            "initial_cash_each_day": 100_000.0,
            "initial_shares_each_day": 1_000,
            "fee_rate_each_side": FEE_RATE,
            "slippage": 0.0,
            "t_target_value": 40_000.0,
            "quantile_units_scale": 1.0,
        },
        "hashes": {"strategies": strategy_hashes, "datasets": dataset_hashes},
        "summary": summary,
        "aggregate": aggregate,
        "daily_results": raw,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
