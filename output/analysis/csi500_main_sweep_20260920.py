"""收窄池（中证500 ∩ 主板 ∩ 非ST）上的因子尾部检验 + 引擎扫描。

## 这个脚本回答的问题

用户要求把择股范围收窄为「中证500 中的股票，非ST，非创业板科创板」。
收窄后是 415 只，**±20% 涨跌停板全部消失**。

中证500 原口径下唯一走到引擎层的最强因子是 F6（MAX，回避彩票股），
而彩票股正住在创业板/科创板。所以必须回答：

> **原来的结论在收窄池上还成立吗？**

## 与中证500 研究的关系（可比性）

用**完全相同的 16 个配置**（4 因子 × 2 处理 × 2 持仓数）、
完全相同的引擎与成本模型，**只换股票池**。这样两边可直接对比。

**但必须记在 trials_log 里**：这是同一个 2023-09~2026-09 窗口上的
**第 17 次**尝试。换股票池不是免检操作 —— 它同样消耗一个自由度。

## 判定顺序（沿用本轮确立的教训）

1. **运气基线先做** —— 在同一个收窄池上，否则引擎数字无意义
2. **IC 层** —— 必须先过随机对照
3. **尾部层（top-N）** —— IC 为正 ≠ 只做多能赚（本轮最重要的发现）
4. **引擎层** —— 含成本、T+1 开盘成交、涨跌停、整手

用法：
    python analysis/csi500_main_sweep_20260920.py --stage tail
    python analysis/csi500_main_sweep_20260920.py --stage engine
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
sys.path.insert(0, str(ROOT))

from portfolio import factors as factor_lib          # noqa: E402
from portfolio import metrics, transforms            # noqa: E402
from portfolio.costs import CostModel                # noqa: E402
from portfolio.engine import PortfolioEngine         # noqa: E402
from portfolio.panel import PanelData                # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
UNIVERSE = ROOT / "analysis/panel_20260920/csi500_main_universe.json"
OUT = ROOT / "analysis/csi500_main_study_20260920"

START, END = "20230901", "20260918"
WARMUP = "20210101"

HORIZONS = (5, 21)
TOP_N = (10, 20, 30)

# **与中证500 研究完全相同的 16 个配置** —— 换池不换配置，才可比
SWEEP_FACTORS = ("F6_max_effect", "F7_ivol", "F2_reversal_1m",
                 "F10_overnight_intraday")
SWEEP_TREATMENTS = ("raw", "size_neutral")
SWEEP_HOLDINGS = (10, 30)


def load_panel() -> PanelData:
    codes = set(json.loads(UNIVERSE.read_text(encoding="utf-8"))["codes"])
    panel = PanelData.load(PANEL).between(WARMUP, END)
    long = panel.long[panel.long["code"].isin(codes)]
    return PanelData.from_frame(long.reset_index(drop=True))


def forward_returns(long: pd.DataFrame, horizon: int) -> pd.DataFrame:
    frame = long[["code", "date", "adj_close"]].sort_values(["code", "date"])
    frame["fwd"] = (frame.groupby("code")["adj_close"]
                    .transform(lambda s: s.shift(-horizon) / s - 1.0))
    return frame[["code", "date", "fwd"]]


def ic_stats(merged: pd.DataFrame, step: int) -> dict:
    dates = set(sorted(merged["date"].unique())[::step])
    sampled = merged[merged["date"].isin(dates)]
    rows = []
    for _, day in sampled.groupby("date", sort=True):
        value = metrics.spearman_ic(day.set_index("code")["alpha"],
                                    day.set_index("code")["fwd"])
        if np.isfinite(value):
            rows.append(value)
    ic = pd.Series(rows)
    if len(ic) < 5:
        return {}
    return {"n": int(len(ic)), "ic_mean": float(ic.mean()),
            "ic_positive": float((ic > 0).mean()),
            "t_nw": metrics.newey_west_t(ic.to_numpy(), lags=3)}


def topn_stats(merged: pd.DataFrame) -> dict:
    universe = float(merged["fwd"].mean())
    out = {"universe_mean": universe}
    for n in TOP_N:
        picks = (merged.sort_values("alpha", ascending=False)
                 .groupby("date").head(n))
        per_date = picks.groupby("date")["fwd"].mean()
        out[f"top{n}_mean"] = float(per_date.mean())
        out[f"top{n}_excess"] = float(per_date.mean() - universe)
        out[f"top{n}_positive"] = float((per_date > 0).mean())
    return out


# ─────────────────────── Stage: tail ───────────────────────

def stage_tail(panel: PanelData, random_control: int) -> pd.DataFrame:
    long = panel.long
    dates = sorted(long[(long["date"] >= START)]["date"].unique())
    print(f"  研究窗口 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）",
          flush=True)

    forwards = {h: forward_returns(long, h) for h in HORIZONS}
    size = transforms.size_proxy(long)

    rows = []
    for key in factor_lib.FACTORS:
        t0 = time.time()
        try:
            base = factor_lib.compute(long, key)
        except Exception as error:
            print(f"  [fail] {key}: {type(error).__name__}: {error}", flush=True)
            continue
        if base.empty:
            continue
        for treatment in SWEEP_TREATMENTS:
            processed = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            processed = processed[processed["date"] >= START]
            if processed.empty:
                continue
            for horizon in HORIZONS:
                merged = processed.merge(forwards[horizon],
                                         on=["code", "date"], how="inner")
                if merged.empty:
                    continue
                ic = ic_stats(merged, step=horizon)
                if not ic:
                    continue
                row = {"factor": key, "treatment": treatment,
                       "horizon": horizon, "ic_mean": ic["ic_mean"],
                       "ic_positive": ic["ic_positive"], "t_nw": ic["t_nw"],
                       "n_ic": ic["n"]}
                row.update(topn_stats(merged))
                rows.append(row)
        print(f"  {key} 完成（{time.time() - t0:.0f} 秒）", flush=True)

    grid = pd.DataFrame(rows)

    control = {}
    if random_control:
        print(f"  随机对照 {random_control} 次 ...", flush=True)
        narrowed = long[long["date"] >= START]
        rng = np.random.default_rng(20260920)
        best = []
        for _ in range(random_control):
            sample = narrowed[["code", "date"]].copy()
            sample["alpha"] = rng.normal(size=len(sample))
            sample = transforms.preprocess(sample)
            for horizon in HORIZONS:
                merged = sample.merge(forwards[horizon], on=["code", "date"],
                                      how="inner")
                merged = merged[merged["date"] >= START]
                ic = ic_stats(merged, step=horizon)
                if ic:
                    best.append(abs(ic["ic_mean"]))
        control = {"n_draws": random_control,
                   "best_abs_ic": float(np.max(best)),
                   "p95_abs_ic": float(np.percentile(best, 95)),
                   "median_abs_ic": float(np.median(best))}
        print(f"    随机 |IC| 上限 {control['best_abs_ic']:.4f}", flush=True)

    if not grid.empty and control:
        grid["beats_random"] = grid["ic_mean"].abs() > control["best_abs_ic"]
        grid["tail_ok"] = grid["top30_excess"] > 0
        grid["survives"] = grid["beats_random"] & grid["tail_ok"]

    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_csv(OUT / "grid.csv", index=False, encoding="utf-8")
    (OUT / "random_control.json").write_text(
        json.dumps(control, ensure_ascii=False, indent=2), encoding="utf-8")
    return grid


# ─────────────────────── Stage: engine ───────────────────────

def stage_engine(panel: PanelData, slippage_bps: float) -> pd.DataFrame:
    long = panel.long
    size = transforms.size_proxy(long)
    costs = CostModel(slippage_bps=slippage_bps)
    benchmark = panel.field_matrix("close", panel.dates)
    rows = []

    total = (len(SWEEP_FACTORS) * len(SWEEP_TREATMENTS) * len(SWEEP_HOLDINGS))
    done = 0
    for key in SWEEP_FACTORS:
        base = factor_lib.compute(long, key)
        for treatment in SWEEP_TREATMENTS:
            alpha = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            for n_holdings in SWEEP_HOLDINGS:
                done += 1
                t0 = time.time()
                engine = PortfolioEngine(panel, costs=costs,
                                         n_holdings=n_holdings,
                                         rebalance_rule="M",
                                         execution="open")
                result = engine.run(lambda p, a=alpha: a, None,
                                    start=START, end=END)
                curve = result.equity
                annual = metrics.annualized_return(curve)
                # `metrics.sharpe` 收的是**收益率**不是净值 —— 传 curve 进去
                # 会把「净值序列」当收益算，得到的是个好看但无意义的数。
                sharpe = metrics.sharpe(result.returns)
                # `rebalances` 是 DataFrame，直接迭代得到的是**列名**不是行。
                # 用引擎算好的 diagnostics，别再自己遍历一遍。
                diag = result.diagnostics
                rows.append({
                    "factor": key, "treatment": treatment, "n": n_holdings,
                    "ann": annual,
                    "sharpe": sharpe,
                    "dd": metrics.max_drawdown(curve),
                    "turnover": diag.get("avg_turnover", 0.0) * 12.0,
                    "fees": diag.get("total_fees", 0.0),
                })
                print(f"  [{done}/{total}] {key}/{treatment}/n={n_holdings} "
                      f"年化 {annual:+.1%}（{time.time() - t0:.0f} 秒）",
                      flush=True)

    grid = pd.DataFrame(rows).sort_values("ann", ascending=False)
    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_csv(OUT / "engine_sweep.csv", index=False, encoding="utf-8")
    return grid


# ─────────────────────── 报告 ───────────────────────

def write_report(grid: pd.DataFrame, engine: pd.DataFrame | None,
                 control: dict) -> None:
    lines = [
        "# 收窄池（中证500 主板 / 非ST）：因子研究",
        "",
        "> ## ⚠️ 样本内 + 第 17 次尝试",
        ">",
        "> 2023-09 ~ 2026-09 是上一轮预注册里**锁死的 OOS-2**。",
        "> 中证500 原口径已在此窗口上试过 16 个配置；**换股票池再来一次",
        "> 是第 17 次**，同样消耗一个自由度。",
        ">",
        "> 本页一切结果都是样本内，不构成可预测性的证据。",
        "",
        "## 收窄规则",
        "",
        "中证500 ∩ 主板（沪 600/601/603/605、深 000/001/002/003）∩ 非当前ST。",
        "**415 只**（原 500 − 创业板 56 − 科创板 26 − 当前ST 3）。",
        "",
        "**关键影响不是少了 85 只，而是 ±20% 涨跌停板全部消失。**",
        "",
        "## 随机对照",
        "",
        f"`|IC|` 上限 **{control.get('best_abs_ic', float('nan')):.4f}**、"
        f"95 分位 {control.get('p95_abs_ic', float('nan')):.4f}、"
        f"中位 {control.get('median_abs_ic', float('nan')):.4f}"
        f"（{control.get('n_draws', 0)} 次抽样）",
        "",
        "## IC 层",
        "",
        "| 因子 | 处理 | 持有期 | n | IC 均值 | IC>0 | t(NW) | 超随机 |",
        "|---|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for _, r in grid.sort_values("ic_mean", ascending=False).iterrows():
        lines.append(
            f"| {r['factor']} | {r['treatment']} | {r['horizon']} | {r['n_ic']} | "
            f"{r['ic_mean']:+.4f} | {r['ic_positive']:.0%} | {r['t_nw']:+.2f} | "
            f"{'✓' if r['beats_random'] else '✗'} |")

    lines.extend([
        "",
        "## 尾部层（只做多真正能拿到的）",
        "",
        "| 因子 | 处理 | 持有期 | top10 超额 | top20 超额 | **top30 超额** | top30 为正 | 尾部通过 |",
        "|---|---|---:|---:|---:|---:|---:|:---:|",
    ])
    for _, r in grid.sort_values("top30_excess", ascending=False).iterrows():
        lines.append(
            f"| {r['factor']} | {r['treatment']} | {r['horizon']} | "
            f"{r['top10_excess']:+.3%} | {r['top20_excess']:+.3%} | "
            f"**{r['top30_excess']:+.3%}** | {r['top30_positive']:.0%} | "
            f"{'✓' if r['tail_ok'] else '✗'} |")

    if engine is not None and not engine.empty:
        lines.extend([
            "",
            "## 引擎层（16 个配置，与中证500 研究完全相同）",
            "",
            "| 因子 | 处理 | 持仓 | 年化 | 夏普 | 最大回撤 | 年化换手 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ])
        for _, r in engine.iterrows():
            lines.append(
                f"| {r['factor']} | {r['treatment']} | {r['n']} | "
                f"**{r['ann']:+.1%}** | {r['sharpe']:.2f} | {r['dd']:.1%} | "
                f"{r['turnover']:.0%} |")
        lines.extend([
            "",
            f"中位年化 **{engine['ann'].median():+.1%}**，"
            f"极差 {engine['ann'].min():+.1%} ~ **{engine['ann'].max():+.1%}**",
        ])

    lines.extend([
        "",
        "## 口径限制",
        "",
        "| 限制 | 影响 |",
        "|---|---|",
        "| **样本内 + 第 17 次尝试** | 结果不可作为可预测性证据 |",
        "| 只有 36 个月度调仓期 | 统计功效极低 |",
        "| 当前成分名单，无历史成分 | 生存者偏差 |",
        "| **历史 `isST` 未建模** | 只剔了**当前**名字含 ST 的 3 只，带轻微前视 |",
        "| 7 只取不到桥接详情 | 见 `instrument_names.json`，其中 6 只窗口中途停止交易 |",
    ])

    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT / 'README.md'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="all",
                        choices=["tail", "engine", "all"])
    parser.add_argument("--random-control", type=int, default=20)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    print("载入收窄池面板 ...", flush=True)
    panel = load_panel()
    print(f"  {len(panel.long):,} 行，{len(panel.codes)} 只，"
          f"{panel.long['date'].min()} ~ {panel.long['date'].max()}", flush=True)

    grid = pd.DataFrame()
    engine = None
    if args.stage in ("tail", "all"):
        print("\n[Stage 尾部检验]", flush=True)
        grid = stage_tail(panel, args.random_control)
    if args.stage in ("engine", "all"):
        print("\n[Stage 引擎扫描]", flush=True)
        engine = stage_engine(panel, args.slippage_bps)

    control_path = OUT / "random_control.json"
    control = (json.loads(control_path.read_text(encoding="utf-8"))
               if control_path.exists() else {})
    if grid.empty and (OUT / "grid.csv").exists():
        grid = pd.read_csv(OUT / "grid.csv")
    if engine is None and (OUT / "engine_sweep.csv").exists():
        engine = pd.read_csv(OUT / "engine_sweep.csv")
    write_report(grid, engine, control)

    if not grid.empty:
        print("\nIC 层（前 8）：")
        print(grid.sort_values("ic_mean", ascending=False).head(8)[
            ["factor", "treatment", "horizon", "ic_mean", "t_nw",
             "top30_excess"]].to_string(index=False))
    if engine is not None and not engine.empty:
        print("\n引擎层（前 8）：")
        print(engine.head(8).to_string(index=False))


if __name__ == "__main__":
    main()
