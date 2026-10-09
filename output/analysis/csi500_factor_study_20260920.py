"""中证500 / 近三年（2023-09 ~ 2026-09）：因子研究。

## 口径声明（必须与任何结论一起引用）

**本窗口是上一轮预注册里锁死的 OOS-2，从未被看过。现在用它来做开发，
所以本脚本产出的一切都是「样本内」结果，不构成可预测性的证据。**

用户已明确接受这一点。报告里每一处都必须标注。

## 与上一轮全市场研究的差异

| 项 | 上一轮 | 本轮 |
|---|---|---|
| 股票池 | 沪深全市场 5,498 只 | **中证500 成分 500 只** |
| 窗口 | IS 2011–2018 | **2023-09 ~ 2026-09（3.0 年）** |
| 调仓期数 | 96 | **36**（月度）或 148（周度） |
| 市场代理 | 全市场等权 | **成分股等权** |

**统计功效是本轮最大的弱点**：36 个月度调仓期，非重叠 IC 观测点只有 36 个。
周度（5 日）能到 148 个，但换手率会高得多。

## 为什么因子在成分股子集上重算

`F7_ivol` / `F11_low_beta` 需要对市场回归，市场代理取「成分股等权」更贴合
中证500 口径。同时截面标准化也在成分股内做 —— 否则得到的是
「全市场标准化、在成分股里取子集」，与「成分股内相对排序」不是一回事。

## 筛选顺序（沿用上一轮的教训）

1. **IC 层** —— 但必须先过随机对照
2. **尾部层（top-N）** —— 上一轮的关键发现：IC 为正 ≠ 只做多能赚
3. 只有两关都过的才进组合回测

用法：
    python analysis/csi500_factor_study_20260920.py
    python analysis/csi500_factor_study_20260920.py --random-control 20
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
from portfolio.panel import PanelData                # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
UNIVERSE = ROOT / "analysis/panel_20260920/csi500_universe.json"
OUT = ROOT / "analysis/csi500_study_20260920"

START, END = "20230901", "20260918"
HORIZONS = (5, 21)
TOP_N = (10, 20, 30)
TARGET_ANNUAL = 0.50


def load_csi500() -> PanelData:
    codes = set(json.loads(UNIVERSE.read_text(encoding="utf-8"))["covered"])
    panel = PanelData.load(PANEL)
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
            "t_nw": metrics.newey_west_t(ic.to_numpy(), lags=3),
            "ic_series": ic}


def topn_stats(merged: pd.DataFrame) -> dict:
    universe = float(merged["fwd"].mean())
    out = {"universe_mean": universe}
    for n in TOP_N:
        picks = (merged.sort_values("alpha", ascending=False)
                 .groupby("date").head(n))
        per_date = picks.groupby("date")["fwd"].mean()
        out[f"top{n}_mean"] = float(per_date.mean())
        out[f"top{n}_median"] = float(per_date.median())
        out[f"top{n}_positive"] = float((per_date > 0).mean())
        out[f"top{n}_excess"] = float(per_date.mean() - universe)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factors", nargs="*", default=None)
    parser.add_argument("--random-control", type=int, default=20)
    args = parser.parse_args()

    print("载入中证500 面板 ...", flush=True)
    panel = load_csi500()
    long = panel.long
    print(f"  {len(long):,} 行，{len(panel.codes)} 只，"
          f"{long['date'].min()} ~ {long['date'].max()}", flush=True)

    window = (long["date"] >= START) & (long["date"] <= END)
    win_long = long[window]
    dates = sorted(win_long["date"].unique())
    print(f"  研究窗口 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）",
          flush=True)

    forwards = {h: forward_returns(long, h) for h in HORIZONS}
    size = transforms.size_proxy(long)

    keys = args.factors or list(factor_lib.FACTORS)
    rows = []
    for key in keys:
        t0 = time.time()
        try:
            base = factor_lib.compute(long, key)
        except Exception as error:
            print(f"  [fail] {key}: {type(error).__name__}: {error}", flush=True)
            continue
        prior = factor_lib.FACTORS[key].prior
        for treatment in ("raw", "size_neutral"):
            processed = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            processed = processed[(processed["date"] >= START)
                                  & (processed["date"] <= END)]
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
                row = {"factor": key, "prior": prior,
                       "treatment": treatment, "horizon": horizon,
                       "ic_mean": ic["ic_mean"], "ic_positive": ic["ic_positive"],
                       "t_nw": ic["t_nw"], "n_ic": ic["n"]}
                row.update(topn_stats(merged))
                rows.append(row)
        print(f"  {key} 完成（{time.time() - t0:.0f} 秒）", flush=True)

    grid = pd.DataFrame(rows)

    # ── 随机对照 ──
    control = {}
    if args.random_control:
        print(f"随机对照 {args.random_control} 次 ...", flush=True)
        rng = np.random.default_rng(20260920)
        best = []
        for i in range(args.random_control):
            sample = win_long[["code", "date"]].copy()
            sample["alpha"] = rng.normal(size=len(sample))
            sample = transforms.preprocess(sample)
            for horizon in HORIZONS:
                merged = sample.merge(forwards[horizon], on=["code", "date"],
                                      how="inner")
                merged = merged[(merged["date"] >= START)
                                & (merged["date"] <= END)]
                ic = ic_stats(merged, step=horizon)
                if ic:
                    best.append(abs(ic["ic_mean"]))
        control = {"n_draws": args.random_control,
                   "best_abs_ic": float(np.max(best)) if best else float("nan"),
                   "p95_abs_ic": float(np.percentile(best, 95)) if best else float("nan"),
                   "median_abs_ic": float(np.median(best)) if best else float("nan")}
        print(f"  随机 |IC| 上限 {control['best_abs_ic']:.4f}", flush=True)

    # ── 筛选 ──
    if not grid.empty and control:
        grid["beats_random"] = grid["ic_mean"].abs() > control["best_abs_ic"]
        grid["tail_ok"] = grid["top30_excess"] > 0
        grid["survives"] = grid["beats_random"] & grid["tail_ok"]
    else:
        grid["beats_random"] = grid["tail_ok"] = grid["survives"] = False

    # ── 报告 ──
    lines = [
        "# 中证500 / 近三年：因子研究",
        "",
        "> ## ⚠️ 样本内声明",
        ">",
        "> 2023-09 ~ 2026-09 是上一轮预注册里**锁死的 OOS-2**，从未被看过。",
        "> 本轮用它做开发，**所有结果都是样本内**，不构成可预测性的证据。",
        ">",
        "> 且窗口只有 **36 个月度调仓期**，统计功效很低。",
        "",
        f"- 股票池：中证500 成分 **{len(panel.codes)} 只**（面板 100% 覆盖）",
        f"- 窗口：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日 ≈ "
        f"{len(dates) / 243:.1f} 年）",
        "",
        "## 随机对照",
        "",
    ]
    if control:
        lines.extend([
            f"**{control['n_draws']} 个随机截面分数**走同一套管线：",
            f"`|IC|` 上限 **{control['best_abs_ic']:.4f}**、"
            f"95 分位 {control['p95_abs_ic']:.4f}、"
            f"中位 {control['median_abs_ic']:.4f}",
        ])

    lines.extend([
        "",
        "## IC 层",
        "",
        "| 因子 | 处理 | 持有期 | n | IC 均值 | IC>0 | t(NW) | 超随机 |",
        "|---|---|---:|---:|---:|---:|---:|:---:|",
    ])
    for _, r in grid.sort_values("ic_mean", ascending=False).iterrows():
        lines.append(
            f"| {r['factor']} | {r['treatment']} | {r['horizon']} | "
            f"{r['n_ic']} | {r['ic_mean']:+.4f} | {r['ic_positive']:.0%} | "
            f"{r['t_nw']:+.2f} | {'✓' if r['beats_random'] else '✗'} |")

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

    survivors = grid[grid["survives"]]
    lines.extend(["", "## 两关都过的", ""])
    if survivors.empty:
        lines.extend([
            "**没有配置同时通过「超随机」与「尾部为正」。**",
            "",
            "按上一轮确立的顺序，到此为止 —— 不进入组合回测。",
        ])
    else:
        lines.extend([
            f"共 **{len(survivors)}** 个，进入组合回测（Stage 4）：", "",
        ])
        for _, r in survivors.iterrows():
            lines.append(f"- `{r['factor']}` / {r['treatment']} / "
                         f"{r['horizon']} 日：IC {r['ic_mean']:+.4f}，"
                         f"top30 超额 {r['top30_excess']:+.3%}")

    lines.extend([
        "",
        "## 口径限制",
        "",
        "| 限制 | 影响 |",
        "|---|---|",
        "| **样本内**（2023-09~2026-09 曾是锁死的 OOS-2） | 结果不可作为可预测性证据 |",
        "| 只有 36 个月度调仓期 | 统计功效极低 |",
        "| 成分股用**当前**名单，无历史成分 | 生存者偏差（中证500 每半年调整约 10%） |",
        "| 市场代理取成分股等权 | 与中证500 市值加权口径不同 |",
        "| 历史 `isST` 未建模、F9 换手率缺失、无行业中性化 | 见 `analysis/选股择时策略研究结论.md` 第六节 |",
    ])

    OUT.mkdir(parents=True, exist_ok=True)
    grid.drop(columns=["ic_series"], errors="ignore").to_csv(
        OUT / "grid.csv", index=False, encoding="utf-8")
    (OUT / "random_control.json").write_text(
        json.dumps(control, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT / 'README.md'}")


if __name__ == "__main__":
    main()
