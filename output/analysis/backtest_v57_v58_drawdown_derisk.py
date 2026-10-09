"""Compare v56 with v57/v58 drawdown exposure controls."""

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
from backtest.dayt_registry import STRATEGIES


OUT = ROOT / "analysis/dayt_v57_v58_drawdown_derisk_20260917"
START = "20250915"
END = "20260911"
VERSIONS = {
    "v56_nomom": "v56 基准",
    "v57_nomom": "v57 激进清仓",
    "v58_nomom": "v58 分级降仓",
}
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


def summarize(result: dict, initial_equity: float, initial_shares: int) -> dict:
    equity = pd.DataFrame(result["equity"])
    equity["running_max"] = equity["equity"].cummax()
    equity["drawdown"] = 1.0 - equity["equity"] / equity["running_max"]
    trough_index = equity["drawdown"].idxmax()
    peak_index = equity.loc[:trough_index, "equity"].idxmax()
    session_close = equity.assign(day=equity["time"].str[:10]).groupby("day").tail(1)
    orders = result["orders"]
    drawdown = result.get("drawdown_record") or {}
    risk_unclosed = [row for row in result["unclosed"] if row["group"] == "RISK"]
    risk_unrealized = sum(row["unrealized"] for row in risk_unclosed)
    risk_period = (float(equity["risk_active"].mean())
                   if "risk_active" in equity else 0.0)
    return {
        "failure": result["failure"],
        "days": result["days"],
        "initial_equity": initial_equity,
        "account_net": result["account_net"],
        "account_return": result["account_net"] / initial_equity,
        "excess_net": result["excess_net"],
        "excess_return": result["excess_net"] / initial_equity,
        "maximum_drawdown": result["maximum_drawdown"],
        "drawdown_peak": equity.loc[peak_index, "time"],
        "drawdown_trough": equity.loc[trough_index, "time"],
        "fees": result["fees"],
        "completed_cycles": result["completed_cycles"],
        "final_cash": result["final_cash"],
        "final_position": result["final_position"],
        "derisk_events": int(drawdown.get("derisk_events", 0)),
        "restore_events": int(drawdown.get("restore_events", 0)),
        "risk_order_count": sum(
            order["label"].startswith("RISK") or "DRAWDOWN" in order["label"]
            for order in orders),
        "cash_days": int((session_close["position"] == 0).sum()),
        "risk_period_pct": risk_period,
        "risk_inventory": sum(row["quantity"] for row in risk_unclosed),
        "risk_unrealized": risk_unrealized,
        "missed_upside": max(0.0, -risk_unrealized),
        "terminal_derisked": bool(drawdown) and (
            float(drawdown.get("target_fraction", 1.0)) < 1.0 or
            bool(risk_unclosed)),
        "unclosed": result["unclosed"],
        "rejection_counts": result["rejection_counts"],
    }


def money(value: float) -> str:
    return f"{value:,.2f}"


