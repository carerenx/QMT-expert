"""Daily-reset backtest of v56, v056 and v056 with forward-T enabled and no FWD-T stop-loss.

Every trading day restarts from cash 100,000, 1,000 base shares and a fresh
strategy state.  This isolates single-session behaviour; it does not model
overnight carry, T+1 sellable quantities or cross-day recovery, so it cannot
replace the strict continuous-account reports.
"""

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


OUT = ROOT / "analysis/dayt_v56_v056_fwdt_daily_reset_20260918"
START = "20250915"
END = "20260911"
FEE_RATE = 0.0005
INITIAL_CASH = 100_000.0
INITIAL_SHARES = 1_000
COMMON_OVERRIDES = {"T_TARGET_VALUE": 40_000.0, "QUANTILE_UNITS_SCALE": 1.0}

# FWD-T stop-loss sentinel: the guard fires when price <= avg_bp*(1-STOP_LOSS_PCT).
# 1.0 therefore disables the stop without touching the shared config default.
NO_STOP_SENTINEL = 1.0

VARIANTS = {
    "v56": {
        "version": "v56_nomom",
        "name": "v56基准",
        "overrides": {},
        "cfg_overrides": {},
    },
    "v056_frozen": {
        "version": "v056_nomom",
        "name": "v056 正T冻结",
        "overrides": {"LONG_RESEARCH_DISABLED": True},
        "cfg_overrides": {},
    },
    "v056_fwdt_nostop": {
        "version": "v056_nomom",
        "name": "v056 正T开启·无正T止损",
        "overrides": {"LONG_RESEARCH_DISABLED": False},
        "cfg_overrides": {"STOP_LOSS_PCT": NO_STOP_SENTINEL},
    },
}
ORDER = ("v56", "v056_frozen", "v056_fwdt_nostop")

DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}

REV_OPEN = "REV-T sell"
REV_CLOSE_PREFIX = "REV-T buyback"
LONG_OPEN = "FWD-T buy"
LONG_CLOSE = ("FWD-T sell", "FWD-T force sell")


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


def lane_tape(trades: list[dict], mark: float) -> dict:
    """Recompute realized and marked-out gross per lane from the fill tape.

    This is an attribution recomputation, not the authoritative P&L; the
    account and excess figures stay the reported totals.  It exists so the
    report can separate reverse-T from forward-T contribution, and so the
    share of the result that is still unrealized at the day's close is
    visible rather than buried inside the account total.
    """
    rev_queue: list[list[float]] = []
    long_queue: list[list[float]] = []
    stats = {
        label: {"opens": 0, "closes": 0, "turnover": 0.0, "realized": 0.0,
                "open_shares": 0, "open_cost": 0.0, "open_marked": 0.0}
        for label in ("REV-T", "FWD-T")
    }
    for trade in trades:
        label, shares, price = trade["label"], trade["shares"], trade["price"]
        turnover = abs(shares) * price
        if label == REV_OPEN:
            rev_queue.append([price, abs(shares)])
            stats["REV-T"]["opens"] += 1
            stats["REV-T"]["turnover"] += turnover
        elif label.startswith(REV_CLOSE_PREFIX):
            _consume(rev_queue, abs(shares), price, stats["REV-T"], short=True)
            stats["REV-T"]["closes"] += 1
            stats["REV-T"]["turnover"] += turnover
        elif label == LONG_OPEN:
            long_queue.append([price, abs(shares)])
            stats["FWD-T"]["opens"] += 1
            stats["FWD-T"]["turnover"] += turnover
        elif label in LONG_CLOSE:
            _consume(long_queue, abs(shares), price, stats["FWD-T"], short=False)
            stats["FWD-T"]["closes"] += 1
            stats["FWD-T"]["turnover"] += turnover
    _mark_open(rev_queue, mark, stats["REV-T"], short=True)
    _mark_open(long_queue, mark, stats["FWD-T"], short=False)
    return stats


def _mark_open(queue: list[list[float]], mark: float, bucket: dict,
               short: bool) -> None:
    """Value whatever is still open at the session's closing price."""
    bucket["open_shares"] = sum(int(n) for _, n in queue)
    bucket["open_cost"] = sum(entry * n for entry, n in queue)
    bucket["open_marked"] = sum(
        ((entry - mark) if short else (mark - entry)) * n
        for entry, n in queue)


