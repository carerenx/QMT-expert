"""LongHoldRotation_v2 的回测 —— 与组合策略（收窄池）做同口径比较。

## 为什么要专门跑这个

`Stragety/RedisQMT/Long/LongHoldRotation_v2_TrendHysteresis.py` 只出信号、
不回测，`Stragety/RedisQMT/Long/StrategicResearchDirectionsAndEffectivenessRecords.md`
里写着「**尚未录入**」——**没有任何已记录的收益数字**。
所以「跟它比收益」这个问题，在补上这个回测之前是无法回答的。

## 它是什么

**单标的**（`600584.SH` 长电科技）的趋势配置策略，不是轮动：

* `classify_regime` → `REGIME_WEIGHTS`
  {STRONG_BULL 1.00 / BULL 0.85 / SIDEWAYS 0.60 / BEAR 0.30}
* **权重地板 0.30** —— 永不空仓，也永不满仓（除非 STRONG_BULL）
* 单次调仓上限 25% 权益，最小权重差 0.10 → 渐进式、低换手
* 回撤阶梯：15% → 上限 0.75 ；20% → 上限 0.45 ；25% → 目标 0.30
* regime 需连续 3 日确认（BEAR 立即生效）

## 与组合策略的可比性

两者**不是同一策略类型**（按仓库规则标 `不具可比性`）：

| | Long（本策略） | Portfolio（上一轮交付） |
|---|---|---|
| 标的数 | **1 只** | 408 只（中证500 主板/非ST） |
| 决策 | 单标的总仓位 0.30~1.00 | 横截面选股 + 仓位 |
| 账户模型 | 单标的持仓 | 组合净值 |

**但 `decide_allocation` 用的 `classify_regime` 正是组合策略择时层当年包的那套。**
所以这个比较有实质意义：它是「同样的 regime 择时，用在单标的上」的表现。

## 口径

* 窗口 2023-09-01 ~ 2026-09-18（与组合研究同窗口）
* 复权价（`adj_*`，含股息再投的总收益）
* **T 收盘算信号、T+1 开盘成交**（与组合引擎同口径）
* 成本：`portfolio/costs.py`，滑点 5bp
* 基准：**买入持有 600584.SH**（这才是它的正确对照，不是中证500）

用法：
    python analysis/backtest_longhold_v2_vs_portfolio_20260920.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import metrics                                  # noqa: E402
from portfolio.costs import CostModel                          # noqa: E402
from Stragety.MiniQMT_Stragety.core.long_hold_allocation import (  # noqa: E402
    calculate_indicators, classify_regime)
from Stragety.MiniQMT_Stragety.core.long_hold_allocation_v2 import (  # noqa: E402
    decide_allocation, target_order)


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
OUT = ROOT / "analysis/longhold_v2_backtest_20260920"

STOCK = "600584.SH"
START, END = "20230901", "20260918"
WARMUP = "20220101"
INITIAL = 100_000.0
MIN_BARS = 140          # v2 要求至少 140 根已完成日线


def load_bars(stock: str) -> pd.DataFrame:
    frame = pd.read_parquet(PANEL, columns=[
        "code", "time", "adj_open", "adj_high", "adj_low", "adj_close"])
    frame = frame[(frame["code"] == stock)
                  & (frame["time"] >= WARMUP) & (frame["time"] <= END)]
    frame = frame.sort_values("time").reset_index(drop=True)
    return frame.rename(columns={"time": "date"})


def run(bars: pd.DataFrame, execution: str, costs: CostModel,
        stock: str) -> tuple[pd.Series, pd.Series]:
    """按 v2 的逻辑逐日重放。返回净值序列（索引为日期）。"""
    dates = bars["date"].tolist()
    highs = bars["adj_high"].tolist()
    lows = bars["adj_low"].tolist()
    closes = bars["adj_close"].tolist()
    opens = bars["adj_open"].tolist()

    cash, shares = INITIAL, 0
    peak = INITIAL
    active_regime = None
    prev_candidate = None
    streak = 0
    equity_curve, weight_rows = {}, {}

    for i in range(MIN_BARS - 1, len(dates) - 1):
        signal_date, exec_date = dates[i], dates[i + 1]
        if signal_date < START:
            continue

        price = closes[i]
        equity = cash + shares * price
        peak = max(peak, equity)
        drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
        current_weight = shares * price / equity if equity > 0 else 0.0

        # 与 v2 一致：先用**当日候选**更新连续天数，再调用 decide_allocation
        candidate = classify_regime(
            calculate_indicators(highs[:i + 1], lows[:i + 1],
                                 closes[:i + 1]))[0]
        streak = streak + 1 if candidate == prev_candidate else 1
        prev_candidate = candidate

        decision = decide_allocation(
            highs[:i + 1], lows[:i + 1], closes[:i + 1], current_weight,
            drawdown, signal_date, exec_date, active_regime, streak)
        active_regime = decision["regime"]

        # **`sessions_since_rebalance` 原脚本从 state 读、但从不写回** ——
        # 所以它恒为 None，`target_order` 里那段冷却期检查永远被跳过。
        # 这里照样传 None，保持与原脚本一致；不一致才是真的分歧。
        delta = target_order(
            shares, cash, price, decision["target_weight"],
            sessions_since_rebalance=None,
            allow_buy=decision["risk_state"] != "HALT_BUYS")

        if delta:
            # T+1 开盘成交（`close` 口径用 signal_date 收盘价，作诊断对照）
            raw_price = opens[i + 1] if execution == "open" else price
            side = "sell" if delta < 0 else "buy"
            # 滑点体现在成交价里，费用单独收 —— 两件事，不能合并成一项。
            fill = costs.fill_price(raw_price, side)
            fee = costs.total_fee(stock, exec_date, side, fill, abs(int(delta)))
            cash -= delta * fill + fee
            shares += delta

        equity_curve[exec_date] = cash + shares * closes[i + 1]
        weight_rows[exec_date] = (shares * closes[i + 1]
                                  / max(equity_curve[exec_date], 1e-9))

    return pd.Series(equity_curve).sort_index(), pd.Series(weight_rows).sort_index()


def summarize(stock: str, execution: str, costs: CostModel) -> dict:
    bars = load_bars(stock)
    if len(bars) < MIN_BARS + 50:
        return {}
    window = bars[(bars["date"] >= START) & (bars["date"] <= END)]
    if len(window) < 100:
        return {}
    first, last = window["adj_close"].iloc[0], window["adj_close"].iloc[-1]
    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    hold_total = last / first - 1.0
    hold_annual = (1.0 + hold_total) ** (1.0 / years) - 1.0

    equity, weights = run(bars, execution, costs, stock)
    curve = equity[equity.index >= START]
    if len(curve) < 100:
        return {}
    curve = curve / curve.iloc[0]
    return {
        "stock": stock,
        "策略年化": metrics.annualized_return(curve),
        "夏普": metrics.sharpe(curve.pct_change().dropna()),
        "最大回撤": metrics.max_drawdown(curve),
        "平均仓位": float(weights[weights.index >= START].mean()),
        "持有年化": hold_annual,
        "持有总收益": hold_total,
        "超额": metrics.annualized_return(curve) - hold_annual,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock", default=STOCK)
    parser.add_argument("--scan", type=int, default=0,
                        help="随机扫 N 只主板股（含本股），量单标的结果的离散度")
    parser.add_argument("--seed", type=int, default=20260920)
    args = parser.parse_args()

    costs = CostModel(slippage_bps=5.0)
    bars = load_bars(args.stock)
    print(f"{args.stock}：{len(bars)} 根日线，"
          f"{bars['date'].iloc[0]} ~ {bars['date'].iloc[-1]}", flush=True)

    window = bars[(bars["date"] >= START) & (bars["date"] <= END)]
    first, last = window["adj_close"].iloc[0], window["adj_close"].iloc[-1]
    hold_total = last / first - 1.0
    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    hold_annual = (1.0 + hold_total) ** (1.0 / years) - 1.0

    rows = []
    for execution in ("open", "close"):
        equity, weights = run(bars, execution, costs, args.stock)
        curve = equity[equity.index >= START]
        # 归一化到 1.0 起点，才和买入持有可比
        curve = curve / curve.iloc[0]
        rows.append({
            "口径": f"T+1 开盘" if execution == "open" else "T 日收盘（诊断）",
            "年化": metrics.annualized_return(curve),
            "夏普": metrics.sharpe(curve.pct_change().dropna()),
            "最大回撤": metrics.max_drawdown(curve),
            "总收益": float(curve.iloc[-1] - 1.0),
            "平均仓位": float(weights[weights.index >= START].mean()),
            "换手(累计%)": float(abs(weights.diff()).sum() * 100),
        })

    scan_table = []
    if args.scan:
        import json
        codes = json.loads(
            (ROOT / "analysis/panel_20260920/csi500_main_universe.json")
            .read_text(encoding="utf-8"))["covered"]
        rng = np.random.default_rng(args.seed)
        picks = [args.stock] + list(rng.choice(
            [c for c in codes if c != args.stock],
            size=max(0, args.scan - 1), replace=False))
        print(f"\n扫描 {len(picks)} 只主板股（评估单标的结果的离散度）...",
              flush=True)
        for i, code in enumerate(picks, 1):
            out = summarize(code, "open", costs)
            if out:
                scan_table.append(out)
                print(f"  [{i}/{len(picks)}] {code} 策略 {out['策略年化']:+.1%}"
                      f" / 持有 {out['持有年化']:+.1%}", flush=True)

    lines = [
        "# LongHoldRotation_v2 回测 —— 与组合策略的同窗口比较",
        "",
        f"标的 **{STOCK}**，窗口 **{START} ~ {END}**（{years:.2f} 年），"
        f"初始资金 {INITIAL:,.0f} 元。",
        "",
        "> **口径**：复权价（含股息再投）、T 收盘算信号 T+1 开盘成交、",
        "> 成本用 `portfolio/costs.py`（佣金万2.5 + 印花税按日期 + 过户费按日期 + 滑点 5bp）。",
        "",
        "## 对照：买入持有",
        "",
        f"**{STOCK}** 期间 {first:.2f} → {last:.2f}，"
        f"总收益 **{hold_total:+.1%}**，年化 **{hold_annual:+.1%}**。",
        "",
        "## 结果",
        "",
        "| 口径 | 年化 | 夏普 | 最大回撤 | 总收益 | 平均仓位 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['口径']} | **{r['年化']:+.1%}** | {r['夏普']:.2f} | "
            f"{r['最大回撤']:.1%} | {r['总收益']:+.1%} | {r['平均仓位']:.1%} |")

    main_row = rows[0]
    lines.extend([
        "",
        f"买入持有年化 **{hold_annual:+.1%}** vs 策略年化 "
        f"**{main_row['年化']:+.1%}** → "
        f"**策略跑输 {main_row['年化'] - hold_annual:+.1%}**",
        "",
        "## 与组合策略（收窄池）对照",
        "",
        "| 策略 | 标的数 | 年化 | 窗口 |",
        "|---|---:|---:|---|",
        f"| LongHoldRotation_v2（本策略） | 1 | **{main_row['年化']:+.1%}** | 2023-09~2026-09 |",
        f"| 买入持有 {STOCK} | 1 | **{hold_annual:+.1%}** | 同上 |",
        "| Portfolio 收窄池 F10 隔夜 s_n n=30 | 30 | +18.0% | 同上 |",
        "| Portfolio 收窄池 F7 特质波动率 s_n n=10 | 10 | +7.8% | 同上 |",
        "| Portfolio 收窄池「随机选股」A 线 | 10~30 | +4.1~5.7% | 同上 |",
        "| 收窄池等权全成分 | 415 | +7.0% | 同上 |",
        "",
        "## 必须同时读的三点",
        "",
        "1. **两者不是同一策略类型** —— 单标的 vs 多标的组合，按仓库规则",
        "   标 `不具可比性`。上表只是把它们放在同一窗口同一成本口径下并排，",
        "   **不构成「谁更好」的判定**。",
        "2. **单标的的结果不可外推** —— 600584.SH 这三年是大牛股",
        f"   （{hold_total:+.0%}）。同一个策略换一只没涨的股票，结果会完全不同。",
        "   组合策略的 408 只池子没有这个问题。",
        "3. **它和组合策略的择时层是同一套 regime 逻辑** —— 组合研究已经量过",
        "   这套逻辑在两个口径下都是负贡献（−5.32% / −6.64%/年）。",
        "   本回测是在验证同一件事在单标的上是否也成立。",
        "",
        "## 已知实现细节（照原样复现，不改）",
        "",
        "* `sessions_since_rebalance` 原脚本**从 state 读、但从不写回** →",
        "  恒为 `None` → `target_order` 里的 5 日冷却期**永远不生效**。",
        "  本回测照传 `None`，与原脚本一致。",
        "* 权重地板 0.30：**永不空仓**。躲不掉 600584 的任何一段下跌。",
        "* 单次调仓上限 25% 权益 → 转向慢，且 `min_weight_gap=0.10`",
        "  意味着权重差 < 10% 时**完全不动**。",
    ])

    if scan_table:
        scan = pd.DataFrame(scan_table).sort_values("超额", ascending=False)
        wins = int((scan["超额"] > 0).sum())
        lines.extend([
            "",
            "## 同一策略换股票的离散度（这是最关键的一张表）",
            "",
            f"在收窄池里随机取 **{len(scan)} 只主板股**，跑**同一个**策略：",
            "",
            "| 分位 | 策略年化 | 持有年化 | 超额 |",
            "|---|---:|---:|---:|",
            f"| 最好 | {scan['策略年化'].max():+.1%} | "
            f"{scan.loc[scan['超额'].idxmax(), '持有年化']:+.1%} | "
            f"{scan['超额'].max():+.1%} |",
            f"| 中位 | {scan['策略年化'].median():+.1%} | "
            f"{scan['持有年化'].median():+.1%} | "
            f"{scan['超额'].median():+.1%} |",
            f"| 最差 | {scan['策略年化'].min():+.1%} | "
            f"{scan.loc[scan['超额'].idxmin(), '持有年化']:+.1%} | "
            f"{scan['超额'].min():+.1%} |",
            "",
            f"**策略跑赢买入持有的只有 {wins}/{len(scan)} 只"
            f"（{wins / len(scan):.0%}）**；中位超额 "
            f"**{scan['超额'].median():+.1%}/年**。",
            "",
            "> **这就是「单标的策略的收益」的真面目**：它不是一个策略属性，",
            "> 而是「策略 × 你恰好选的那只股票」的联合结果。",
            "> 600584.SH 这三年涨了 "
            f"{hold_total:+.0%}，所以它的数字好看；换一只同样跑这个策略，",
            "> 结果会掉到完全不同的地方。",
            "",
            "### 全部样本",
            "",
            "| 股票 | 策略年化 | 持有年化 | 超额 | 平均仓位 | 最大回撤 |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for _, r in scan.iterrows():
            mark = "**" if r["stock"] == args.stock else ""
            lines.append(
                f"| {mark}{r['stock']}{mark} | {r['策略年化']:+.1%} | "
                f"{r['持有年化']:+.1%} | {r['超额']:+.1%} | "
                f"{r['平均仓位']:.0%} | {r['最大回撤']:.1%} |")
        scan.to_csv(OUT / "scan.csv", index=False, encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "result.csv", index=False, encoding="utf-8")
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print()
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\n买入持有 {args.stock} 年化 {hold_annual:+.2%}")
    if scan_table:
        scan = pd.DataFrame(scan_table)
        print(f"扫描 {len(scan)} 只：策略跑赢持有的 "
              f"{int((scan['超额'] > 0).sum())}/{len(scan)}，"
              f"中位超额 {scan['超额'].median():+.1%}/年")
    print(f"→ {OUT / 'README.md'}")


if __name__ == "__main__":
    main()
