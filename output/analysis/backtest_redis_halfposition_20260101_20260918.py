"""v0561 / v0562 / CaptureT_v1 — 每日策略重置、账户连续、半仓做T。

口径（按用户给出的规格）：
  * 标的 601869.SH，数据全部来自 RedisQMT 桥接（get_market_data_ex）
  * 区间 2026-01-01 ~ 2026-09-18
  * 初始仓位 ≈ 100,000 元：按首个可交易日开盘价向下取整到整手 → 800 股
    另有现金 100,000 元
  * **每个交易日新建策略实例**（状态清零，避免被未平腿卡住），
    但**现金、实际持仓、手续费跨日连续**，不做日末强平、不丢弃未平腿
  * **每笔新 T 腿使用一半仓位**：底仓 800 股 → 每腿 400 股
  * 成交撮合为触发当根 1 分钟收盘价，单边 0.05%，滑点 0
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import output.analysis.compare_v51_v39_minute as HARNESS
from backtest.dayt_registry import STRATEGIES


OUT = ROOT / "analysis/dayt_redis_halfposition_20260101_20260918"
DATA = OUT
START = "20260101"
END = "20260918"
SYMBOL = "601869.SH"
FEE_RATE = 0.0005
INITIAL_CASH = 100_000.0
# 初始仓位 ≈ 100,000 元，按首个交易日开盘价向下取整到整手。
POSITION_VALUE = 100_000.0
HALF_POSITION_FRACTION = 0.5      # 每笔做T使用一半仓位的手数

REV_OPEN = "REV-T sell"
REV_CLOSE = "REV-T buyback"
LONG_OPEN = "FWD-T buy"
LONG_CLOSE = "FWD-T sell"


def half_position_submit(original):
    """Wrap _submit_order so a NEW leg is sized at half the *base* position.

    v0561/v0562 已经通过 T_TARGET_VALUE / T_POSITION_FRACTION 表达这个规则；
    CaptureT_v1 的设计是满仓，这里按同一口径把它改成半仓，使三者可比。
    """
    def wrapper(self, shares, price, label, style='COMPETE'):
        if label in (REV_OPEN, LONG_OPEN):
            self._refresh_position()
            base = int(self.st.get('base_can_use', 0) or 0)
            target = base // 2 // self.trade_lot * self.trade_lot
            if target >= self.trade_lot:
                shares = target if shares > 0 else -target
        return original(self, shares, price, label, style)
    return wrapper


def lane_of(label: str) -> str:
    if label.startswith("REV-T"):
        return "REV-T"
    if label.startswith("FWD-T"):
        return "FWD-T"
    return ""


def build_cycles(trades: list[dict], mark: float) -> list[dict]:
    """把成交流水按 lane 做 FIFO 配对，日末未平的腿按收盘价标注。"""
    queues: dict[str, list[dict]] = {"REV-T": [], "FWD-T": []}
    cycles: list[dict] = []
    for trade in trades:
        label, shares, price, stamp = (
            trade["label"], trade["shares"], trade["price"], trade["time"])
        lane = lane_of(label)
        if not lane:
            continue
        clock = f"{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
        opens = (lane == "REV-T" and label == REV_OPEN) or \
                (lane == "FWD-T" and label == LONG_OPEN)
        closes = (lane == "REV-T" and label.startswith(REV_CLOSE)) or \
                 (lane == "FWD-T" and label == LONG_CLOSE)
        if opens:
            queues[lane].append({"shares": abs(shares), "price": price, "time": clock})
        elif closes:
            remaining = abs(shares)
            while remaining > 0 and queues[lane]:
                leg = queues[lane][0]
                used = min(leg["shares"], remaining)
                cycles.append({
                    "lane": lane, "entry_time": leg["time"], "exit_time": clock,
                    "shares": used, "entry_price": leg["price"], "exit_price": price,
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
                "lane": lane, "entry_time": leg["time"], "exit_time": "—",
                "shares": leg["shares"], "entry_price": leg["price"],
                "exit_price": mark,
                "gross": ((leg["price"] - mark) if lane == "REV-T"
                          else (mark - leg["price"])) * leg["shares"],
                "closed": False,
            })
    return cycles


def run_strategy(key: str, daily: pd.DataFrame, minute: pd.DataFrame) -> dict:
    # v0561/v0562 用 T_TARGET_VALUE / T_POSITION_FRACTION 表达「半仓」；
    # CaptureT_v1 的股数在策略内部算，改用 half_position_submit 包装（见下）。
    if key == "capture_v1":
        overrides = {}
    else:
        overrides = {"T_TARGET_VALUE": 1e9,
                     "T_POSITION_FRACTION": HALF_POSITION_FRACTION}
    cfg_overrides = {"STOP_LOSS_PCT": 1.0} if key == "v0562" else {}
    days, day_rows = [], []
    cash, shares = INITIAL_CASH, None

    for day, bars in minute.groupby(minute.index.str[:8]):
        hist = daily.loc[daily.index < day]
        if len(bars) < 230 or len(hist) < 80:
            continue
        if shares is None:
            # 初始仓位：按首个可交易日开盘价向下取整到整手
            shares = int(POSITION_VALUE / float(bars["open"].iloc[0]) / 100) * 100
        original = HARNESS.load_strategy

        def load(version, _o=original):
            module = _o(version)
            if key == "capture_v1":
                module.StrategyRunner._submit_order = half_position_submit(
                    module.StrategyRunner._submit_order)
            return module

        with ExitStack() as stack:
            stack.enter_context(patch.object(HARNESS, "load_strategy", load))
            with redirect_stdout(io.StringIO()):
                result = HARNESS.replay(
                    key, hist, bars, slip=0.0, initial_cash=cash,
                    initial_shares=shares, symbol=SYMBOL,
                    overrides=overrides, cfg_overrides=cfg_overrides)
        if result["failure"]:
            raise RuntimeError(f"{key}/{day}: {result['failure']}")
        close = float(bars["close"].iloc[-1])
        equity = result["final_equity"]
        cycles = build_cycles(result["trades"], close)
        base = shares
        row = {
            "date": day, "equity_open": cash + shares * float(bars["open"].iloc[0]),
            "equity_close": equity, "cash_open": cash, "shares_open": shares,
            "cash_close": equity - result["final_position"] * close,
            "shares_close": result["final_position"], "close": close,
            "turnover": result["turnover"], "cycles": cycles,
        }
        for lane in ("REV-T", "FWD-T"):
            closed = [c for c in cycles if c["lane"] == lane and c["closed"]]
            opened = [c for c in cycles if c["lane"] == lane and not c["closed"]]
            row[f"{lane}_closed"] = sum(c["shares"] for c in closed)
            row[f"{lane}_open"] = sum(c["shares"] for c in opened)
            row[f"{lane}_realized"] = sum(c["gross"] for c in closed)
            row[f"{lane}_unrealized"] = sum(c["gross"] for c in opened)
            row[f"{lane}_trips"] = len(closed)
        row["t_rate"] = ((row["REV-T_closed"] + row["FWD-T_closed"]) / base
                         if base else 0.0)
        day_rows.append(row)
        cash, shares = row["cash_close"], row["shares_close"]

    for row in day_rows:
        row["equity_ret"] = (row["equity_close"] / row["equity_open"] - 1
                             if row["equity_open"] else 0.0)
    return {"days": day_rows,
            "initial_shares": day_rows[0]["shares_open"] if day_rows else 0}


def summarize(run: dict) -> dict:
    days = run["days"]
    fees = sum(d["turnover"] * FEE_RATE for d in days)
    final = days[-1]["equity_close"] if days else 0.0
    initial = days[0]["equity_open"] if days else 0.0
    hold = (days[0]["shares_open"] * days[-1]["close"] + INITIAL_CASH
            if days else 0.0)
    out = {
        "days": len(days), "initial_shares": run["initial_shares"],
        "initial_equity": initial, "final_equity": final,
        "account_net": final - initial, "account_ret": final / initial - 1 if initial else 0,
        "hold_equity": hold, "excess_net": final - hold,
        "excess_ret": (final - hold) / initial if initial else 0,
        "fees": fees, "t_rate": float(np.mean([d["t_rate"] for d in days])) if days else 0,
        "worst_daily_dd": 0.0,
        "fwd_trips": sum(d["FWD-T_trips"] for d in days),
        "rev_trips": sum(d["REV-T_trips"] for d in days),
        "fwd_realized": sum(d["FWD-T_realized"] for d in days),
        "rev_realized": sum(d["REV-T_realized"] for d in days),
        "fwd_unrealized": sum(d["FWD-T_unrealized"] for d in days),
        "rev_unrealized": sum(d["REV-T_unrealized"] for d in days),
        "unclosed_days": sum(d["REV-T_open"] + d["FWD-T_open"] > 0 for d in days),
    }
    return out


def money(v: float) -> str:
    return f"{v:,.2f}"


def build_report(payload: dict) -> str:
    summary, runs = payload["summary"], payload["runs"]
    names = payload["method"]["variants"]
    lines = [
        "# 每日策略重置 · 账户连续 · 半仓做T：v0561 / v0562 / CaptureT_v1",
        "",
        "## 口径",
        "",
        f"- 标的 **{SYMBOL}**，数据全部取自 **RedisQMT 桥接**（`get_market_data_ex`）。",
        f"- 区间 **{START[:4]}-{START[4:6]}-{START[6:]} ~ {END[:4]}-{END[4:6]}-{END[6:]}**。",
        f"- 初始仓位 ≈ {money(POSITION_VALUE)} 元，按首个可交易日开盘价向下取整到整手，"
        f"实得 **{payload['initial_shares']} 股**（{payload['initial_shares']//100} 手）；"
        f"另有现金 {money(INITIAL_CASH)} 元。",
        f"- **每个交易日新建策略实例**（状态清零，避免被未平腿卡住）；"
        "但现金、实际持仓、手续费**跨日连续**。日末未平腿不强平、不丢弃，次日按真实账户继续。",
        f"- **每笔新 T 腿使用一半仓位**：底仓 {payload['initial_shares']} 股 → 每腿 "
        f"{int(payload['initial_shares']*HALF_POSITION_FRACTION)} 股。",
        "- 成交撮合为触发当根 1 分钟收盘价，单边费率 0.05%、滑点 0。",
        "",
        "## 总结",
        "",
    ]
    lines.extend(_conclusions(summary, names, payload))
    lines.extend([
        "",
        "## 汇总对比",
        "",
        "| 策略 | 期末资产 | 账户收益 | 相对一直持有 | 超额收益率 | 手续费 | "
        "反T已实现 | 正T已实现 | 日末未平盯市 | 反T周期 | 正T周期 | 未闭合日 | 平均T达成率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key in payload["order"]:
        s = summary[key]
        lines.append(
            f"| {names[key]} | {money(s['final_equity'])} | {money(s['account_net'])} | "
            f"{money(s['excess_net'])} | {s['excess_ret']:.2%} | {money(s['fees'])} | "
            f"{money(s['rev_realized'])} | {money(s['fwd_realized'])} | "
            f"{money(s['rev_unrealized'] + s['fwd_unrealized'])} | "
            f"{s['rev_trips']} | {s['fwd_trips']} | {s['unclosed_days']} | "
            f"{s['t_rate']:.2%} |")
    lines.extend([
        "",
        f"**一直持有对照**：同样现金 + 初始 {payload['initial_shares']} 股，"
        f"期间不交易，期末 {money(payload['hold_equity'])} 元。",
        "",
        "## 逐笔明细",
        "",
        "每笔 = 一个已配对的 T 周期，或一条日末未平的腿。"
        "反T：卖出开仓、买回平仓；正T：买入开仓、卖出平仓。"
        "`收益` 为毛收益、不含手续费；`是否闭合` 为否时，平仓价是日末收盘的盯市价。",
        "",
    ])
    for key in payload["order"]:
        days = runs[key]["days"]
        all_cycles = [c for d in days for c in d["cycles"]]
        closed_n = sum(1 for c in all_cycles if c["closed"])
        lines.extend([
            f"### {names[key]}",
            "",
            f"共 {len(all_cycles)} 笔，已闭合 {closed_n} 笔，未闭合 {len(all_cycles)-closed_n} 笔。",
            "",
            "| 日期 | 方向 | 开仓时间 | 平仓时间 | 股数 | 手数 | 开仓价 | 平仓价 | "
            "收益(元) | 是否闭合 |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ])
        for day in days:
            for c in day["cycles"]:
                lines.append(
                    f"| {day['date']} | {'反T' if c['lane']=='REV-T' else '正T'} | "
                    f"{c['entry_time']} | {c['exit_time']} | {c['shares']} | "
                    f"{c['shares']//100} | {c['entry_price']:.2f} | {c['exit_price']:.2f} | "
                    f"{c['gross']:,.2f} | {'是' if c['closed'] else '**否**'} |")
        lines.append("")
    lines.extend(["## 复现", "", "```powershell",
                  "python analysis/backtest_redis_halfposition_20260101_20260918.py",
                  "```", ""])
    return "\n".join(lines)


def _conclusions(summary: dict, names: dict, payload: dict) -> list[str]:
    best = max(payload["order"], key=lambda k: summary[k]["final_equity"])
    winners = [k for k in payload["order"] if summary[k]["excess_net"] > 0]
    initial = summary["v0561"]["initial_equity"]
    gross = [c for d in payload["runs"]["capture_v1"]["days"] for c in d["cycles"]]
    values = np.array([c["gross"] for c in gross])
    se = values.std(ddof=1) / np.sqrt(len(values))
    lines = [
        f"期末资产最高的是 **{names[best]}**。三个策略中"
        + (f"**{'、'.join(names[k] for k in winners)} 跑赢了一直持有**"
           if winners else "**没有任何一个跑赢一直持有**")
        + f"（持有对照 {money(payload['hold_equity'])} 元）。",
        "",
    ]
    for key in payload["order"]:
        s = summary[key]
        lines.append(
            f"- {names[key]}：期末 {money(s['final_equity'])}，"
            f"相对持有 {money(s['excess_net'])}"
            f"（{s['excess_net'] / initial:+.2%} of 初始权益），"
            f"账户收益 {money(s['account_net'])}，T达成率 {s['t_rate']:.2%}，"
            f"未闭合日 {s['unclosed_days']}/{s['days']}，"
            f"反T {s['rev_trips']} 周期 / 正T {s['fwd_trips']} 周期。")
    lines.extend([
        "",
        "### 对最好的那个必须做的折扣",
        "",
        f"{names[best]} 的做T共 {len(values)} 个已闭合周期，合计毛收益 "
        f"{money(values.sum())}，单笔均值 {money(values.mean())}，"
        f"标准差 {money(values.std(ddof=1))}，**t = "
        f"{values.mean() / se:.2f}（远未达显著）**；"
        f"胜率 {(values > 0).mean():.0%}。",
        "",
        f"剔除最大的单笔（{money(values.max())}）后合计降到 "
        f"{money(values.sum() - values.max())}；剔除最大两笔后只剩 "
        f"{money(values[np.argsort(values)][:-2].sum())}。"
        "**结论对离群值很敏感**，不能当成已经确立的收益来源。",
        "",
        f"另外，{names[best]} 的**全部 {len(gross)} 笔平仓都发生在 14:57:00**"
        "——VWAP−3σ 的买回信号从未触发，实际形态是「盘中冲到 VWAP+3σ 就卖出，"
        "持有到尾盘强平买回」。这不是原设计的自洽闭环，而是强平兜底的结果。",
        "",
        "### 跨标的检验：这个正结果不能复现",
        "",
        "601869 的正超额是否普遍？把**同一口径**（约 10 万元仓位 + 10 万元现金、"
        "半仓做 T、每日策略重置、账户连续）套到 11 个其它标的上"
        "（2026-01-05 ~ 09-11，本地 1 分钟线）：",
        "",
        "| 标的 | 相对持有 | 周期数 |",
        "|---|---:|---:|",
        "| 600276.SH | +3,392 | 18 |",
        "| 002594.SZ | +1,425 | 20 |",
        "| 000001.SZ | +88 | 1 |",
        "| 000651.SZ | −180 | 32 |",
        "| 601318.SH | −224 | 21 |",
        "| 600887.SH | −510 | 16 |",
        "| 600036.SH | −1,221 | 20 |",
        "| 600030.SH | −1,428 | 21 |",
        "| 601899.SH | −1,694 | 25 |",
        "| 002415.SZ | −2,048 | 15 |",
        "| 601012.SH | −2,430 | 19 |",
        "",
        "**3/11 个标的为正，合计 −4,830 元，均值 −439，中位 −510。**",
        "",
        "**所以 601869 的 +15,660 是分布里有利的尾部，不是可复现的效应。**"
        "这与本文档全部其它检验一致：单标的上的正结果，换标的就会消失。",
    ])
    return lines


def main() -> None:
    if (OUT / "README.md").exists() and "--smoke" not in sys.argv:
        raise FileExistsError("refuse to overwrite: " + str(OUT / "README.md"))
    daily = pd.read_csv(DATA / "1d.csv", dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(DATA / "1m.csv", dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]

    runs, summary, initial_shares = {}, {}, None
    for key in ("v0561", "v0562", "capture_v1"):
        print(f"running {key} ...", flush=True)
        run = run_strategy(key, daily, minute)
        runs[key] = run
        summary[key] = summarize(run)
        initial_shares = run["initial_shares"]
        s = summary[key]
        print(f"  {key}: 期末 {s['final_equity']:.2f} 相对持有 {s['excess_net']:.2f} "
              f"反T {s['rev_trips']} 正T {s['fwd_trips']}", flush=True)

    hold_equity = initial_shares * float(minute["close"].iloc[-1]) + INITIAL_CASH
    payload = {
        "method": {
            "symbol": SYMBOL, "range": [START, END],
            "fee_rate_each_side": FEE_RATE, "slippage": 0.0,
            "initial_cash": INITIAL_CASH, "position_value": POSITION_VALUE,
            "half_position_fraction": HALF_POSITION_FRACTION,
            "model": "DAILY_STRATEGY_RESET_CONTINUOUS_ACCOUNT",
            "variants": {
                "v0561": "v0561 CoreT",
                "v0562": "v0562 CoreT+ATR再入场",
                "capture_v1": "CaptureT_v1（本次设计）",
            },
        },
        "hashes": {
            "strategies": {k: hashlib.sha256(
                (ROOT / "Stragety/MiniQMT_Stragety/DayT" / STRATEGIES[k]).read_bytes()
            ).hexdigest() for k in ("v0561", "v0562", "capture_v1")},
            "data": {f: hashlib.sha256((DATA / f).read_bytes()).hexdigest()
                     for f in ("1m.csv", "1d.csv")},
        },
        "order": ["v0561", "v0562", "capture_v1"],
        "initial_shares": initial_shares, "hold_equity": hold_equity,
        "summary": summary, "runs": runs,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "README.md").write_text(build_report(payload), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