def _consume(queue: list[list[float]], quantity: int, price: float,
             bucket: dict, short: bool) -> None:
    remaining = quantity
    while remaining and queue:
        entry, shares = queue[0]
        used = min(shares, remaining)
        bucket["realized"] += ((entry - price) if short else (price - entry)) * used
        remaining -= used
        if used == shares:
            queue.pop(0)
        else:
            queue[0] = [entry, shares - used]


def summarize(rows: list[dict]) -> dict:
    fees = sum(row["turnover"] * FEE_RATE for row in rows)
    account_gross = sum(row["account_gross"] for row in rows)
    excess_gross = sum(row["excess_gross"] for row in rows)
    initial_equity = sum(row["initial_equity"] for row in rows)
    lanes = {
        lane: {field: (0 if field in ("opens", "closes", "open_shares") else 0.0)
               for field in ("opens", "closes", "turnover", "realized",
                             "open_shares", "open_cost", "open_marked")}
        for lane in ("REV-T", "FWD-T")}
    for row in rows:
        for lane, values in row["lanes"].items():
            for key in lanes[lane]:
                lanes[lane][key] += values[key]
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
        "unclosed_days": sum(bool(
            row["short_unclosed"] or row["long_unclosed"] or
            any(row["ledger_unclosed"].values())) for row in rows),
        "fwd_open_days": sum(row["lanes"]["FWD-T"]["open_shares"] > 0 for row in rows),
        "lanes": lanes,
        "lane_identity_max_error": max(
            abs(row["excess_gross"] - sum(
                row["lanes"][lane]["realized"] + row["lanes"][lane]["open_marked"]
                for lane in ("REV-T", "FWD-T"))) for row in rows),
    }


def money(value: float) -> str:
    return f"{value:,.2f}"


def _drawdown_sentence(aggregate: dict) -> str:
    """State the drawdown comparison only for what the numbers actually show."""
    worst = {key: aggregate[key]["worst_daily_drawdown"] for key in ORDER}
    values = set(round(value, 6) for value in worst.values())
    if len(values) == 1:
        return ("三者最差单日回撤相同，均为 "
                f"{max(values):.2%}；本样本内关闭正T止损没有改变最差单日回撤。")
    best_key = min(worst, key=worst.get)
    worst_key = max(worst, key=worst.get)
    return (f"最差单日回撤最低的是 {VARIANTS[best_key]['name']}"
            f"（{worst[best_key]:.2%}），最高的是 {VARIANTS[worst_key]['name']}"
            f"（{worst[worst_key]:.2%}）。")


def _lane_verdict(fwd_lane: dict) -> str:
    """Interpret the forward-T lane from its realized/unrealized split."""
    realized, marked = fwd_lane["realized"], fwd_lane["open_marked"]
    if fwd_lane["opens"] == 0:
        return ("正T通道在本样本中未产生任何成交，因此三个口径的差异全部来自反T通道。")
    total = realized + marked
    if total <= 0:
        return (f"正T腿合计贡献 {money(total)} 元，仍为负；"
                "**关闭止损并没有让正T通道转正。**")
    if marked < 0:
        return (f"正T腿已实现 {money(realized)} 元，但日末仍持有的正T腿浮亏 "
                f"{money(marked)} 元，把合计压到 {money(total)} 元——"
                f"已实现利润的 {abs(marked) / realized:.0%} 被未平腿的浮动亏损抵消。"
                "**该通道的账面转正依赖未平仓腿的处置方式，本样本不足以支持"
                "\"正T已经转正\"的结论。**")
    if marked > realized:
        return (f"正T腿的合计贡献 {money(total)} 元中，{money(marked)} 元是收盘盯市，"
                "超过已实现部分，说明该口径的优势主要来自未平仓腿的浮动盈亏而非落袋利润；"
                "**本样本不足以支持\"正T已经转正\"的结论。**")
    return (f"正T腿合计贡献 {money(total)} 元，其中已实现 {money(realized)} 元，"
            "主要来自已平仓腿；本样本内关闭 -1.5% 止损后正T通道转为正贡献，"
            "但样本仅来自 601869，仍需独立样本验证。")


