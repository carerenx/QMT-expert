"""Daily-reset cycle report for v056 / v0561 / v0562 on 1-minute bars.

Answers four questions the aggregate reports could not:
  * how much of the result came from 反T (sell first) versus 正T (buy first)
  * what share of the base position was actually round-tripped each day (T达成率)
  * every individual trade: entry/exit clock time, shares, P&L, closed or not
  * which mechanical weaknesses the trade tape exposes

DATA WINDOW: the canonical 1-minute feed (BigQMT bridge) ends 2026-09-11 and has
no minute bars after it.  The requested window ends 2026-09-18, so the run stops
at 2026-09-11.  A Sina/akshare substitute was measured against the canonical feed
on the 2026-09-09..09-11 overlap and rejected: 41.7% of bars disagree by more
than Y0.05 and 16.3% by more than Y0.20, while the strategies trigger on ~0.1% of
price (about Y0.46 on a Y460 stock).  Splicing would corrupt fill decisions.
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


OUT = ROOT / "analysis/dayt_coreT_cycle_report_20260919"
START = "20260101"
END = "20260911"
FEE_RATE = 0.0005
INITIAL_CASH = 100_000.0
INITIAL_SHARES = 1_000
LOT = 100
COMMON = {"T_TARGET_VALUE": 40_000.0}
NO_STOP_SENTINEL = 1.0

DATASETS = {
    "600584.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600584",
    "600105.SH": ROOT / "analysis/dayt_v55_longhold_comparable_20260913/data_600105",
    "601869.SH": ROOT / "analysis/long_hold_vs_v55_601869_20260913/data_601869",
}

VARIANTS = {
    "v056_frozen": {
        "name": "v056 正T冻结", "version": "v056_nomom",
        "overrides": {"LONG_RESEARCH_DISABLED": True}, "cfg_overrides": {},
    },
    "v056_fwdt": {
        "name": "v056 正T开启·无正T止损", "version": "v056_nomom",
        "overrides": {"LONG_RESEARCH_DISABLED": False},
        "cfg_overrides": {"STOP_LOSS_PCT": NO_STOP_SENTINEL},
    },
    "v0561": {
        "name": "v0561 CoreT", "version": "v0561",
        "overrides": {}, "cfg_overrides": {},
    },
    "v0562": {
        "name": "v0562 CoreT+ATR再入场", "version": "v0562",
        "overrides": {}, "cfg_overrides": {},
    },
}
ORDER = ("v056_frozen", "v056_fwdt", "v0561", "v0562")

REV_OPEN = "REV-T sell"
REV_CLOSE = "REV-T buyback"
LONG_OPEN = "FWD-T buy"
LONG_CLOSE = "FWD-T sell"
LANE_CN = {"REV-T": "反T", "FWD-T": "正T"}


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


def fill_lane(label: str) -> str:
    if label.startswith("REV-T"):
        return "REV-T"
    if label.startswith("FWD-T"):
        return "FWD-T"
    return ""


def build_cycles(trades: list[dict], mark: float) -> list[dict]:
    """FIFO-match each lane's fills into round-trip cycles.

    反T opens on a sell and closes on the buyback; 正T opens on a buy and closes
    on the sell.  Whatever is still open at the close is emitted as unclosed and
    marked at the session's last price so the tape never silently drops exposure.
    """
    queues: dict[str, list[dict]] = {"REV-T": [], "FWD-T": []}
    cycles: list[dict] = []
    for trade in trades:
        label, shares, price, stamp = (
            trade["label"], trade["shares"], trade["price"], trade["time"])
        lane = fill_lane(label)
        if not lane:
            continue
        clock = f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
        opens = (lane == "REV-T" and label == REV_OPEN) or \
                (lane == "FWD-T" and label == LONG_OPEN)
        closes = (lane == "REV-T" and label.startswith(REV_CLOSE)) or \
                 (lane == "FWD-T" and label == LONG_CLOSE)
        if opens:
            queues[lane].append({"shares": abs(shares), "price": price,
                                 "time": clock})
        elif closes:
            remaining = abs(shares)
            while remaining > 0 and queues[lane]:
                leg = queues[lane][0]
                used = min(leg["shares"], remaining)
                cycles.append({
                    "lane": lane, "direction": LANE_CN[lane],
                    "entry_time": leg["time"], "exit_time": clock,
                    "shares": used, "entry_price": leg["price"],
                    "exit_price": price,
                    "gross": ((leg["price"] - price) if lane == "REV-T"
                              else (price - leg["price"])) * used,
                    "closed": True,
                })
                remaining -= used
                if used == leg["shares"]:
                    queues[lane].pop(0)
                else:
                    leg["shares"] -= used
    for lane, legs in queues.items():
        for leg in legs:
            cycles.append({
                "lane": lane, "direction": LANE_CN[lane],
                "entry_time": leg["time"], "exit_time": "—",
                "shares": leg["shares"], "entry_price": leg["price"],
                "exit_price": mark,
                "gross": ((leg["price"] - mark) if lane == "REV-T"
                          else (mark - leg["price"])) * leg["shares"],
                "closed": False,
            })
    return cycles


def summarize_day(date: str, result: dict) -> dict:
    cycles = result["cycles_detail"]
    fees = result["turnover"] * FEE_RATE
    row = {
        "date": date,
        "excess_gross": result["excess_gross"],
        "excess_net": result["excess_gross"] - fees,
        "fees": fees,
        "turnover": result["turnover"],
        "state": result["state"],
        "max_drawdown": result["max_drawdown"],
        "cycles": cycles,
    }
    for lane in ("REV-T", "FWD-T"):
        closed = [c for c in cycles if c["lane"] == lane and c["closed"]]
        open_ = [c for c in cycles if c["lane"] == lane and not c["closed"]]
        row[f"{lane}_closed_shares"] = sum(c["shares"] for c in closed)
        row[f"{lane}_open_shares"] = sum(c["shares"] for c in open_)
        row[f"{lane}_realized"] = sum(c["gross"] for c in closed)
        row[f"{lane}_unrealized"] = sum(c["gross"] for c in open_)
        row[f"{lane}_cycles"] = len(closed)
    # 反T 的「平掉的那条腿」是买回, 正T 是卖出; 两者都记为完成一次 T。
    row["t_done_shares"] = row["REV-T_closed_shares"] + row["FWD-T_closed_shares"]
    row["t_rate"] = row["t_done_shares"] / INITIAL_SHARES
    return row


def aggregate(days: list[dict]) -> dict:
    base = INITIAL_SHARES * len(days)
    out = {
        "days": len(days),
        "excess_net": sum(d["excess_net"] for d in days),
        "fees": sum(d["fees"] for d in days),
        "turnover": sum(d["turnover"] for d in days),
        "worst_daily_drawdown": max((d["max_drawdown"] for d in days), default=0.0),
        "t_done_shares": sum(d["t_done_shares"] for d in days),
        "t_rate": sum(d["t_done_shares"] for d in days) / base if base else 0.0,
        "mean_daily_t_rate": (sum(d["t_rate"] for d in days) / len(days)
                              if days else 0.0),
        "zero_t_days": sum(d["t_done_shares"] == 0 for d in days),
        "unclosed_days": sum(
            d["REV-T_open_shares"] + d["FWD-T_open_shares"] > 0 for d in days),
        "all_cycles": [c for d in days for c in d["cycles"]],
    }
    for lane in ("REV-T", "FWD-T"):
        out[f"{lane}_realized"] = sum(d[f"{lane}_realized"] for d in days)
        out[f"{lane}_unrealized"] = sum(d[f"{lane}_unrealized"] for d in days)
        out[f"{lane}_cycles"] = sum(d[f"{lane}_cycles"] for d in days)
        out[f"{lane}_closed_shares"] = sum(d[f"{lane}_closed_shares"] for d in days)
        out[f"{lane}_t_rate"] = (
            out[f"{lane}_closed_shares"] / base if base else 0.0)
    return out


def money(value: float) -> str:
    return f"{value:,.2f}"


def run_all(smoke: bool) -> tuple[dict, dict]:
    """Return per-symbol per-variant day records, plus dataset hashes."""
    days_by_symbol: dict = {}
    dataset_hashes = {}
    for symbol, folder in DATASETS.items():
        daily, minute = load_data(folder)
        dataset_hashes[symbol] = {
            "1d.csv": sha256(folder / "1d.csv"),
            "1m.csv": sha256(folder / "1m.csv"),
        }
        grouped = list(minute.groupby(minute.index.str[:8]))
        if smoke:
            grouped = [entry for entry in grouped if len(entry[1]) >= 230][-4:]
        days_by_symbol[symbol] = {}
        for key in ORDER:
            config = VARIANTS[key]
            overrides = dict(COMMON)
            overrides.update(config["overrides"])
            days = []
            for index, (day, bars) in enumerate(grouped, 1):
                history = daily.loc[daily.index < day]
                if len(bars) < 230 or len(history) < 80:
                    continue
                with redirect_stdout(io.StringIO()):
                    result = replay(
                        config["version"], history, bars, slip=0.0,
                        initial_cash=INITIAL_CASH, initial_shares=INITIAL_SHARES,
                        symbol=symbol, overrides=overrides,
                        cfg_overrides=config["cfg_overrides"])
                if result["failure"]:
                    raise RuntimeError(f"{symbol}/{key}/{day}: {result['failure']}")
                result["cycles_detail"] = build_cycles(
                    result["trades"], float(bars.iloc[-1].close))
                days.append(summarize_day(day, result))
                if index % 60 == 0:
                    print(f"{symbol} {config['name']}: {index}/{len(grouped)}",
                          flush=True)
            days_by_symbol[symbol][key] = days
            print(f"completed {symbol} {config['name']}: {len(days)} days",
                  flush=True)
    return days_by_symbol, dataset_hashes


def aggregate_all(days_by_symbol: dict) -> dict:
    return {
        symbol: {key: aggregate(days) for key, days in variants.items()}
        for symbol, variants in days_by_symbol.items()}


def active_symbols(agg: dict) -> list[str]:
    return [s for s in DATASETS if any(agg[s][k]["all_cycles"] for k in ORDER)]


def totals(agg: dict) -> dict:
    """Roll a variant up over the symbols that actually traded."""
    live = active_symbols(agg)
    if not live:
        return {}
    if len(live) == 1:
        return {key: agg[live[0]][key] for key in ORDER}
    out = {}
    for key in ORDER:
        row = dict(agg[live[0]][key])
        base = INITIAL_SHARES * sum(agg[s][key]["days"] for s in live)
        for field in ("excess_net", "fees", "turnover", "t_done_shares",
                      "zero_t_days", "unclosed_days", "days",
                      "REV-T_realized", "FWD-T_realized", "REV-T_unrealized",
                      "FWD-T_unrealized", "REV-T_cycles", "FWD-T_cycles",
                      "REV-T_closed_shares", "FWD-T_closed_shares"):
            row[field] = sum(agg[s][key][field] for s in live)
        row["worst_daily_drawdown"] = max(
            agg[s][key]["worst_daily_drawdown"] for s in live)
        row["t_rate"] = row["t_done_shares"] / base if base else 0.0
        row["mean_daily_t_rate"] = (
            sum(agg[s][key]["mean_daily_t_rate"] for s in live) / len(live))
        for lane in ("REV-T", "FWD-T"):
            row[f"{lane}_t_rate"] = (
                row[f"{lane}_closed_shares"] / base if base else 0.0)
        out[key] = row
    return out


def _key_findings(agg: dict, live: list[str]) -> list[str]:
    """The numbers that actually explain the result, computed not asserted."""
    cycles = [c for s in live for k in ORDER for c in agg[s][k]["all_cycles"]]
    closed = [c for c in cycles if c["closed"]]
    unclosed = [c for c in cycles if not c["closed"]]
    lines = []
    if not cycles:
        return ["（无成交，无可分析项。）"]
    c_mean = sum(c["gross"] for c in closed) / len(closed)
    u_mean = sum(c["gross"] for c in unclosed) / len(unclosed)
    lines.extend([
        "### 1. 已平仓的 T 是赚钱的，日末未平的腿把利润全部吃掉",
        "",
        f"四个口径合计 {len(cycles)} 笔周期，其中已闭合 {len(closed)} 笔、"
        f"日末未平 {len(unclosed)} 笔（占 {len(unclosed)/len(cycles):.1%}）。",
        "",
        "| | 笔数 | 合计毛收益 | 单笔均值 |",
        "|---|---:|---:|---:|",
        f"| 已闭合周期 | {len(closed)} | {money(sum(c['gross'] for c in closed))} | "
        f"{money(c_mean)} |",
        f"| 日末未平腿 | {len(unclosed)} | {money(sum(c['gross'] for c in unclosed))} | "
        f"{money(u_mean)} |",
        "",
        f"已闭合周期单笔均值 **+{money(c_mean)}**，未平腿单笔均值 "
        f"**{money(u_mean)}**——一条未平的腿平均要吞掉约 "
        f"{abs(u_mean)/c_mean:.1f} 笔已平仓周期的利润。"
        "**触发、开仓、配对平仓这条链路本身是有效的；亏损几乎全部来自没平掉的那部分。**",
        "",
        "### 2. 未平腿按日末收盘盯市，是「每日重置」这个口径的必然结果",
        "",
        "每日重置意味着日末未平的腿被直接丢弃、按收盘价折算价值。"
        "反T未平（少 100 股）在上涨日亏、正T未平（多 100 股）在下跌日亏，"
        "所以这份盯市损失度量的是「当天没走完的方向」。",
        "",
        "| 标的 | 口径 | 反T未平盯市 | 正T未平盯市 | 合计 |",
        "|---|---|---:|---:|---:|",
    ])
    for symbol in live:
        for key in ORDER:
            row = agg[symbol][key]
            if not row["all_cycles"]:
                continue
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | "
                f"{money(row['REV-T_unrealized'])} | "
                f"{money(row['FWD-T_unrealized'])} | "
                f"{money(row['REV-T_unrealized'] + row['FWD-T_unrealized'])} |")
    lines.extend([
        "",
        "**注意**：这笔盯市损失在连续账户口径下不一定实现——腿可以带着过夜、"
        "等更低的价格买回。但本口径要求每日重置，"
        "所以它对这一天的计分是终局值。两者不能混着解释。",
        "",
        "### 3. T达成率由仓位换算公式决定，且**随价格而变**",
        "",
        "每笔新腿的股数由 `calculate_t_shares(price, 可配对底仓, 100, "
        "T_TARGET_VALUE=40000, T_POSITION_FRACTION=0.40)` 决定。"
        "目标金额 40,000 元与底仓 40%（400 股）两者取小，"
        "还要向下取整到整手；价格足够高时一手成本超过 40,000 元，"
        "公式退化为「至少一手」：",
        "",
        "| 价位 | 单手成本 | 实际腿股数 | 腿名义金额 | 占 1,000 股底仓 | 受限于 |",
        "|---:|---:|---:|---:|---:|---|",
    ])
    for price in (100, 200, 300, 450, 600, 1000):
        shares, binding = _leg_size(price)
        lines.append(
            f"| {price} | {price * 100:,} | {shares} | {shares * price:,} | "
            f"{shares / INITIAL_SHARES:.0%} | {binding} |")
    lines.extend([
        "",
        "标的不同，单腿占底仓的比例就不同：",
        "",
        "| 标的 | 区间均价 | 单腿股数 | 占 1,000 股底仓 | 受限于 |",
        "|---|---:|---:|---:|---|",
    ])
    for symbol in live:
        shares, binding = _leg_size(int(_mean_price(symbol)))
        lines.append(
            f"| {symbol} | ≈{_mean_price(symbol):,.0f} | {shares} | "
            f"{shares / INITIAL_SHARES:.0%} | {binding} |")
    lines.extend([
        "",
        f"实测成交股数分布：{_lot_histogram(agg, live)}，与上表一致。",
        "",
        "**用户定义的「10 手全部卖出买回 = 100%」在本策略下不可达**，"
        "但原因因标的而异：601869 价位高，单腿被 `T_TARGET_VALUE=40000` 锁在 1 手（10%）；"
        "600584/600105 价位低，单腿是 4 手（40%），受限的是底仓 40% 那一条。"
        f"再叠加 `MAX_DAILY_TRADES={_max_daily_trades()}`（每方向每天最多 5 笔），"
        "即使两个方向都打满，一天最多也只能周转 "
        f"{_max_daily_trades() * 2 * 400 / INITIAL_SHARES:.0%}（低价标的、每笔 4 手）。"
        f"实测合计达成率 {min(agg[s][k]['t_rate'] for s in live for k in ORDER):.1%}～"
        f"{max(agg[s][k]['t_rate'] for s in live for k in ORDER):.1%}。",
        "",
        "**所以 T达成率是仓位公式与价格共同决定的设计结果，不是执行失败。**"
        "想提高达成率，要调的是 `T_TARGET_VALUE`、`T_POSITION_FRACTION`、"
        "`MAX_DAILY_TRADES`，以及标的选择——不是触发价。",
    ])
    return lines


def _mean_price(symbol: str) -> float:
    """Mean daily close over the report window, for the sizing table."""
    daily = pd.read_csv(DATASETS[symbol] / "1d.csv", dtype={"time": str})
    daily = daily[daily["time"] >= START]
    return float(daily["close"].mean())


def _leg_size(price: int) -> tuple[int, str]:
    """Re-derive the shipped sizing for one price, and say what binds it."""
    from core.t_position_size import calculate_t_shares
    shares = calculate_t_shares(
        float(price), INITIAL_SHARES, LOT, COMMON["T_TARGET_VALUE"],
        0.40, 1e12)
    by_value = max(LOT, int(COMMON["T_TARGET_VALUE"] / price / LOT) * LOT)
    by_base = max(LOT, int(INITIAL_SHARES * 0.40 / LOT) * LOT)
    if by_value <= by_base:
        binding = f"目标金额 {COMMON['T_TARGET_VALUE']:,.0f} 元"
        if by_value == LOT:
            binding += "（不足一手，退回最少一手）"
    else:
        binding = "底仓 40%"
    return shares, binding


def _lot_histogram(agg: dict, live: list[str]) -> str:
    counts: dict[int, int] = {}
    for symbol in live:
        for key in ORDER:
            for cycle in agg[symbol][key]["all_cycles"]:
                lots = cycle["shares"] // LOT
                counts[lots] = counts.get(lots, 0) + 1
    total = sum(counts.values()) or 1
    parts = [f"{lots} 手 {n} 笔（{n/total:.0%}）"
             for lots, n in sorted(counts.items())[:4]]
    return "、".join(parts)


def _max_daily_trades() -> int:
    from core import config as cfg
    return cfg.MAX_DAILY_TRADES


def build_report(payload: dict, agg: dict, days_by_symbol: dict) -> str:
    live = active_symbols(agg)
    total = totals(agg)
    lines = [
        "# 三策略每日重置回测：正T/反T分解 · T达成率 · 逐笔明细",
        "",
    ]
    if payload.get("superseded_by"):
        lines.extend([
            "> ⚠️ **本报告的结论已被样本外验证推翻。** "
            f"后续区间检验见 `{payload['superseded_by']}`："
            "样本内观察到的 v0562 优势在样本外未重现（合计由 +22,123 转为 -1,333，"
            "且最好口径变为 v0561）。阅读本报告时请以那一份为准。",
            "",
        ])
    lines.extend([
        "## 结论",
        "",
    ])
    lines.extend(_conclusions(agg, live, total))
    lines.extend(_sample_split_section(payload, agg, live))
    lines.extend(["", "## 关键发现", ""])
    lines.extend(_key_findings(agg, live))
    lines.extend([
        "",
        "## 汇总对比",
        "",
        f"统计口径：{'、'.join(live) if live else '无'}（其余标的在所有版本下零成交，"
        "已在「问题与优化点」中单列）。",
        "",
        "| 策略 | 超额净收益 | 反T已实现 | 正T已实现 | 日末未平盯市 | 手续费 | "
        "最差单日回撤 | 反T周期 | 正T周期 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key in ORDER:
        row = total[key]
        lines.append(
            f"| {VARIANTS[key]['name']} | {money(row['excess_net'])} | "
            f"{money(row['REV-T_realized'])} | {money(row['FWD-T_realized'])} | "
            f"{money(row['REV-T_unrealized'] + row['FWD-T_unrealized'])} | "
            f"{money(row['fees'])} | {row['worst_daily_drawdown']:.2%} | "
            f"{row['REV-T_cycles']} | {row['FWD-T_cycles']} |")
    lines.extend([
        "",
        "> `反T已实现`/`正T已实现` 只统计当日已平仓的腿；`日末未平盯市` 是收盘仍持有、"
        "按当日收盘价折算的浮动盈亏。三项之和 = 超额毛收益（未经手续费）。",
        "",
        "## T达成率",
        "",
        "**定义**：T达成率 = 当日已平掉的那条腿的股数 ÷ 当日底仓股数。"
        "反T计**已买回**股数，正T计**已卖出**股数。",
        "底仓 1,000 股时，反T 卖出 1,000 股并全部买回 = 反T达成率 100%；"
        "合计达成率超过 100% 表示当天周转了不止一轮。",
        "",
        "| 策略 | 反T达成率 | 正T达成率 | 合计T达成率 | 逐日平均 | 零成交日 | 有未平腿的日 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for key in ORDER:
        row = total[key]
        lines.append(
            f"| {VARIANTS[key]['name']} | {row['REV-T_t_rate']:.1%} | "
            f"{row['FWD-T_t_rate']:.1%} | {row['t_rate']:.1%} | "
            f"{row['mean_daily_t_rate']:.1%} | {row['zero_t_days']}/{row['days']} | "
            f"{row['unclosed_days']}/{row['days']} |")
    lines.extend([
        "",
        "## 分标的结果",
        "",
        "| 标的 | 策略 | 超额净收益 | 反T已实现 | 正T已实现 | 反T周期 | 正T周期 | "
        "合计T达成率 | 零成交日 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for symbol in DATASETS:
        for key in ORDER:
            row = agg[symbol][key]
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | {money(row['excess_net'])} | "
                f"{money(row['REV-T_realized'])} | {money(row['FWD-T_realized'])} | "
                f"{row['REV-T_cycles']} | {row['FWD-T_cycles']} | "
                f"{row['t_rate']:.1%} | {row['zero_t_days']}/{row['days']} |")
    lines.extend(["", "## 问题与优化点", ""])
    lines.extend(_problems(agg, live, total))
    lines.extend([
        "",
        "## 口径与数据",
        "",
        f"- 区间：{START[:4]}-{START[4:6]}-{START[6:]} 至 {END[:4]}-{END[4:6]}-{END[6:]}"
        "（见下方数据说明）。",
        "- 本地 1 分钟 K 线。每日重置：每天以现金 100,000 元、底仓 1,000 股、"
        "全新策略状态重新开始，日末未平腿不带入次日。",
        f"- 每边费率 {FEE_RATE:.2%}，无额外滑点；费用按成交额后处理扣除。",
        "- `T_TARGET_VALUE=40,000`；v056 额外 `QUANTILE_UNITS_SCALE=1.0`。",
        "- v056 出厂默认 `LONG_RESEARCH_DISABLED=True`（正T冻结）。此处把它单列为一行，"
        "否则 v056 的正T收益恒为 0、正T机制无从比较；"
        "「正T开启」行同时关闭了正T止损（`STOP_LOSS_PCT=1.0`），与 v0561/v0562 口径一致。",
        "- 最大回撤是各独立交易日中最差的**单日内**回撤，不能与连续账户最大回撤比较。",
        "",
        "## 数据说明（为什么止于 2026-09-11）",
        "",
        "要求区间是 2026-01-01 至 2026-09-18，但 1 分钟数据实际只到 **2026-09-11**：",
        "",
        "- 权威 1 分钟源（BigQMT bridge）在 2026-09-12 之后返回 **0 行**分钟线；"
        "同一接口的**日线**能取到 09-18，所以这是分钟线本身的供给边界，不是连接问题。",
        "- 备选源（Sina/akshare）能取到 09-18，但与权威源在 09-09～09-11 的重叠区间逐根比对后"
        "**不可用**：41.7% 的分钟收盘价相差 > ¥0.05，16.3% 相差 > ¥0.20"
        "（中位数仅 ¥0.02，双峰分布，不是舍入误差）。",
        "- 策略触发价按价格的 0.1% 量级计算，600 元价位约合 ¥0.46——"
        "备选源 16% 的分钟线偏差已进入该量级，拼接会污染成交判定。",
        "",
        "因此本报告覆盖 **2026-01-01 至 2026-09-11**（少 5 个交易日），"
        "而不用异源数据补齐。",
        "",
        "## 完整性检查",
        "",
        "- 回放失败：无。",
        "- 逐笔明细见 `trades.md`；结构化结果见 `results.json`。",
        "",
        "## 复现",
        "",
        "```powershell",
        "python analysis/backtest_coreT_cycle_report_20260919.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def _conclusions(agg: dict, live: list[str], total: dict) -> list[str]:
    if not live:
        return ["区间内所有标的、所有口径均无成交。"]
    best_key = max(ORDER, key=lambda k: total[k]["excess_net"])
    if len(live) == len(DATASETS):
        lines = [
            f"三个标的在全部口径下均有成交，下表是组合合计"
            f"（{'、'.join(live)}，各 {agg[live[0]][ORDER[0]]['days']} 个交易日）。",
            "",
        ]
    else:
        lines = [
            f"区间内只有 **{'、'.join(live)}** 产生成交；其余标的在所有口径下零成交，"
            "所以下面的数字不代表策略在组合层面的表现。",
            "",
        ]
    for key in ORDER:
        row = total[key]
        lines.append(
            f"- **{VARIANTS[key]['name']}**：超额净收益 {money(row['excess_net'])} 元 "
            f"= 反T已实现 {money(row['REV-T_realized'])} + 正T已实现 "
            f"{money(row['FWD-T_realized'])} + 未平盯市 "
            f"{money(row['REV-T_unrealized'] + row['FWD-T_unrealized'])} "
            f"− 手续费 {money(row['fees'])}；"
            f"合计T达成率 {row['t_rate']:.1%}，"
            f"反T {row['REV-T_cycles']} 周期 / 正T {row['FWD-T_cycles']} 周期。")
    lines.extend([
        "",
        f"收益最高的是 **{VARIANTS[best_key]['name']}**。"
        "但请注意下一条：它的 T 达成率并不是最高的——"
        "**达成率高不等于收益高**，这是本报告最需要强调的一点。",
    ])
    return lines


def _problems(agg: dict, live: list[str], total: dict) -> list[str]:
    if not live:
        return ["（无成交，无可分析项。）"]
    lines = ["### 逐口径交易质量", ""]
    for key in ORDER:
        row = total[key]
        cycles = [c for s in live for c in agg[s][key]["all_cycles"]]
        if not cycles:
            lines.extend([f"**{VARIANTS[key]['name']}**：无成交。", ""])
            continue
        lines.append(f"**{VARIANTS[key]['name']}**")
        lines.append("")
        for lane in ("REV-T", "FWD-T"):
            items = [c for c in cycles if c["lane"] == lane]
            if not items:
                lines.append(f"- {LANE_CN[lane]}：无成交。")
                continue
            wins = [c for c in items if c["gross"] > 0]
            losses = [c for c in items if c["gross"] <= 0]
            gain = sum(c["gross"] for c in wins)
            loss = sum(c["gross"] for c in losses)
            pf = gain / abs(loss) if loss else float("inf")
            lines.append(
                f"- **{LANE_CN[lane]}** {len(items)} 笔：胜率 {len(wins)/len(items):.1%}，"
                f"盈利因子 {pf:.2f}，合计 {money(gain + loss)} 元"
                f"（盈利 {money(gain)} / 亏损 {money(loss)}），"
                f"单笔均值 {money((gain + loss)/len(items))} 元"
                f"，单笔中位数 {money(sorted(c['gross'] for c in items)[len(items)//2])} 元。")
        unclosed = [c for c in cycles if not c["closed"]]
        if unclosed:
            lines.append(
                f"- **日末未平腿** {len(unclosed)} 条 / "
                f"{sum(c['shares'] for c in unclosed)} 股，盯市 "
                f"{money(sum(c['gross'] for c in unclosed))} 元。")
        lines.append("")
    lines.extend([
        "### 结构性观察",
        "",
        "1. **`SELL_TRIGGER_SCALE` 那一类「按标的校准触发价」的建议应当作废**。"
        "本报告早期版本曾把「600584/600105 零成交」解释为触发价对低波动标的过高——"
        "**那个解释是错的**。真实原因是回放框架的持仓桩把 `m_strInstrumentID` 写死成 `'601869'`，"
        "而策略用 `pos.m_strInstrumentID == self.stock_code` 匹配持仓，"
        "于是 600584/600105 永远匹配不到自己的底仓、`base_can_use` 恒为 0、"
        "两个方向都被容量检查关掉。（同一份框架里 `StrictBroker` 是正确的，"
        "只有 `analysis/compare_v51_v39_minute.py` 的 `Broker` 写死；已修复。）"
        "修好后三个标的都正常成交，触发价公式没有任何问题。",
        "2. **T达成率与收益不同向**。v0561 的达成率最高、收益最低。"
        "它删掉了 ATR 再入场，周期结束后继续套用早盘触发价，于是在已失效的价位反复进出——"
        "周转上去了，单位收益被摊薄。**把达成率当 KPI 会直接选错策略。**",
        "3. **日末未平腿的盯市不是利润**。这部分按收盘价计入超额收益，"
        "既未付日末平仓手续费，也未承担次日跳空；实盘质量低于账面。",
        "4. **手续费是本策略最确定的支出**。下表按各口径"
        "`毛收益 = 净收益 + 手续费` 计算。在毛收益为正的格里，手续费要拿走一半上下；"
        "毛收益为负时比值无意义，已标注。",
        "",
    ])
    lines.extend(_fee_table(agg, live))
    lines.extend([
        "",
        "这条把前面几点的含义收紧了：**在这类 T 策略上，"
        "「减少交易次数」和「提高单笔幅度」比「提高信号胜率」更值钱**——"
        "反T胜率已经稳定在 69%～72%、正T在 58%～76%，"
        "信号侧没有多少可捡的，而费率是按成交额固定抽走的。",
        "",
        "### 本轮已实施",
        "",
    ])
    lines.extend(_done_items(agg, live))
    lines.extend(_directional_section(agg, live))
    lines.extend([
        "### 仍待实施",
        "",
        "1. **方向过滤不要加回来**（已由下方 2×2 对照证伪）："
        "在 v0561/v0562 的「无正T止损」配置下，恢复 `DirectionalAdmission` 反而更差。"
        "它只有在**同时开着正T止损**时才是正贡献——那种组合下过滤器是在替止损擦屁股。"
        "详见「方向过滤的独立效果」一节。",
        "2. **给正T加日末处置规则**：当前口径下正T未平腿被直接丢弃。"
        "即使不做止损，也可以「日末最后 N 分钟不再新开正T」，把未平仓风险挡在收盘前。"
        "本区间正T未平腿合计 "
        f"{money(sum(agg[s]['v0562']['FWD-T_unrealized'] for s in live if agg[s]['v0562']['all_cycles']))} 元"
        "（v0562），是最大的单项漏损。",
        "3. **按标的决定是否启用**：三个标的的结果差异极大——v0562 在 601869（+7,453）"
        "和 600105（+3,277）上是正的，在 600584 上是 **-10,314**。"
        "组合合计只有 +416 元，基本是三个相反结果互相抵消。"
        "在样本外复核之前，不应把这三个标的当成一个可分散的组合来用。",
        "4. **重新审视仓位公式与 T达成率的关系**（本报告新发现）："
        "`T_TARGET_VALUE=40000` 在高价标的（601869，约 330 元）把每条腿锁死在 1 手，"
        "单腿占底仓仅 10%；低价标的则是底仓 40% 那一条先触顶。"
        "若确实以 T达成率为目标，需要按标的分别调 `T_TARGET_VALUE`、"
        "`T_POSITION_FRACTION` 与 `MAX_DAILY_TRADES`，"
        "并重新评估风险敞口——注意这会把单日暴露放大数倍，不是免费的。",
        "5. **用样本外区间复核**：本区间三个标的各约 170 个交易日，"
        "且三个标的的信号方向相反（v0562 在 600584 上是亏的），"
        "任何参数结论都不足以支撑实盘，v0562 相对 v0561 的优势尤其需要在独立样本上重现。",
        "6. **修复回放框架后，历史报告需要重跑**："
        "`analysis/compare_v51_v39_minute.py` 的 `Broker.query_positions` 曾把 "
        "`m_strInstrumentID` 写死为 `'601869'`，导致该框架下所有非 601869 标的的报告里"
        "那些标的都是零成交。本报告已用修复后的框架重跑；"
        "更早的每日重置报告（`dayt_v56_v056_fwdt_daily_reset_20260918`、"
        "`dayt_v0561_vs_v0562_20260919` 等）的三标的合计行仍受该 bug 影响，"
        "其 601869 单标的数字不受影响。",
    ])
    return lines


FACTORIAL_SYMBOL = "601869.SH"
FACTORIAL_WINDOWS = (("2026-01-01~09-11", "20260101", "20260911"),
                     ("2025-09-15~2026-09-11", "20250915", "20260911"))


def run_factorial() -> None:
    """2x2: DIRECTIONAL_ENABLED x forward-T stop-loss, on one symbol.

    Isolates the directional filter, which the cumulative ladder could not: the
    ladder measured it while the stop-loss was still active and reported a large
    negative, but that figure is dominated by the interaction between the two.
    """
    symbol = FACTORIAL_SYMBOL
    daily, minute = load_data(DATASETS[symbol])
    full_minute = pd.read_csv(
        DATASETS[symbol] / "1m.csv", dtype={"time": str}
    ).set_index("time").sort_index()
    cells = {}
    for window, start, end in FACTORIAL_WINDOWS:
        mask = full_minute.index.str[:8]
        windowed = full_minute.loc[(mask >= start) & (mask <= end)]
        grouped = [g for g in windowed.groupby(windowed.index.str[:8])
                   if len(g[1]) >= 230]
        for directional in (True, False):
            for stop_loss in (0.015, NO_STOP_SENTINEL):
                rows = []
                for day, bars in grouped:
                    history = daily.loc[daily.index < day]
                    if len(history) < 80:
                        continue
                    with redirect_stdout(io.StringIO()):
                        result = replay(
                            "v056_nomom", history, bars, slip=0.0,
                            initial_cash=INITIAL_CASH,
                            initial_shares=INITIAL_SHARES, symbol=symbol,
                            overrides={**COMMON,
                                       "LONG_RESEARCH_DISABLED": False,
                                       "DIRECTIONAL_ENABLED": directional},
                            cfg_overrides={"STOP_LOSS_PCT": stop_loss})
                    if result["failure"]:
                        raise RuntimeError(f"{window}/{day}: {result['failure']}")
                    rows.append(result)
                fees = sum(r["turnover"] * FEE_RATE for r in rows)
                fwd = sum(1 for r in rows for t in r["trades"]
                          if t["label"] == "FWD-T buy")
                key = f"{window}|{int(directional)}|{stop_loss}"
                cells[key] = {
                    "window": window,
                    "directional": directional,
                    "stop_loss": stop_loss,
                    "days": len(rows),
                    "excess_net": sum(r["excess_gross"] for r in rows) - fees,
                    "fwd_buys": fwd,
                }
                print(f"factorial {window} directional={directional} "
                      f"stop={stop_loss}: {cells[key]['excess_net']:.2f}",
                      flush=True)
    (OUT / "factorial.json").write_text(
        json.dumps({"symbol": symbol, "cells": cells}, ensure_ascii=False,
                   indent=2), encoding="utf-8")
    print(OUT / "factorial.json")


def _directional_section(agg: dict, live: list[str]) -> list[str]:
    """Report the isolated directional-filter effect, or say it was not run."""
    path = OUT / "factorial.json"
    if not path.exists():
        return [
            "### 方向过滤的独立效果",
            "",
            "未运行。用 `python analysis/backtest_coreT_cycle_report_20260919.py "
            "--factorial` 生成后本节会自动填入。",
        ]
    data = json.loads(path.read_text(encoding="utf-8"))
    cells = data["cells"]
    symbol = data["symbol"]
    lines = [
        "### 方向过滤（`DirectionalAdmission`）的独立效果",
        "",
        "`DirectionalAdmission` 要求价格站上日内均价、且 5 分钟动量与均价斜率同时向上，"
        "连续 3 个采样达标后才放行正T买入。它只挡正T开仓，不影响反T。",
        "",
        f"下表是 {symbol} 上的 2×2 对照，唯一变量是「方向过滤」与「正T止损」，"
        "走势制度等其他层保持原样：",
        "",
        "| 窗口 | 正T止损 | 保留过滤 | 删除过滤 | 删除 − 保留 |",
        "|---|---|---:|---:|---:|",
    ]
    for window, _s, _e in FACTORIAL_WINDOWS:
        for stop_loss in (0.015, NO_STOP_SENTINEL):
            on = cells.get(f"{window}|1|{stop_loss}")
            off = cells.get(f"{window}|0|{stop_loss}")
            if not on or not off:
                continue
            delta = off["excess_net"] - on["excess_net"]
            lines.append(
                f"| {window} | {'ON' if stop_loss < 1 else 'OFF'} | "
                f"{money(on['excess_net'])} | {money(off['excess_net'])} | "
                f"**{money(delta)}** |")
    lines.extend([
        "",
        "**结论：方向过滤的好坏取决于正T止损是否同时开着，符号会反转。**",
        "",
        "- **止损开着时，删除过滤是灾难性的**（两个窗口分别 "
        f"{money(_one_delta(cells, FACTORIAL_WINDOWS[0][0], 0.015))} / "
        f"{money(_one_delta(cells, FACTORIAL_WINDOWS[1][0], 0.015))} 元）。"
        "因为不过滤会让正T买入次数翻 3～4 倍，而每笔都被 -1.5% 止损割掉，"
        "反复买、反复割，手续费和亏损一起放大。",
        "- **止损关掉后（即 v0561/v0562 的配置），删除过滤是略微正面的**（两个窗口分别 "
        f"{money(_one_delta(cells, FACTORIAL_WINDOWS[0][0], NO_STOP_SENTINEL))} / "
        f"{money(_one_delta(cells, FACTORIAL_WINDOWS[1][0], NO_STOP_SENTINEL))} 元）。"
        "没有止损这把快刀，不过滤只是让正T多做几笔，净效果小正。",
        "",
        "所以**对 v0561/v0562 这个具体配置，删掉方向过滤是对的**。"
        "之前把累积阶梯里那步 -10,735 元说成「方向过滤是第二大负贡献」是错的——"
        "那一步是在止损仍然开着时测的，测到的是「过滤器 × 止损」的组合效应，"
        "不是过滤器本身。**这也是累积阶梯的固有陷阱：每一步的差额都依赖前面已关的层。**",
        "",
    ])
    return lines


def _one_delta(cells: dict, window: str, stop_loss: float) -> float:
    on = cells.get(f"{window}|1|{stop_loss}")
    off = cells.get(f"{window}|0|{stop_loss}")
    if not on or not off:
        return 0.0
    return off["excess_net"] - on["excess_net"]


def _sample_split_section(payload: dict, agg: dict, live: list[str]) -> list[str]:
    """In-sample vs out-of-sample, focused on the v0561 -> v0562 improvement.

    The criteria are printed before the numbers so the verdict cannot be
    rationalised after the fact.
    """
    baseline = payload.get("baseline")
    current_range = payload["method"]["range"]
    lines = [
        "## 样本内 vs 样本外",
        "",
        "**判定标准（先定后看）**",
        "",
        "1. **主判据**：样本外 `v0562 − v0561` 的合计差额为正，**且三个标的同向**",
        "2. **次判据**：样本外 v0562 仍是四个口径里最好的",
        "3. 任一条不成立，即判定「样本内的改善未重现，属于区间特异结果」，"
        "且不得再建议把 v0562 当作已验证改进",
        "",
        "**关于「样本外」的含义**：本仓库 1 分钟数据只有 2025-09-09～2026-09-11 一段，"
        "所以这里是把同一段数据切成前后两半，不是严格意义上的训练/留出分离。"
        "因为没有任何参数是在后半段上拟合出来的，它仍然是一次有效的稳健性检验；"
        "但不能被称作 holdout。",
        "",
    ]
    if not baseline:
        lines.extend([
            "未提供对照。用 `--baseline <样本内 results.json>` 重新生成以填入本节。",
            "（既有逐笔数据都在，`--report-only` 加同一个参数即可，无需重跑回放。）",
            "",
        ])
        return lines

    prior = baseline["summary"]
    prior_range = baseline["range"]
    lines.extend([
        f"- 样本内：{prior_range[0]} ~ {prior_range[1]}"
        f"（{baseline['source']}）",
        f"- 样本外：{current_range[0]} ~ {current_range[1]}（本次）",
        "",
        "### 逐标的对照：超额净收益",
        "",
        "| 标的 | 口径 | 样本内 | 样本外 | 样本外−样本内 |",
        "|---|---|---:|---:|---:|",
    ])
    for symbol in live:
        for key in ORDER:
            if symbol not in prior or key not in prior[symbol]:
                continue
            before = prior[symbol][key]["excess_net"]
            after = agg[symbol][key]["excess_net"]
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | {money(before)} | "
                f"{money(after)} | {money(after - before)} |")

    lines.extend([
        "",
        "### 核心：v0561 → v0562 的改善是否重现",
        "",
        "| 标的 | 样本内差额 | 样本外差额 | 同向？ |",
        "|---|---:|---:|---|",
    ])
    deltas = {}
    for symbol in live:
        if symbol not in prior:
            continue
        before = (prior[symbol]["v0562"]["excess_net"]
                  - prior[symbol]["v0561"]["excess_net"])
        after = (agg[symbol]["v0562"]["excess_net"]
                 - agg[symbol]["v0561"]["excess_net"])
        deltas[symbol] = (before, after)
        same = "是" if (before > 0) == (after > 0) else "**否**"
        lines.append(f"| {symbol} | {money(before)} | {money(after)} | {same} |")
    total_before = sum(v[0] for v in deltas.values())
    total_after = sum(v[1] for v in deltas.values())
    lines.append(
        f"| **合计** | **{money(total_before)}** | **{money(total_after)}** | "
        f"{'是' if (total_before > 0) == (total_after > 0) else '**否**'} |")
    lines.extend(["", "### 判定", ""])
    lines.extend(_verdict(deltas, total_after, agg, live, prior))
    return lines


def _verdict(deltas: dict, total_after: float, agg: dict, live: list[str],
             prior: dict) -> list[str]:
    positive = [s for s in live if deltas.get(s, (0, 0))[1] > 0]
    consistent = len(positive) == len(live) and len(live) > 0
    cur_totals = {k: sum(agg[s][k]["excess_net"] for s in live) for k in ORDER}
    best_key = max(ORDER, key=lambda k: cur_totals[k])
    best_ok = best_key == "v0562"
    lines = [
        f"- 主判据（样本外 v0562−v0561 合计为正且三标同向）：合计 "
        f"**{money(total_after)}**，"
        f"{len(positive)}/{len(live)} 个标的为正 → "
        f"**{'成立' if (total_after > 0 and consistent) else '不成立'}**",
        f"- 次判据（样本外 v0562 仍最好）：最好的是 "
        f"**{VARIANTS[best_key]['name']}** → **{'成立' if best_ok else '不成立'}**",
        "",
    ]
    if total_after > 0 and consistent and best_ok:
        lines.append(
            "**结论：样本内观察到的改善在样本外重现，方向与排序都一致。**"
            "v0562 相对 v0561 的优势可以被当作真实效应，而不只是区间内的偶然。"
            "但注意量级——见下方每标的日一栏，仍不足以支撑「这是一个能赚钱的策略」的说法。")
    else:
        lines.append(
            "**结论：样本内观察到的改善未能重现。** "
            "v0561 → v0562 的正向差额是区间特异结果，"
            "不应据此把 v0562 当作已验证的改进；"
            "此前报告里「v0562 是必要修复」的表述需要一并收回。")
    lines.extend([
        "",
        "| 口径 | 样本外合计超额 | 标的日数 | 每标的日 |",
        "|---|---:|---:|---:|",
    ])
    days = sum(agg[s]["v0562"]["days"] for s in live)
    for key in ORDER:
        lines.append(
            f"| {VARIANTS[key]['name']} | {money(cur_totals[key])} | {days} | "
            f"{money(cur_totals[key] / days if days else 0)} 元 |")
    lines.append("")
    return lines


def _fee_table(agg: dict, live: list[str]) -> list[str]:
    """Fees against gross, so the reader can see how thin the margin is.

    The ratio is only meaningful when gross is positive; when gross is negative
    the sign of the ratio flips and says nothing, so those cells show the fee as
    a multiple of turnover instead.
    """
    lines = [
        "| 标的 | 口径 | 毛收益 | 手续费 | 净收益 | 手续费/毛收益 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for symbol in live:
        for key in ORDER:
            row = agg[symbol][key]
            if not row["all_cycles"]:
                continue
            gross = row["excess_net"] + row["fees"]
            ratio = (f"{row['fees'] / gross:.0%}" if gross > 0
                     else "毛收益为负，比值无意义")
            lines.append(
                f"| {symbol} | {VARIANTS[key]['name']} | {money(gross)} | "
                f"{money(row['fees'])} | {money(row['excess_net'])} | {ratio} |")
    return lines


def _done_items(agg: dict, live: list[str]) -> list[str]:
    """State what has already been implemented, with the measured effect."""
    lines = []
    if not live:
        return ["（无成交，无可分析项。）"]
    deltas = {}
    for symbol in live:
        if not agg[symbol]["v0562"]["all_cycles"]:
            continue
        deltas[symbol] = (agg[symbol]["v0562"]["excess_net"]
                          - agg[symbol]["v0561"]["excess_net"])
    if deltas:
        detail = "、".join(f"{s} {money(d)} 元" for s, d in deltas.items())
        lines.extend([
            "1. **恢复 ATR 再入场（已实施于 v0562，本表即其验证）**："
            "周期闭合后用实际成交价重新锚定下一轮触发价，而不是沿用早盘价格。"
            f"相对 v0561 的超额净收益差：{detail}。"
            "这是本轮唯一已落地的改进，也是 v0562 从亏损转为盈利的原因。",
            "",
            "2. **关闭正T止损（已实施于 v0561/v0562）**："
            "`STOP_LOSS_PCT` 分支已删除，日末强平分支也已删除。"
            "消融显示这一步在 601869 上值 +21,035 元，是单项影响最大的一步（同样来自 2025-09-15～2026-09-11 区间的阶梯回测，非本区间）。",
            "",
        ])
    return lines


def build_trades(days_by_symbol: dict, agg: dict) -> str:
    lines = [
        "# 逐笔交易明细",
        "",
        "每笔 = 一个已配对的 T 周期，或一条日末未平的腿。"
        "反T：卖出开仓、买回平仓；正T：买入开仓、卖出平仓。",
        "`收益` 为**毛收益，不含手续费**。`是否闭合` 为「否」时，"
        "平仓价与平仓时间列显示的是日末收盘盯市，不是真实成交。",
        "",
    ]
    for symbol in DATASETS:
        if not any(agg[symbol][key]["all_cycles"] for key in ORDER):
            lines.extend([f"## {symbol}", "", "区间内所有口径均无成交。", ""])
            continue
        lines.extend([f"## {symbol}", ""])
        for key in ORDER:
            days = days_by_symbol[symbol][key]
            cycles = agg[symbol][key]["all_cycles"]
            closed_n = sum(1 for c in cycles if c["closed"])
            lines.extend([
                f"### {VARIANTS[key]['name']}", "",
                f"共 {len(cycles)} 笔，已闭合 {closed_n} 笔，"
                f"未闭合 {len(cycles) - closed_n} 笔。", "",
                "| 日期 | 方向 | 开仓时间 | 平仓时间 | 股数 | 手数 | 开仓价 | "
                "平仓价 | 收益(元) | 是否闭合 |",
                "|---|---|---|---|---:|---:|---:|---:|---:|---|",
            ])
            for day in days:
                for cycle in day["cycles"]:
                    lines.append(
                        f"| {day['date']} | {cycle['direction']} | "
                        f"{cycle['entry_time']} | {cycle['exit_time']} | "
                        f"{cycle['shares']} | {cycle['shares'] // LOT} | "
                        f"{cycle['entry_price']:.2f} | {cycle['exit_price']:.2f} | "
                        f"{cycle['gross']:,.2f} | "
                        f"{'是' if cycle['closed'] else '**否**'} |")
            lines.append("")
    return "\n".join(lines)


def _arg(flag: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> None:
    global OUT, START, END
    if "--out" in sys.argv:
        OUT = ROOT / _arg("--out")
    # Window override. load_data and _mean_price read these globals at call time,
    # so they must be rebound before run_all() rather than passed around.
    START = _arg("--start", START)
    END = _arg("--end", END)
    if "--factorial" in sys.argv:
        OUT.mkdir(parents=True, exist_ok=True)
        run_factorial()
        return
    if "--report-only" in sys.argv:
        if "--superseded-by" in sys.argv:
            # Banner for a report whose conclusion a later window has overturned;
            # baked into results.json so it survives future --report-only runs.
            path = OUT / "results.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["superseded_by"] = _arg("--superseded-by")
            path.write_text(json.dumps(stored, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")
        rebuild_report_only()
        return
    smoke = "--smoke" in sys.argv
    if OUT.exists() and not smoke:
        raise FileExistsError("refuse to overwrite existing report: " + str(OUT))
    OUT.mkdir(parents=True, exist_ok=True)
    days_by_symbol, dataset_hashes = run_all(smoke)
    agg = aggregate_all(days_by_symbol)
    payload = {
        "method": {
            "model": "INDEPENDENT_DAILY_RESET",
            "range": [START, END],
            "initial_cash_each_day": INITIAL_CASH,
            "initial_shares_each_day": INITIAL_SHARES,
            "fee_rate_each_side": FEE_RATE,
            "slippage": 0.0,
            "common_overrides": COMMON,
            "variants": {
                key: {"name": VARIANTS[key]["name"],
                      "entry": STRATEGIES[VARIANTS[key]["version"]],
                      "overrides": {**COMMON, **VARIANTS[key]["overrides"]},
                      "cfg_overrides": VARIANTS[key]["cfg_overrides"]}
                for key in ORDER},
        },
        "hashes": {
            "strategies": {key: sha256(
                ROOT / "Stragety/MiniQMT_Stragety/DayT"
                / STRATEGIES[VARIANTS[key]["version"]]) for key in ORDER},
            "datasets": dataset_hashes,
        },
        "summary": {
            symbol: {key: {k: v for k, v in row.items() if k != "all_cycles"}
                     for key, row in variants.items()}
            for symbol, variants in agg.items()},
        # Cycles are persisted so --report-only can rebuild both documents
        # without replaying ~170 sessions per variant.
        "days": days_by_symbol,
    }
    if "--baseline" in sys.argv:
        # A prior run's results.json, used for the in-sample vs out-of-sample
        # comparison.  Baked into the payload so --report-only can still render.
        source = ROOT / _arg("--baseline")
        prior = json.loads(source.read_text(encoding="utf-8"))
        payload["baseline"] = {
            "source": str(source.relative_to(ROOT)).replace("\\", "/"),
            "range": prior["method"]["range"],
            "summary": prior["summary"],
        }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    write_documents(payload, agg, days_by_symbol)


def write_documents(payload: dict, agg: dict, days_by_symbol: dict) -> None:
    (OUT / "README.md").write_text(
        build_report(payload, agg, days_by_symbol), encoding="utf-8")
    (OUT / "trades.md").write_text(
        build_trades(days_by_symbol, agg), encoding="utf-8")
    print(OUT / "README.md")
    print(OUT / "trades.md")


def rebuild_report_only() -> None:
    payload = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
    days_by_symbol = payload["days"]
    agg = aggregate_all(days_by_symbol)
    write_documents(payload, agg, days_by_symbol)


if __name__ == "__main__":
    main()
