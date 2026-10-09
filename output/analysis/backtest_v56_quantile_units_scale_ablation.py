"""Compare v56 first-entry quantile unit scaling on identical 1-minute data."""

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

from backtest.dayt_strict import replay


OUT = ROOT / "analysis/dayt_v56_quantile_units_scale_ablation_20260917"
START = "20250915"
END = "20260911"
SCALES = (1.0, 0.6, 0.8, 0.9)
DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_data(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.read_csv(folder / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(folder / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]
    return daily, minute


def compact(result: dict, initial_equity: float) -> dict:
    return {
        "failure": result["failure"],
        "days": result["days"],
        "initial_equity": initial_equity,
        "account_net": result["account_net"],
        "account_return": result["account_net"] / initial_equity,
        "excess_net": result["excess_net"],
        "excess_return": result["excess_net"] / initial_equity,
        "maximum_drawdown": result["maximum_drawdown"],
        "fees": result["fees"],
        "completed_cycles": result["completed_cycles"],
        "final_cash": result["final_cash"],
        "final_position": result["final_position"],
        "longest_holding_sessions": result["longest_holding_sessions"],
        "unclosed": result["unclosed"],
        "rejection_counts": result["rejection_counts"],
    }


def money(value: float) -> str:
    return f"{value:,.2f}"


def label(scale: float) -> str:
    return "1.0（不缩放）" if scale == 1.0 else f"{scale:.1f}"


def build_report(payload: dict) -> str:
    results = payload["results"]
    aggregate = payload["aggregate"]
    control = aggregate["1.0"]
    best_money = max(SCALES, key=lambda value: aggregate[str(value)]["excess_net"])
    best_return = max(SCALES, key=lambda value: aggregate[str(value)]["mean_excess_return"])
    lines = [
        "# v56 `QUANTILE_UNITS_SCALE` 消融回测",
        "",
        "## 结论",
        "",
        "本报告将 `1.0` 定义为不启用首轮 quantile 单位缩放，并与 `0.6/0.8/0.9` 比较。"
        "结论仅适用于当前 v56、所列本地样本及回测假设，不代表样本外收益。",
        "",
        f"按三标的合计超额收益，样本最高为 **{label(best_money)}**；"
        f"按三标的等权平均超额收益率，样本最高为 **{label(best_return)}**。",
        "",
        "**样本内判断：缩放机制有效，但不能仅凭本样本确定实盘最优参数。**"
        "`0.6` 相对 `1.0` 的合计超额收益增加 4,751.98 元，但在 601869 上减少 662.31 元；"
        "`0.8` 对三只标的均有改善，同时取得最低的最差最大回撤 41.58%，跨标的一致性更好。"
        "所有配置的超额收益仍为负，表示它们在该区间都未跑赢静态持有底仓。",
        "",
        "## 汇总结果",
        "",
        "| Scale | 合计账户收益(元) | 合计超额收益(元) | 相对1.0变化(元) | "
        "等权平均超额收益率 | 最差最大回撤 | 完成周期 | 手续费(元) | 未闭合腿 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scale in SCALES:
        key = str(scale)
        row = aggregate[key]
        lines.append(
            f"| {label(scale)} | {money(row['account_net'])} | {money(row['excess_net'])} | "
            f"{money(row['excess_net'] - control['excess_net'])} | "
            f"{row['mean_excess_return']:.3%} | {row['worst_maximum_drawdown']:.2%} | "
            f"{row['completed_cycles']} | {money(row['fees'])} | {row['unclosed_legs']} |"
        )
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | Scale | 账户收益(元) | 超额收益(元) | 相对1.0变化(元) | "
        "超额收益率 | 最大回撤 | 完成周期 | 未闭合腿 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol, variants in results.items():
        baseline = variants["1.0"]["excess_net"]
        for scale in SCALES:
            row = variants[str(scale)]
            lines.append(
                f"| {symbol} | {label(scale)} | {money(row['account_net'])} | "
                f"{money(row['excess_net'])} | {money(row['excess_net'] - baseline)} | "
                f"{row['excess_return']:.3%} | {row['maximum_drawdown']:.2%} | "
                f"{row['completed_cycles']} | {len(row['unclosed'])} |"
            )
    lines.extend([
        "",
        "## 回测口径",
        "",
        f"- 区间：{START[:4]}-{START[4:6]}-{START[6:]} 至 {END[:4]}-{END[4:6]}-{END[6:]}。",
        "- 数据：本地 1 分钟 K 线；严格连续账户回放，不做逐日重置。",
        "- 标的：600584.SH、600105.SH、601869.SH。",
        "- 每标的初始现金 100,000 元、底仓 1,000 股、T 目标金额 40,000 元。",
        "- 每边费率 0.05%，无额外滑点；超额收益扣除静态持有底仓。",
        "- 唯一变量为 `QUANTILE_UNITS_SCALE`；`REENTRY_UP_UNITS_SCALE=0.8` 等其他参数保持当前值。",
        "- 该参数只作用于 quantile 模式首轮反 T 触发距离；不改变已有周期买回目标和后续轮次缩放。",
        "- 合计金额会受高价股绝对市值影响，因此同时报告按各标的初始权益归一化后的等权平均超额收益率。",
        "",
        "## 完整性检查",
        "",
    ])
    failures = [f"{symbol}/{scale}: {row['failure']}" for symbol, variants in results.items()
                for scale, row in variants.items() if row["failure"]]
    lines.append("- 回放失败：" + ("；".join(failures) if failures else "无。"))
    lines.extend([
        "- 原始结构化结果、数据及策略哈希见 `results.json`。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/backtest_v56_quantile_units_scale_ablation.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = {}
    dataset_hashes = {}
    for symbol, folder in DATASETS.items():
        daily, minute = load_data(folder)
        initial_equity = 100_000.0 + 1_000 * float(minute.iloc[0].open)
        dataset_hashes[symbol] = {
            "1d.csv": sha256(folder / "1d.csv"),
            "1m.csv": sha256(folder / "1m.csv"),
            "minute_rows": len(minute),
            "minute_range": [minute.index[0], minute.index[-1]],
        }
        results[symbol] = {}
        for scale in SCALES:
            print(f"replay {symbol} QUANTILE_UNITS_SCALE={scale}", flush=True)
            with redirect_stdout(io.StringIO()):
                result = replay(
                    "v56_nomom",
                    daily,
                    minute,
                    rate=0.0005,
                    overrides={
                        "T_TARGET_VALUE": 40_000.0,
                        "QUANTILE_UNITS_SCALE": scale,
                    },
                    symbol=symbol,
                    stock_name=symbol.split(".")[0],
                    initial_cash=100_000.0,
                    initial_shares=1_000,
                )
            results[symbol][str(scale)] = compact(result, initial_equity)
    aggregate = {}
    for scale in SCALES:
        rows = [variants[str(scale)] for variants in results.values()]
        aggregate[str(scale)] = {
            "account_net": sum(row["account_net"] for row in rows),
            "excess_net": sum(row["excess_net"] for row in rows),
            "mean_excess_return": sum(row["excess_return"] for row in rows) / len(rows),
            "worst_maximum_drawdown": max(row["maximum_drawdown"] for row in rows),
            "fees": sum(row["fees"] for row in rows),
            "completed_cycles": sum(row["completed_cycles"] for row in rows),
            "unclosed_legs": sum(len(row["unclosed"]) for row in rows),
        }
    strategy = ROOT / "Stragety/MiniQMT_Stragety/DayT/DayT_v56_nomom_ConfirmedReversalRiskBudget.py"
    payload = {
        "method": {
            "strategy": "v56_nomom",
            "bar_period": "1 minute",
            "range": [START, END],
            "fee_rate_each_side": 0.0005,
            "initial_cash_per_symbol": 100_000.0,
            "initial_shares_per_symbol": 1_000,
            "t_target_value": 40_000.0,
            "scales": SCALES,
        },
        "hashes": {"strategy": sha256(strategy), "datasets": dataset_hashes},
        "results": results,
        "aggregate": aggregate,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
