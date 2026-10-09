"""中证500 / 近三年：**运气基线** —— 随机组合能拿到多少？

## 为什么必须先做这一步

目标是「年化 50%」。在 3 年窗口里，这个数字本身**不构成技能的证据** ——
必须先知道「什么都不做、只是随机选股」能拿到什么分布。

如果随机组合里已经有相当比例达到 50%，那么任何「我找到了一个 3 年
年化 50% 的策略」的结论都是空的 —— 那是运气的形状，不是技能的形状。

仓库历史上被这件事坑过六次（最近一次是本轮的 F4：尾部检验
+2.26%/月，真实引擎 +0.84%/年）。这一次把它放到**最前面**做。

## 做法

* 股票池：中证500 成分 500 只（面板 100% 覆盖）
* 窗口：2023-09-01 ~ 2026-09-18（739 个交易日 ≈ 3.0 年）
* 每期月末随机选 N 只、等权、下期整批换掉
* 记录 3 年总收益与年化

**成本口径**：随机选股没有持续性，每期近似全额换手。
按单边 10bp（佣金+印花税+过户费+滑点）计，每期成本 ≈ 20bp。

同时给出：
* 最好的 K 个随机组合（best-of-K）—— 这是我们「试 K 次」的噪声上限
* 等权买入全部 500 只（不做任何选择的基准）

用法：
    python analysis/csi500_luck_baseline_20260920.py
    python analysis/csi500_luck_baseline_20260920.py --draws 2000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
UNIVERSES = {
    "csi500": ROOT / "analysis/panel_20260920/csi500_universe.json",
    "csi500main": ROOT / "analysis/panel_20260920/csi500_main_universe.json",
}
OUTS = {
    "csi500": ROOT / "analysis/csi500_baseline_20260920",
    "csi500main": ROOT / "analysis/csi500_main_baseline_20260920",
}

START, END = "20230901", "20260918"
TARGET_ANNUAL = 0.50
HOLDINGS = (10, 20, 30, 50)
COST_PER_SIDE = 0.001        # 单边 10bp：佣金 2.5bp + 印花税 5bp + 过户费 0.1bp + 滑点 5bp（近似）


def load(universe: str) -> tuple[pd.DataFrame, np.ndarray]:
    codes = json.loads(UNIVERSES[universe].read_text(encoding="utf-8"))["covered"]
    frame = pd.read_parquet(PANEL, columns=[
        "code", "time", "close", "adj_close", "tradable", "liquid",
        "cannot_buy", "listed_days"])
    # 窗口要往前多取——因子与第一个调仓日需要预热
    frame = frame[(frame["time"] <= END) & frame["code"].isin(codes)]
    return frame, np.array(sorted(codes))


def month_end_mask(dates: np.ndarray) -> np.ndarray:
    """月末交易日的布尔掩码。"""
    months = np.array([d[:6] for d in dates])
    return np.r_[months[1:] != months[:-1], True]


class Matrices:
    """预先 pivot 好的矩阵。

    原来每抽样一次就 pivot 一遍全表 —— 1000 次抽样就是 1000 次重复劳动，
    而且每次都在 pandas 里分配新对象。这里只做一次。
    """

    def __init__(self, frame: pd.DataFrame, dates: np.ndarray):
        def matrix(field, fill):
            return (frame.pivot(index="time", columns="code", values=field)
                    .reindex(dates).astype("float64").fillna(fill))

        close = matrix("adj_close", np.nan)
        self.rets = close.ffill().pct_change().fillna(0.0).to_numpy()
        self.tradable = matrix("tradable", 0.0).to_numpy() > 0.5
        self.liquid = matrix("liquid", 0.0).to_numpy() > 0.5
        self.listed = matrix("listed_days", 0.0).to_numpy()
        self.dates = dates
        self.ends = np.flatnonzero(month_end_mask(dates))


def simulate(mats: Matrices, n_holdings: int, rng: np.random.Generator | None,
             use_costs: bool = True) -> float:
    """一次随机组合的模拟，返回年化收益。

    `n_holdings=None` 表示「全部可买标的等权」，即不做任何选择的基准。
    """
    ends = mats.ends
    if len(ends) < 2:
        return np.nan

    equity = 1.0
    for i in range(len(ends) - 1):
        row = ends[i]
        elig = np.flatnonzero(mats.tradable[row] & mats.liquid[row]
                              & (mats.listed[row] >= 250))
        if len(elig) == 0:
            continue
        # **取 min 而不是跳过整期。** 写 `if len(elig) < n_holdings: continue`
        # 会让「全部标的等权」的基准在任一期末满员时整期不走 ——
        # 实测这会把基准压成 0.00%，看上去像「等权全成分不赚钱」。
        size = min(n_holdings, len(elig))
        picked = (elig if size == len(elig)
                  else rng.choice(elig, size=size, replace=False))
        segment = mats.rets[row + 1:ends[i + 1] + 1][:, picked]
        equity *= float((1.0 + segment.mean(axis=1)).prod())
        if use_costs:
            # 随机选股无持续性 → 近似全额换手
            equity *= (1.0 - 2.0 * COST_PER_SIDE)

    start, end = str(mats.dates[ends[0]]), str(mats.dates[ends[-1]])
    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    if years <= 0 or equity <= 0:
        return np.nan
    return equity ** (1.0 / years) - 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--universe", default="csi500",
                        choices=sorted(UNIVERSES),
                        help="csi500 | csi500main（中证500 主板 / 非ST）")
    args = parser.parse_args()

    OUT = OUTS[args.universe]
    frame, codes = load(args.universe)
    dates = np.array(sorted(frame["time"].unique()))
    dates = dates[dates >= START]
    frame = frame[frame["time"] >= START]
    label = ("中证500 主板 / 非ST" if args.universe == "csi500main"
             else "中证500 全成分")
    print(f"{label} {len(codes)} 只，窗口 {dates[0]} ~ {dates[-1]}"
          f"（{len(dates)} 个交易日）", flush=True)

    print("预计算矩阵 ...", flush=True)
    mats = Matrices(frame, dates)
    n_periods = len(mats.ends) - 1
    print(f"  调仓期数 {n_periods}", flush=True)

    # 成本换算要用**年**，不是**期数**。
    #
    # 这里原来写的是 `(1+gross) * (1-c) ** n_periods - 1`。但 `simulate()`
    # 返回的是**年化**收益率，`1+gross` 是「一年的财富」而不是「整段窗口的
    # 财富」。按 35 期去扣，等于把 35 个月的成本压在 1 年的财富上 ——
    # **成本被多扣了 ≈`years` 倍**（本窗口约 2.9 倍）。
    #
    # 实测影响：n=10 的净收益中位被压低到 +1.6%，正确值应约 +6.6%。
    # 修正后随机分布整体上移，对「引擎结果是否超出运气」的判定更严格。
    start_d, end_d = str(mats.dates[mats.ends[0]]), str(mats.dates[mats.ends[-1]])
    years = (pd.Timestamp(end_d) - pd.Timestamp(start_d)).days / 365.25
    cost_periods_per_year = n_periods / years
    print(f"  窗口 {years:.2f} 年，{n_periods} 期 → "
          f"成本按 {cost_periods_per_year:.2f} 期/年计", flush=True)

    rng = np.random.default_rng(args.seed)
    rows = []
    raw_draws: dict[int, np.ndarray] = {}
    for n in HOLDINGS:
        print(f"  随机 {n} 只，{args.draws} 次抽样 ...", flush=True)
        gross = np.array([simulate(mats, n, rng, use_costs=False)
                          for _ in range(args.draws)], dtype=float)
        gross = gross[np.isfinite(gross)]
        # 成本版：年化口径下，每年扣 `cost_periods_per_year` 期的成本
        net = ((1.0 + gross)
               * (1.0 - 2.0 * COST_PER_SIDE) ** cost_periods_per_year - 1.0)
        # **原始抽样必须落盘。** 只存中位数/P90 之类的汇总统计，
        # 就没法回答「引擎报的 +18% 到底落在哪个分位」——
        # 而那正是整份研究唯一要回答的问题。
        raw_draws[n] = net
        rows.append({
            "n_holdings": n,
            "gross_median": float(np.median(gross)),
            "net_median": float(np.median(net)),
            "net_p90": float(np.percentile(net, 90)),
            "net_p99": float(np.percentile(net, 99)),
            "net_max": float(np.max(net)),
            "p_target": float((net >= TARGET_ANNUAL).mean()),
        })

    result = pd.DataFrame(rows)

    # 等权买入全部成分
    bench = simulate(mats, 10 ** 9, None, use_costs=True)

    lines = [
        f"# {label} / 近三年：运气基线",
        "",
        f"窗口 **{dates[0]} ~ {dates[-1]}**（{len(dates)} 个交易日 ≈ "
        f"{len(dates) / 243:.1f} 年），股票池 **{label} {len(codes)} 只**。",
        f"随机抽样 **{args.draws}** 次，每期月末等权、下期整批换掉。",
        "",
        # 两个地方都乘 100 会把 20bp 印成「0.2bp」—— bp 是万分之一，
        # 换算回 bp 要乘 10000，不是乘 100。
        f"**成本口径**：随机选股无持续性 → 每期近似全额换手，"
        f"按单边 {COST_PER_SIDE * 10000:.0f}bp 计，每期约 "
        f"{2 * COST_PER_SIDE * 10000:.0f}bp。",
        "",
        "## 核心问题：随机组合有多少能到 50%/年？",
        "",
        "| 持仓数 | 毛收益中位 | **净收益中位** | 净收益 P90 | P99 | 最好的一次 | **≥50%/年 的比例** |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in result.iterrows():
        lines.append(
            f"| {r['n_holdings']} | {r['gross_median']:+.1%} | "
            f"**{r['net_median']:+.1%}** | {r['net_p90']:+.1%} | "
            f"{r['net_p99']:+.1%} | {r['net_max']:+.1%} | "
            f"**{r['p_target']:.2%}** |")

    lines.extend([
        "",
        f"等权买入全部成分（不做任何选择）：年化 **{bench:+.1%}**",
        "",
        "## 怎么读这张表",
        "",
        "**如果 `≥50%/年 的比例` 明显大于 0**，说明这个目标在 3 年窗口里",
        "**不需要任何技能就能达到** —— 那么在此窗口上找出一个「年化 50% 的",
        "策略」就不构成证据，它只是从这个分布里抽了一个样本。",
        "",
        "**如果这个比例是 0**，说明 50% 确实在随机分布的尾部之外，",
        "那么「找到了 50%」才值得进一步追问「它可复现吗」。",
        "",
        "无论哪种结果，**3 年窗口的统计功效都很低** —— 后面研究结论",
        "必须把这一点写在最前面。",
    ])

    OUT.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUT / "baseline.csv", index=False, encoding="utf-8")
    pd.DataFrame(raw_draws).to_parquet(OUT / "draws.parquet")
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print()
    print(result.to_string(index=False))
    print(f"\n等权全成分年化 {bench:+.2%}")
    print(f"→ {OUT / 'README.md'}")


if __name__ == "__main__":
    main()
