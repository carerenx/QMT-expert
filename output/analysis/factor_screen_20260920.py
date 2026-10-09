"""Stage 3 因子筛选：66 个预注册配置 + 20 个随机对照。

## 分两层，因为闸门吃的是不同的东西

* **Stage A1（IC 层）** —— 闸门 A/B/C。跑 66 个配置，每个只算截面
  Spearman IC，不碰组合模拟。**便宜、不受成本与整手噪声干扰、
  多重检验也更干净。**
* **Stage A2（组合层）** —— 闸门 D/E。只对通过 A1 的因子跑。
  闸门 D 要「成本后超额年化 > 2%」、闸门 E 要「T+1 开盘版本的超额
  ≥ T 日收盘版本的 50%」—— 这两条必须走真实撮合才能算。

一次跑完一遍 66 配置并把每个配置都写进 `factor_trials_log.csv`，
**包括失败的**。N_trials 靠测量，不靠声称。

## 关键约定

* IC 只在**不重叠**的时点上采样（step = horizon），否则自相关会把
  t 值抬高。
* t 值用 Newey-West 校正。
* 用 `alpha` 与 `forward` 的截面 Spearman 相关，不是 Pearson ——
  因子分布重尾，Pearson 会被极端值主导。

用法：
    python analysis/factor_screen_20260920.py                 # 全部
    python analysis/factor_screen_20260920.py --factors F2_reversal_1m
    python analysis/factor_screen_20260920.py --random-control 20
"""

from __future__ import annotations

import argparse
import hashlib
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
OUT = ROOT / "analysis/factor_screen_20260920"
TRIALS = ROOT / "analysis/factor_trials_log.csv"

HORIZONS = (5, 20, 60)
TREATMENTS = ("raw", "size_neutral")

IS = ("20110101", "20181231")
OOS1 = ("20190101", "20221231")
OOS2 = ("20230101", "20260918")

N_TRIALS_PREREG = len(factor_lib.FACTORS) * len(TREATMENTS) * len(HORIZONS)
BONFERRONI_ALPHA = 0.05


def config_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def log_trial(row: dict) -> None:
    """把一次尝试追加进 trials_log。**包括失败的。**"""
    frame = pd.DataFrame([row])
    header = not TRIALS.exists()
    frame.to_csv(TRIALS, mode="a", header=header, index=False,
                 encoding="utf-8")