def build_report(payload: dict) -> str:
    results = payload["summary"]
    aggregate = payload["aggregate"]
    base = aggregate["v56"]["excess_net"]
    best = max(ORDER, key=lambda key: aggregate[key]["excess_net"])
    fwdt = aggregate["v056_fwdt_nostop"]
    frozen = aggregate["v056_frozen"]
    fwd_lane = fwdt["lanes"]["FWD-T"]
    lines = [
        "# v56 / v056 / v056(正T开启·无正T止损) 每日重置回测",
        "",
        "## 结论",
        "",
        "本报告每天重新初始化现金、1000股底仓、策略状态和周期账本，衡量单日独立启动表现。"
        "它不模拟跨日持仓、T+1延续、风险库存或恢复过程，因此不能替代严格连续账户报告。",
        "",
        f"三个口径中合计超额净收益最高的是 **{VARIANTS[best]['name']}**"
        f"（{money(aggregate[best]['excess_net'])} 元），"
        f"相对 v56 基准变化 {money(aggregate[best]['excess_net'] - base)} 元，"
        f"相对 v056 正T冻结变化 "
        f"{money(aggregate[best]['excess_net'] - frozen['excess_net'])} 元。",
        "",
        _drawdown_sentence(aggregate),
        "",
        f"**收益质量是本报告最需要打折的地方。** 正T开启口径有 "
        f"{frozen['unclosed_days']} → {fwdt['unclosed_days']} 天的未闭合日，"
        f"其中 {fwdt['fwd_open_days']} 天带着未平正T腿过夜，"
        f"累计 {int(fwd_lane['open_shares'])} 股、成本 {money(fwd_lane['open_cost'])} 元。"
        "在每日重置口径下这些腿被直接丢弃，既不计入次日的真实持仓，"
        "也没有为日末强制平仓支付手续费。若实盘在日末强制平掉这批腿，"
        "结果会因卖出时点和额外费用而改变。同时，正T把手续费从 "
        f"{money(frozen['fees'])} 元推高到 {money(fwdt['fees'])} 元，"
        "这部分是确定的成本。",
        "",
        _lane_verdict(fwd_lane),
        "",
        f"600584 与 600105 在每日重置口径下三个版本均无成交，"
        f"全部差异来自 601869，不具备跨标的一致性，不能据此推广到其他标的。",
        "",
        "## 汇总对比",
        "",
        "| 策略口径 | 合计账户净收益 | 合计超额净收益 | 相对v56 | 等权超额收益率 | "
        "最差单日回撤 | 成交(总/反T/正T) | 手续费 | 盈利日/总日 | 未闭合日 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ORDER:
        row = aggregate[key]
        lanes = row["lanes"]
        fills_rev = lanes["REV-T"]["opens"] + lanes["REV-T"]["closes"]
        fills_fwd = lanes["FWD-T"]["opens"] + lanes["FWD-T"]["closes"]
        lines.append(
            f"| {VARIANTS[key]['name']} | {money(row['account_net'])} | "
            f"{money(row['excess_net'])} | {money(row['excess_net'] - base)} | "
            f"{row['mean_excess_return']:.3%} | {row['worst_daily_drawdown']:.2%} | "
            f"{row['fills']}/{fills_rev}/{fills_fwd} | {money(row['fees'])} | "
            f"{row['profitable_days']}/{row['days']} | {row['unclosed_days']} |")
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | 策略口径 | 账户净收益 | 超额净收益 | 超额收益率 | 最差单日回撤 | "
        "成交(总/反T/正T) | 周期 | 手续费 | 仓位偏离日 | 未闭合日 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol, variants in results.items():
        for key in ORDER:
            row = variants[key]
            lanes = row["lanes"]
            fills_rev = lanes["REV-T"]["opens"] + lanes["REV-T"]["closes"]
            fills_fwd = lanes["FWD-T"]["opens"] + lanes["FWD-T"]["closes"]
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | {money(row['account_net'])} | "
                f"{money(row['excess_net'])} | {row['excess_return']:.3%} | "
                f"{row['worst_daily_drawdown']:.2%} | "
                f"{row['fills']}/{fills_rev}/{fills_fwd} | {row['cycles']} | "
                f"{money(row['fees'])} | {row['position_gap_days']} | "
                f"{row['unclosed_days']} |")
    lines.extend([
        "",
        "## 分lane归因（按成交流水FIFO重算）",
        "",
        "反T = 先卖后买；正T = 先买后卖。`已实现` 只统计当日已平仓的腿，"
        "`日末盯市` 是收盘仍持有的腿按当日收盘价的浮动盈亏，"
        "`合计` = 已实现 + 日末盯市，恒等于该口径的超额毛收益。均不含手续费。",
        "",
        "| 策略口径 | 反T开/平 | 反T成交额 | 反T已实现 | 反T日末盯市 | "
        "正T开/平 | 正T成交额 | 正T已实现 | 正T日末盯市 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key in ORDER:
        row = aggregate[key]["lanes"]
        rev = row["REV-T"]
        fwd = row["FWD-T"]
        lines.append(
            f"| {VARIANTS[key]['name']} | {rev['opens']}/{rev['closes']} | "
            f"{money(rev['turnover'])} | {money(rev['realized'])} | "
            f"{money(rev['open_marked'])} | "
            f"{fwd['opens']}/{fwd['closes']} | {money(fwd['turnover'])} | "
            f"{money(fwd['realized'])} | {money(fwd['open_marked'])} |")
    lines.extend([
        "",
        "## 日末未平正T腿",
        "",
        "| 策略口径 | 带未平正T腿过夜的天数 | 日末未平正T股数 | 未平腿成本 | 日末盯市盈亏 |",
        "|---|---:|---:|---:|---:|",
    ])
    for key in ORDER:
        row = aggregate[key]
        lane = row["lanes"]["FWD-T"]
        lines.append(
            f"| {VARIANTS[key]['name']} | {row['fwd_open_days']} | "
            f"{int(lane['open_shares'])} | {money(lane['open_cost'])} | "
            f"{money(lane['open_marked'])} |")
    lines.extend([
        "",
        "## 口径限制",
        "",
        "- 每个交易日均以现金 100,000 元、底仓 1,000 股、全新策略状态重新开始；日与日之间不复投。",
        f"- 本地 1 分钟 K 线，区间 {START}至{END}；仅纳入至少 230 根分钟线且有 80 日历史的交易日。",
        "- 每边费率 0.05%，无额外滑点；费用按成交额后处理扣除。",
        "- `T_TARGET_VALUE=40,000`、`QUANTILE_UNITS_SCALE=1.0`，其余参数使用各策略当前值。",
        "- `v056 正T冻结` 使用策略默认 `LONG_RESEARCH_DISABLED=True`；"
        "`v056 正T开启·无正T止损` 设为 `False` 并把 `STOP_LOSS_PCT` 置为 1.0，"
        "使 `price <= 均价×(1-STOP_LOSS_PCT)` 永不成立，即关闭正T止损。",
        "- 关闭正T止损只解除 -1.5% 强制卖出；日末强制平仓由 `ENABLE_FORCE_CLOSE` 控制，"
        "本轮仍为 False，因此未平正T腿不会在日末卖出，只计入未闭合日。",
        "- 最大回撤是所有独立交易日中最差的单日内回撤，不能与连续账户最大回撤直接比较。",
        "- 日末未闭合腿不会带入次日；相关日单独计数，不能将其当成已经完成的策略收益。",
        "",
        "## 完整性检查",
        "",
    ])
    failures = [
        f"{symbol}/{key}: {row['failure_count']}"
        for symbol, variants in results.items()
        for key, row in variants.items() if row["failure_count"]]
    lines.append("- 回放失败：" + ("；".join(failures) if failures else "无。"))
    identity = max(aggregate[key]["lane_identity_max_error"] for key in ORDER)
    lines.append(
        "- lane 归因恒等式（超额毛收益 = Σ 已实现 + Σ 日末盯市）最大误差："
        f"{identity:.6f} 元。")
    lines.extend([
        "- 全部逐日结果、策略/数据哈希和参数见 `results.json`。",
        "- 本报告写入独立目录，未覆盖严格连续账户报告。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/backtest_v56_v056_fwdt_daily_reset_20260918.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def aggregate_summaries(summary: dict) -> dict:
    """Roll per-symbol summaries up across the three symbols."""
    aggregate = {}
    for key in ORDER:
        rows = [summary[symbol][key] for symbol in DATASETS]
        lanes = {}
        for lane in ("REV-T", "FWD-T"):
            lanes[lane] = {
                field: sum(row["lanes"][lane][field] for row in rows)
                for field in ("opens", "closes", "turnover", "realized",
                              "open_shares", "open_cost", "open_marked")}
        aggregate[key] = {
            "account_net": sum(row["account_net"] for row in rows),
            "excess_gross": sum(row["excess_gross"] for row in rows),
            "excess_net": sum(row["excess_net"] for row in rows),
            "mean_excess_return": sum(
                row["excess_return"] for row in rows) / len(rows),
            "worst_daily_drawdown": max(
                row["worst_daily_drawdown"] for row in rows),
            "fees": sum(row["fees"] for row in rows),
            "fills": sum(row["fills"] for row in rows),
            "unclosed_days": sum(row["unclosed_days"] for row in rows),
            "fwd_open_days": sum(row["fwd_open_days"] for row in rows),
            "profitable_days": sum(row["profitable_days"] for row in rows),
            "days": sum(row["days"] for row in rows),
            "lanes": lanes,
            "lane_identity_max_error": max(
                row["lane_identity_max_error"] for row in rows),
        }
    return aggregate


def run_variant(symbol: str, key: str, daily, grouped) -> list[dict]:
    config = VARIANTS[key]
    rows = []
    for index, (day, bars) in enumerate(grouped, 1):
        history = daily.loc[daily.index < day]
        if len(bars) < 230 or len(history) < 80:
            continue
        overrides = dict(COMMON_OVERRIDES)
        overrides.update(config["overrides"])
        with redirect_stdout(io.StringIO()):
            result = replay(
                config["version"], history, bars, slip=0.0,
                initial_cash=INITIAL_CASH, initial_shares=INITIAL_SHARES,
                symbol=symbol, overrides=overrides,
                cfg_overrides=config["cfg_overrides"])
        result["lanes"] = lane_tape(result["trades"], float(bars.iloc[-1].close))
        rows.append(result)
        if result["failure"]:
            raise RuntimeError(f"{symbol}/{key}/{day}: {result['failure']}")
        if index % 60 == 0:
            print(f"{symbol} {config['name']}: {index}/{len(grouped)}", flush=True)
    return rows


def rebuild_report_only() -> None:
    """Rewrite README.md from an existing results.json without replaying."""
    path = OUT / "results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary"] = {
        symbol: {key: summarize(rows) for key, rows in variants.items()}
        for symbol, variants in payload["daily_results"].items()}
    payload["aggregate"] = aggregate_summaries(payload["summary"])
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


def main() -> None:
    if "--report-only" in sys.argv:
        rebuild_report_only()
        return
    smoke = "--smoke" in sys.argv
    if OUT.exists() and not smoke:
        raise FileExistsError("refuse to overwrite existing report: " + str(OUT))
    OUT.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    summary: dict = {}
    dataset_hashes = {}
    for symbol, folder in DATASETS.items():
        daily, minute = load_data(folder)
        dataset_hashes[symbol] = {
            "1d.csv": sha256(folder / "1d.csv"),
            "1m.csv": sha256(folder / "1m.csv"),
            "minute_rows": len(minute),
            "minute_range": [minute.index[0], minute.index[-1]],
        }
        grouped = list(minute.groupby(minute.index.str[:8]))
        if smoke:
            grouped = [entry for entry in grouped if len(entry[1]) >= 230][-3:]
        raw[symbol] = {}
        summary[symbol] = {}
        for key in ORDER:
            print(f"replay {symbol} {VARIANTS[key]['name']}"
                  f" [{VARIANTS[key]['version']}]", flush=True)
            rows = run_variant(symbol, key, daily, grouped)
            raw[symbol][key] = rows
            summary[symbol][key] = summarize(rows)
            print(f"completed {symbol} {VARIANTS[key]['name']}: "
                  f"{len(rows)} days", flush=True)
            if smoke:
                print(json.dumps(summary[symbol][key], ensure_ascii=False,
                                 indent=2, default=str))
    aggregate = aggregate_summaries(summary)
    strategy_hashes = {
        key: sha256(ROOT / "Stragety/MiniQMT_Stragety/DayT"
                    / STRATEGIES[VARIANTS[key]["version"]])
        for key in ORDER}
    config_hash = sha256(
        ROOT / "Stragety/MiniQMT_Stragety/core/config.py")
    payload = {
        "method": {
            "model": "INDEPENDENT_DAILY_RESET",
            "range": [START, END],
            "initial_cash_each_day": INITIAL_CASH,
            "initial_shares_each_day": INITIAL_SHARES,
            "fee_rate_each_side": FEE_RATE,
            "slippage": 0.0,
            "common_overrides": COMMON_OVERRIDES,
            "forward_t_stop_loss_sentinel": NO_STOP_SENTINEL,
            "variants": {
                key: {
                    "name": VARIANTS[key]["name"],
                    "version": VARIANTS[key]["version"],
                    "entry": STRATEGIES[VARIANTS[key]["version"]],
                    "overrides": {**COMMON_OVERRIDES, **VARIANTS[key]["overrides"]},
                    "cfg_overrides": VARIANTS[key]["cfg_overrides"],
                }
                for key in ORDER},
        },
        "hashes": {
            "strategies": strategy_hashes,
            "core_config": config_hash,
            "datasets": dataset_hashes,
        },
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