def build_report(payload: dict) -> str:
    results = payload["results"]
    aggregate = payload["aggregate"]
    acceptance = payload["acceptance"]
    lines = [
        "# v57/v58 主动降仓回撤控制回测",
        "",
        "## 结论",
        "",
        "本报告按预先固定的参数比较当前 v56、v57 激进清仓和 v58 分级降仓。"
        "没有根据结果追加参数搜索。25%是研究验收线，不是实盘保证。",
        "",
        "| 策略 | 三标的是否全部≤25% | 最差最大回撤 | 等权账户收益率 | 等权超额收益率 | 期末风险未闭合标的数 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for version, name in VERSIONS.items():
        row = aggregate[version]
        lines.append(
            f"| {name} | {'通过' if acceptance[version] else '未通过'} | "
            f"{row['worst_maximum_drawdown']:.2%} | {row['mean_account_return']:.2%} | "
            f"{row['mean_excess_return']:.2%} | {row['terminal_derisked_symbols']} |"
        )
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | 策略 | 账户收益(元/率) | 超额收益(元/率) | 最大回撤 | 相对v56改善 | "
        "峰值→谷值 |",
        "|---|---|---:|---:|---:|---:|---|",
    ])
    for symbol, variants in results.items():
        baseline = variants["v56_nomom"]["maximum_drawdown"]
        for version, name in VERSIONS.items():
            row = variants[version]
            lines.append(
                f"| {symbol} | {name} | {money(row['account_net'])} / {row['account_return']:.2%} | "
                f"{money(row['excess_net'])} / {row['excess_return']:.2%} | {row['maximum_drawdown']:.2%} | "
                f"{baseline - row['maximum_drawdown']:+.2%} | "
                f"{row['drawdown_peak'][:10]}→{row['drawdown_trough'][:10]} |"
            )
    lines.extend([
        "",
        "## 执行与期末状态",
        "",
        "| 标的 | 策略 | T周期 | 降仓/恢复 | 风险订单 | 空仓日 | 风险期占比 | 手续费 | 错过上涨收益 | 期末持仓 | 风险库存 | 未闭合腿 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol, variants in results.items():
        for version, name in VERSIONS.items():
            row = variants[version]
            unclosed = sum(item["quantity"] for item in row["unclosed"])
            lines.append(
                f"| {symbol} | {name} | {row['completed_cycles']} | "
                f"{row['derisk_events']}/{row['restore_events']} | {row['risk_order_count']} | "
                f"{row['cash_days']} | {row['risk_period_pct']:.1%} | {money(row['fees'])} | "
                f"{money(row['missed_upside'])} | {row['final_position']} | "
                f"{row['risk_inventory']} | {unclosed} |"
            )
    lines.extend([
        "",
        "## 收益真实性与期末风险",
        "",
        "- 超额收益以静态持有原始1000股为基准；主动降仓后少承受下跌，也可能错过上涨。",
        "- 存在风险库存或期末控制器目标低于100%时，收益包含尚未完成恢复的风险状态，不视为完整闭环优势。",
        "- `risk_unrealized` 按风险卖出价与期末价标记，已计入账户权益对静态持股的机会损益。",
        "- v57未达到三标的均≤25%的首要目标；v58达到，但三个标的期末均处风险状态，只建议继续样本外和影子验证，不开放实盘。",
        "",
        "## 回测口径",
        "",
        f"- 区间：{START[:4]}-{START[4:6]}-{START[6:]} 至 {END[:4]}-{END[4:6]}-{END[6:]}；本地1分钟数据。",
        "- 严格连续账户；每标的现金100,000元、底仓1,000股、T目标40,000元。",
        "- 每边费率0.05%，无额外滑点；`QUANTILE_UNITS_SCALE=1.0`，其余参数与当前v56一致。",
        "- v57：10%回撤且低于MA20连续3分钟后目标0%。",
        "- v58：8%/12%/16%/20%回撤对应75%/50%/25%/0%目标持仓。",
        "- 两个新策略均以连续2日收盘站上上升MA20为恢复条件，每日恢复25%。",
        "",
        "## 完整性检查",
        "",
    ])
    failures = [f"{symbol}/{version}: {row['failure']}"
                for symbol, variants in results.items()
                for version, row in variants.items() if row["failure"]]
    lines.append("- 回放失败：" + ("；".join(failures) if failures else "无。"))
    lines.extend([
        "- 结构化结果、数据和策略哈希见 `results.json`。",
        "- v57/v58保持RESEARCH ONLY，live模式硬禁用。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/dayt_v57_v58_drawdown_derisk_20260917/reproduce.py",
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
        for version, name in VERSIONS.items():
            print(f"replay {symbol} {name}", flush=True)
            with redirect_stdout(io.StringIO()):
                result = replay(
                    version, daily, minute, rate=0.0005,
                    overrides={
                        "T_TARGET_VALUE": 40_000.0,
                        "QUANTILE_UNITS_SCALE": 1.0,
                    },
                    symbol=symbol, stock_name=symbol.split(".")[0],
                    initial_cash=100_000.0, initial_shares=1_000)
            results[symbol][version] = summarize(
                result, initial_equity, 1_000)
    aggregate = {}
    acceptance = {}
    for version in VERSIONS:
        rows = [variants[version] for variants in results.values()]
        aggregate[version] = {
            "account_net": sum(row["account_net"] for row in rows),
            "excess_net": sum(row["excess_net"] for row in rows),
            "mean_account_return": sum(row["account_return"] for row in rows) / len(rows),
            "mean_excess_return": sum(row["excess_return"] for row in rows) / len(rows),
            "worst_maximum_drawdown": max(row["maximum_drawdown"] for row in rows),
            "fees": sum(row["fees"] for row in rows),
            "completed_cycles": sum(row["completed_cycles"] for row in rows),
            "terminal_derisked_symbols": sum(row["terminal_derisked"] for row in rows),
        }
        acceptance[version] = all(
            row["failure"] is None and row["maximum_drawdown"] <= 0.25
            for row in rows)
    strategy_hashes = {}
    for version in VERSIONS:
        path = ROOT / "Stragety/MiniQMT_Stragety/DayT" / STRATEGIES[version]
        strategy_hashes[version] = sha256(path)
    payload = {
        "method": {
            "range": [START, END],
            "bar_period": "1 minute",
            "fee_rate_each_side": 0.0005,
            "initial_cash_per_symbol": 100_000.0,
            "initial_shares_per_symbol": 1_000,
            "t_target_value": 40_000.0,
            "quantile_units_scale": 1.0,
            "acceptance_maximum_drawdown": 0.25,
        },
        "hashes": {"strategies": strategy_hashes, "datasets": dataset_hashes},
        "results": results,
        "aggregate": aggregate,
        "acceptance": acceptance,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    (OUT / "reproduce.py").write_text(
        "from pathlib import Path\n"
        "import runpy\n\n"
        "root = Path(__file__).resolve().parents[2]\n"
        "runpy.run_path(str(root / 'analysis' / "
        "'backtest_v57_v58_drawdown_derisk.py'), run_name='__main__')\n",
        encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