def forward_returns(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """前瞻 `horizon` 个交易日的总收益，`(code, date, forward)`。"""
    close = panel[["code", "date", "adj_close"]].sort_values(["code", "date"])
    close["forward"] = (close.groupby("code")["adj_close"]
                        .transform(lambda s: s.shift(-horizon) / s - 1.0))
    return close[["code", "date", "forward"]]


def merge_alpha_forward(alpha: pd.DataFrame,
                        forward: pd.DataFrame) -> pd.DataFrame:
    """把因子值与前瞻收益拼到一起。**每个配置只做一次。**

    2000 万行的 merge 约 10 秒，而每个因子 × 处理 × 持有期都要算 IS 和
    OOS-1 两个窗口 —— 如果每次都重新 merge，光这一步就是十几分钟。
    """
    return alpha.merge(forward, on=["code", "date"], how="inner")


def evaluate_ic(merged: pd.DataFrame,
                window: tuple[str, str], step: int) -> dict:
    """在给定区间上算 IC 序列的汇总统计。

    **`step` 必须等于前瞻窗口长度。** 在每个交易日都算 IC 会让相邻观测的
    前瞻窗口互相重叠，t 值因此虚高（重叠部分的噪声被重复计入）。
    按 `step = horizon` 采样，观测之间才是不重叠的。

    行数会因此少掉一个数量级，但换来的是一个**可信的显著性判断** ——
    这正是本次研究最该守住的东西。
    """
    start, end = window
    merged = merged[(merged["date"] >= start) & (merged["date"] <= end)]
    if merged.empty:
        return {}

    dates = set(sorted(merged["date"].unique())[::max(1, step)])
    sampled = merged[merged["date"].isin(dates)]

    # **一次 groupby，不要逐日布尔过滤。**
    # `merged[merged["date"] == date]` 每次都是对整表的全量扫描 ——
    # 2000 万行 × 400 个采样日就是几十亿次行比较，这是整个筛选的瓶颈。
    # 按日期分组一次拿到所有截面。
    rows = []
    for _, day in sampled.groupby("date", sort=True):
        value = metrics.spearman_ic(day.set_index("code")["alpha"],
                                    day.set_index("code")["forward"])
        if np.isfinite(value):
            rows.append(value)
    ic = pd.Series(rows)
    if len(ic) < 8:
        return {}
    return {
        "n_periods": int(len(ic)),
        "ic_mean": float(ic.mean()),
        "ic_median": float(ic.median()),
        "ic_std": float(ic.std(ddof=1)),
        "ic_positive": float((ic > 0).mean()),
        "t_nw": metrics.newey_west_t(ic.to_numpy(), lags=5),
        "ic_series": ic,
        "dates": dates,
    }


def yearly_sign_consistency(merged: pd.DataFrame,
                            step: int) -> tuple[float, dict]:
    """逐年的 IC 均值，返回 (同号比例, {年: ic})。同样按 `step` 采样。"""
    dates = set(sorted(merged["date"].unique())[::max(1, step)])
    sampled = merged[merged["date"].isin(dates)]
    sampled = sampled.assign(year=sampled["date"].str[:4])

    per_year = {}
    # 同样只 groupby 一次：按 (年, 日) 分组，避免在年内反复做全表扫描
    for (year, _), day in sampled.groupby(["year", "date"], sort=True):
        value = metrics.spearman_ic(day.set_index("code")["alpha"],
                                    day.set_index("code")["forward"])
        if np.isfinite(value):
            per_year.setdefault(year, []).append(value)
    per_year = {year: float(np.mean(values))
                for year, values in per_year.items() if values}
    if not per_year:
        return float("nan"), {}
    signs = [np.sign(v) for v in per_year.values()]
    dominant = np.sign(np.mean(signs))
    share = float(np.mean([s == dominant for s in signs]))
    return share, per_year


def run_grid(panel: PanelData, factor_keys: list[str], horizon_step: str,
             trials_log: bool = True) -> pd.DataFrame:
    """跑 Stage A1 的完整网格。"""
    raw = panel.long
    size = transforms.size_proxy(raw) if horizon_step != "none" else None
    forwards = {h: forward_returns(raw, h) for h in HORIZONS}

    results = []
    for key in factor_keys:
        t0 = time.time()
        try:
            base = factor_lib.compute(raw, key)
        except Exception as error:
            print(f"  [fail] {key}: {type(error).__name__}: {error}", flush=True)
            continue
        prior = factor_lib.FACTORS[key].prior

        for treatment in TREATMENTS:
            processed = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            if processed.empty:
                continue
            for horizon in HORIZONS:
                forward = forwards[horizon]
                merged_all = merge_alpha_forward(processed, forward)
                # step = horizon：观测之间不重叠
                stats_is = evaluate_ic(merged_all, IS, step=horizon)
                stats_oos1 = evaluate_ic(merged_all, OOS1, step=horizon)
                if not stats_is:
                    continue

                t_value = stats_is["t_nw"]
                # 因子函数已内置方向，所以预期 IC **恒为正**（见 Factor.expected_ic_sign）
                expected = factor_lib.FACTORS[key].expected_ic_sign
                sign_ok = np.sign(stats_is["ic_mean"]) == expected
                _, t_threshold = metrics.bonferroni_threshold(
                    N_TRIALS_PREREG, BONFERRONI_ALPHA)
                significant = (np.isfinite(t_value)
                               and abs(t_value) > t_threshold)

                merged_window = merged_all[
                    (merged_all["date"] >= IS[0]) & (merged_all["date"] <= IS[1])]
                share, per_year = yearly_sign_consistency(merged_window,
                                                          step=horizon)

                row = {
                    "factor": key,
                    "prior": prior,
                    "treatment": treatment,
                    "horizon": horizon,
                    "n_periods": stats_is["n_periods"],
                    "ic_mean": stats_is["ic_mean"],
                    "ic_median": stats_is["ic_median"],
                    "ic_positive": stats_is["ic_positive"],
                    "t_nw": t_value,
                    "sign_ok": bool(sign_ok),
                    "t_threshold": t_threshold,
                    "significant": bool(significant),
                    "yearly_sign_share": share,
                    "stable": bool(np.isfinite(share) and share >= 0.70),
                    "oos1_ic": stats_oos1.get("ic_mean", np.nan),
                    "oos1_sign_ok": bool(
                        stats_oos1 and
                        np.sign(stats_oos1["ic_mean"]) == expected),
                    "seconds": round(time.time() - t0, 1),
                }
                row["pass_a"] = bool(row["sign_ok"])
                row["pass_b"] = bool(row["significant"])
                row["pass_c"] = bool(row["stable"] and row["oos1_sign_ok"]
                                     and row["sign_ok"])
                results.append(row)

                if trials_log:
                    log_trial({
                        "run_id": f"{key}|{treatment}|{horizon}",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "config_hash": config_hash(
                            {"factor": key, "treatment": treatment,
                             "horizon": horizon, "window": IS}),
                        "stage": "A1_ic",
                        "IS_metric": row["ic_mean"],
                        "OOS1_metric": row["oos1_ic"],
                        "note": (f"t={t_value:.2f} sign_ok={sign_ok} "
                                 f"stable={row['stable']}"),
                    })
        print(f"  {key} 完成（{time.time() - t0:.0f} 秒）", flush=True)

    return pd.DataFrame(results)


def run_random_control(panel: PanelData, n: int, seed: int = 20260920) -> dict:
    """随机对照：同一套管线跑 `n` 个随机截面分数。

    每个随机分数在每个 (treatment, horizon) 上都跑一遍，记录 IS 上的
    最好表现。候选因子若落在该分布之内，它就只是运气。
    """
    rng = np.random.default_rng(seed)
    raw = panel.long
    forwards = {h: forward_returns(raw, h) for h in HORIZONS}
    codes = np.sort(raw["code"].unique())
    dates = np.sort(raw["date"].unique())

    best = []
    for i in range(n):
        # 随机分数：每个 (code, date) 一个均匀随机数
        sample = raw[["code", "date"]].copy()
        sample["alpha"] = rng.normal(size=len(sample))
        factor_values = transforms.preprocess(sample)
        for horizon in HORIZONS:
            merged = merge_alpha_forward(factor_values, forwards[horizon])
            stats_is = evaluate_ic(merged, IS, step=horizon)
            if stats_is:
                best.append({"draw": i, "horizon": horizon,
                             "ic_mean": stats_is["ic_mean"],
                             "abs_ic": abs(stats_is["ic_mean"])})
    frame = pd.DataFrame(best)
    if frame.empty:
        return {}
    return {
        "n_draws": int(frame["draw"].nunique()),
        "best_abs_ic": float(frame["abs_ic"].max()),
        "p95_abs_ic": float(frame["abs_ic"].quantile(0.95)),
        "median_abs_ic": float(frame["abs_ic"].median()),
    }


def build_report(results: pd.DataFrame, control: dict) -> str:
    lines = [
        "# Stage 3 因子筛选（IC 层）",
        "",
        f"预注册网格：**{N_TRIALS_PREREG} 个配置**"
        f"（{len(factor_lib.FACTORS)} 因子 × 2 处理 × 3 持有期）",
        f"实际完成：**{len(results)} 个**",
        "",
        "闸门定义见 `analysis/factor_prereg_20260920.md`。",
        "",
        "## 完整结果（含全部失败）",
        "",
        "| 因子 | 方向 | 处理 | 持有期 | IC 均值 | IC>0 | t(NW) | 符号对 | 显著 | 逐年同号 | OOS-1 IC | A | B | C |",
        "|---|---|---|---:|---:|---:|---:|:---:|:---:|---:|---:|:---:|:---:|:---:|",
    ]
    for _, r in results.sort_values("ic_mean", ascending=False).iterrows():
        lines.append(
            f"| {r['factor']} | {r['prior']} | {r['treatment']} | "
            f"{r['horizon']} | {r['ic_mean']:+.4f} | {r['ic_positive']:.0%} | "
            f"{r['t_nw']:+.2f} | {'✓' if r['sign_ok'] else '✗'} | "
            f"{'✓' if r['significant'] else '✗'} | "
            f"{r['yearly_sign_share']:.0%} | {r['oos1_ic']:+.4f} | "
            f"{'✓' if r['pass_a'] else '✗'} | "
            f"{'✓' if r['pass_b'] else '✗'} | "
            f"{'✓' if r['pass_c'] else '✗'} |")

    threshold = results["t_threshold"].iloc[0] if len(results) else float("nan")
    lines.extend([
        "",
        f"Bonferroni 阈值（`n_trials={N_TRIALS_PREREG}`，`α=0.05`）："
        f"**|t| > {threshold:.2f}**",
        "",
        "## 随机对照",
        "",
    ])
    if control:
        lines.extend([
            f"用同一套管线跑 **{control['n_draws']} 个随机截面分数**：",
            "",
            f"- 随机因子的 `|IC|` 最大：**{control['best_abs_ic']:.4f}**",
            f"- 95 分位：{control['p95_abs_ic']:.4f}",
            f"- 中位：{control['median_abs_ic']:.4f}",
            "",
            "**任何 `|IC| 均值` 不超过随机对照最大值的候选，判定为运气。**",
        ])
    else:
        lines.append("（未跑随机对照）")

    survivors = results[results["pass_a"] & results["pass_b"] & results["pass_c"]]
    lines.extend([
        "",
        "## 通过 A/B/C 的配置",
        "",
    ])
    if survivors.empty:
        lines.extend([
            "**没有配置同时通过三道闸门。**",
            "",
            "按预注册的决策规则第 6 条：**直接进入收尾流程**，"
            "不放宽阈值、不事后加因子、不改符号。",
        ])
    else:
        lines.extend([
            f"共 **{len(survivors)}** 个配置通过，进入 Stage A2（组合层闸门 D/E）：",
            "",
        ])
        for _, r in survivors.iterrows():
            lines.append(f"- `{r['factor']}` / {r['treatment']} / "
                         f"{r['horizon']} 日：IC {r['ic_mean']:+.4f}，"
                         f"t {r['t_nw']:+.2f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factors", nargs="*", default=None)
    parser.add_argument("--random-control", type=int, default=20)
    parser.add_argument("--no-trials-log", action="store_true")
    parser.add_argument("--panel", default=str(PANEL),
                        help="换一个面板文件（试运行用）")
    parser.add_argument("--out", default=str(OUT),
                        help="换一个输出目录（试运行用）")
    args = parser.parse_args()

    panel_path = Path(args.panel)
    out_dir = Path(args.out)
    if not panel_path.exists():
        raise SystemExit(f"缺 {panel_path} —— 先跑 panel_build_20260920.py")

    print("载入面板 ...", flush=True)
    panel = PanelData.load(panel_path)
    print(f"  {len(panel.long):,} 行，{len(panel.codes):,} 只", flush=True)

    keys = args.factors or list(factor_lib.FACTORS)
    print(f"跑 {len(keys)} 个因子 × {len(TREATMENTS)} 处理 × "
          f"{len(HORIZONS)} 持有期 = {len(keys) * 6} 个配置 ...", flush=True)
    results = run_grid(panel, keys, "5",
                       trials_log=not args.no_trials_log)

    control = {}
    if args.random_control:
        print(f"随机对照 {args.random_control} 次 ...", flush=True)
        control = run_random_control(panel, args.random_control)

    out_dir.mkdir(parents=True, exist_ok=True)
    results.drop(columns=["ic_series"], errors="ignore").to_csv(
        out_dir / "ic_grid.csv", index=False, encoding="utf-8")
    (out_dir / "README.md").write_text(build_report(results, control),
                                       encoding="utf-8")
    (out_dir / "random_control.json").write_text(
        json.dumps(control, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out_dir / 'README.md'}")
    print(f"→ {out_dir / 'ic_grid.csv'}")
    if not args.no_trials_log:
        print(f"→ {TRIALS}")


if __name__ == "__main__":
    main()
