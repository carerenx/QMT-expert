"""Stage 2：大盘择时层的独立评测 —— 先在指数上，再进组合。

## 这一步在测什么

`portfolio/timing.py` 提供三种把指数 regime 映射成总仓位的规则。
这个脚本**单独**评测它们，不掺选股，回答一个问题：

> 光靠调总仓位，能不能改善风险调整后收益？

## 为什么必须单独测，而不是直接塞进组合

塞进组合之后，择时的效果会和选股混淆，**而且会被 beta 污染** ——
一个平均仓位 60% 的策略在牛市里看起来「增加了收益」，其实它只是
承担了更少的市场暴露。所以每条线都必须报：

* **平均 gross exposure**
* **对 `sh.000300` 回归后的 alpha 与 t 值**
* 逐年表 + `corr(当年基准收益, 当年择时超额)`

仓库历史上那个 `corr = −0.98` 的结论就是这么来的：择时超额的唯一
规律是「大盘跌的年份它值钱」，那是**事后统计量，事前不可知**。
如果这次仍然是 −0.98，择时层就是**保险不是收益引擎**，
在「收益最高」的目标下应当被拒绝 —— 这个取舍要摆给用户拍板。

## 判定点（预注册）

只有 **≥2/3 指数上改善 IS Sharpe 且不依赖单一年份**，才提升为基准；
否则基准退回 `gross=1.0`，择时记 `无效`。

**这是很可能发生的结果，脚本必须允许它发生。**

用法：
    python analysis/timing_index_study_20260920.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import metrics, timing                       # noqa: E402


OUT = ROOT / "analysis/timing_index_20260920"
CACHE = OUT / "index_daily.parquet"

PRIMARY = "000300.SH"
ROBUSTNESS = ["000905.SH", "000852.SH"]
ALL_INDICES = [PRIMARY] + ROBUSTNESS

IS = ("20110101", "20181231")
OOS1 = ("20190101", "20221231")
OOS2 = ("20230101", "20260918")

BASE = 1.0          # 基准：一直满仓


def load_indices() -> dict[str, pd.DataFrame]:
    if CACHE.exists():
        frame = pd.read_parquet(CACHE)
        return {code: sub.reset_index(drop=True)
                for code, sub in frame.groupby("code")}
    print("从桥接取指数日线 ...", flush=True)
    data = timing.load_index_from_bridge(ALL_INDICES, "20100101", "20260918")
    parts = []
    for code, frame in data.items():
        frame = frame.copy()
        frame["code"] = code
        parts.append(frame)
    if not parts:
        raise SystemExit("指数数据为空")
    merged = pd.concat(parts, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(CACHE, index=False)
    return {code: sub.drop(columns=["code"]).reset_index(drop=True)
            for code, sub in merged.groupby("code")}


def exposure_returns(index: pd.DataFrame, exposure: pd.Series) -> pd.Series:
    """按 `exposure` 缩放指数收益。仓位信号用 T 日收盘算，T+1 生效。

    实现上是把 exposure 前移一天 —— 这是「T 收盘算信号、T+1 才持有」
    的最小正确表达，也是唯一能避免未来函数的写法。
    """
    close = index.set_index("date")["close"].astype(float).sort_index()
    ret = close.pct_change()
    held = exposure.reindex(ret.index).shift(1)
    return (held * ret).dropna()


def window(series: pd.Series, bounds: tuple[str, str]) -> pd.Series:
    start, end = bounds
    return series[(series.index >= start) & (series.index <= end)]


def evaluate(index: pd.DataFrame, exposure: pd.Series,
             bounds: tuple[str, str]) -> dict:
    ret = window(exposure_returns(index, exposure), bounds)
    bench = window(index.set_index("date")["close"].astype(float)
                   .pct_change().dropna(), bounds)
    if len(ret) < 60:
        return {}
    equity = (1.0 + ret).cumprod()
    bench_equity = (1.0 + bench.reindex(ret.index).fillna(0.0)).cumprod()
    out = metrics.summarize(equity, benchmark=bench.reindex(ret.index))
    out["avg_exposure"] = float(
        exposure.reindex(ret.index).shift(1).mean())
    out["max_drawdown"] = metrics.max_drawdown(equity)
    out["bench_max_drawdown"] = metrics.max_drawdown(bench_equity)
    return out


def yearly_table(index: pd.DataFrame, exposure: pd.Series,
                 bounds: tuple[str, str]) -> list[tuple]:
    ret = window(exposure_returns(index, exposure), bounds)
    bench = window(index.set_index("date")["close"].astype(float)
                   .pct_change().dropna(), bounds).reindex(ret.index)
    rows = []
    for year, sub in ret.groupby(ret.index.str[:4]):
        b = bench.loc[sub.index]
        rows.append((year, float((1 + b).prod() - 1),
                     float((1 + sub).prod() - 1),
                     float(((1 + sub).prod() - 1) - ((1 + b).prod() - 1))))
    return rows


def main() -> None:
    indices = load_indices()
    print(f"指数：{list(indices)}", flush=True)

    lines = [
        "# Stage 2：大盘择时层独立评测",
        "",
        "对指数收益按择时总仓位缩放，**不掺选股**。",
        "仓位信号用 T 日收盘算、**T+1 生效**（`exposure.shift(1)`）。",
        "",
        "三种变体：",
        "",
        "| 变体 | 规则 |",
        "|---|---|",
        "| `as_is` | 忠实复用 `classify_regime`：强牛 0.75 / 牛 0.60 / 震荡 0.45 / 熊 0.30 |",
        "| `rescaled` | 重标定到 0~1：1.00 / 0.60 / 0.30 / **0.00** |",
        "| `binary` | `close > MA200 → 1.0 else 0.0`（Faber 2007） |",
        "",
        "⚠️ `classify_regime` 的地板是 0.30，**永远不可能真正空仓** —— "
        "`rescaled` 才是能空仓的那个。",
        "",
    ]

    summaries = []
    for code, index in indices.items():
        lines.extend([f"## {code}", ""])
        for variant in timing.VARIANTS:
            exposure = timing.regime_series(index, variant)
            if exposure.empty:
                lines.extend([f"（{variant} 无信号）", ""])
                continue

            is_stats = evaluate(index, exposure, IS)
            oos_stats = evaluate(index, exposure, OOS1)
            base_is = evaluate(index, pd.Series(1.0, index=index["date"]), IS)
            if not is_stats:
                continue

            sharpe_delta = is_stats["sharpe"] - base_is["sharpe"]
            summaries.append({
                "index": code, "variant": variant,
                "is_sharpe": is_stats["sharpe"],
                "base_sharpe": base_is["sharpe"],
                "sharpe_delta": sharpe_delta,
                "is_return": is_stats["annual_return"],
                "base_return": base_is["annual_return"],
                "max_dd": is_stats["max_drawdown"],
                "base_max_dd": base_is["bench_max_drawdown"],
                "avg_exposure": is_stats["avg_exposure"],
                "alpha": is_stats.get("alpha"),
                "alpha_t": is_stats.get("alpha_t"),
                "oos_sharpe": oos_stats.get("sharpe"),
            })

            lines.extend([
                f"### {variant}",
                "",
                "| 指标 | 择时 | 一直满仓 | 差 |",
                "|---|---:|---:|---:|",
                f"| IS 年化 | {is_stats['annual_return']:+.2%} | "
                f"{base_is['annual_return']:+.2%} | "
                f"{is_stats['annual_return'] - base_is['annual_return']:+.2%} |",
                f"| IS 夏普 | {is_stats['sharpe']:.3f} | "
                f"{base_is['sharpe']:.3f} | **{sharpe_delta:+.3f}** |",
                f"| IS 最大回撤 | {is_stats['max_drawdown']:.2%} | "
                f"{base_is['bench_max_drawdown']:.2%} | "
                f"{is_stats['max_drawdown'] - base_is['bench_max_drawdown']:+.2%} |",
                f"| 平均仓位 | {is_stats['avg_exposure']:.1%} | 100% | |",
                f"| 对基准 alpha | {is_stats.get('alpha', float('nan')):+.2%} |"
                f" | t = {is_stats.get('alpha_t', float('nan')):+.2f} |",
                "",
                "逐年（超额 = 择时 − 满仓）：",
                "",
                "| 年份 | 满仓收益 | 择时收益 | 超额 |",
                "|---|---:|---:|---:|",
            ])
            yearly = yearly_table(index, exposure, IS)
            for year, b, s, e in yearly:
                lines.append(f"| {year} | {b:+.1%} | {s:+.1%} | **{e:+.1%}** |")

            if len(yearly) >= 4:
                bench_series = pd.Series({y: b for y, b, _, _ in yearly})
                excess_series = pd.Series({y: e for y, _, _, e in yearly})
                corr = float(np.corrcoef(bench_series, excess_series)[0, 1])
                lines.extend([
                    "",
                    f"**corr(当年满仓收益, 当年择时超额) = {corr:+.2f}**"
                    f"（n = {len(yearly)} 年）",
                    "",
                    "若这个相关接近 −1，说明择时超额的唯一规律是"
                    "「大盘跌的年份它值钱」—— 那是**事后统计量，事前不可知**，"
                    "择时层就是保险而不是收益引擎。" if corr < -0.7
                    else "这个相关没有明显接近 −1，择时不是纯粹的"
                         "「跌年才值钱」形态。",
                ])
            lines.append("")

    frame = pd.DataFrame(summaries)
    OUT.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT / "summary.csv", index=False, encoding="utf-8")

    # ── 预注册判定 ──
    lines.extend(["## 预注册判定", ""])
    if frame.empty:
        lines.append("没有产生任何有效结果。")
    else:
        improved = frame[frame["sharpe_delta"] > 0]
        per_variant = (improved.groupby("variant")["index"]
                       .nunique().to_dict())
        lines.extend([
            "**判定标准（预注册）**：≥2/3 指数上改善 IS 夏普，且不依赖单一年份。",
            "",
            "| 变体 | 改善的指数数 | 通过 |",
            "|---|---:|:---:|",
        ])
        for variant in timing.VARIANTS:
            count = per_variant.get(variant, 0)
            lines.append(f"| {variant} | {count} / {len(indices)} | "
                         f"{'✓' if count >= 2 else '✗'} |")
        lines.extend([
            "",
            "**结果不由脚本自动写进策略配置** —— 通过与否都要人工确认，"
            "因为「不依赖单一年份」这一条需要读逐年表才能判断。",
            "",
            "若全部未通过：基准退回 `gross=1.0`，择时层记 `无效`，"
            "策略退化为纯选股。**这是完全可以接受的结果。**",
        ])

    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT / 'README.md'}")
    print(f"→ {OUT / 'summary.csv'}")
    if not frame.empty:
        print()
        print(frame[["index", "variant", "sharpe_delta", "max_dd",
                     "base_max_dd", "avg_exposure"]].to_string(index=False))


if __name__ == "__main__":
    main()
