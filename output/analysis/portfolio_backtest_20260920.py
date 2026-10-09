"""Stage 4：组合回测、四线消融、成本敏感度。

## 四线消融（仓库规则强制）

| 线 | 选股 | 择时 |
|---|---|---|
| A | 关 | 关 |
| B | 关 | 开 |
| C | 开 | 开 |
| D | 开 | 关 |

归因：选股 = D − A；择时 = B − A；交互 = C − D − B + A。

## 「选股关」不等于「等权买入全市场」

100 万资金买不下 3000-5000 只股票（每只 200 元，一手都买不起）。
所以「关选股」实现为**同一套机制下的随机 N 只**：
同样的持仓数、同样的调仓周期、同样的缓冲带与成本，只是分数变成随机数。

这比「等权全市场」更严格 —— 它把「机制本身」（整手、T+1、涨跌停、
再平衡漂移）也一并控制掉了，剩下的差异才是信号的贡献。
多 seed 取平均。

## 必须报 beta 调整后的 alpha

择时层改变**平均仓位**，朴素比较会被 beta 污染 —— 平均仓位 60% 的线
在牛市里看起来「增加了收益」，其实只是承担了更少的市场暴露。
所以每条线都报：平均 gross exposure、对 `000300.SH` 回归的 alpha 与 t 值、
逐年表、`corr(当年基准收益, 当年择时超额)`。

用法：
    python analysis/portfolio_backtest_20260920.py --factor F2_reversal_1m
    python analysis/portfolio_backtest_20260920.py --ablation
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import factors as factor_lib                 # noqa: E402
from portfolio import metrics, timing, transforms           # noqa: E402
from portfolio.costs import CostModel                       # noqa: E402
from portfolio.engine import PortfolioEngine                # noqa: E402
from portfolio.panel import PanelData                       # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
OUT = ROOT / "analysis/portfolio_20260920"
INDEX_CACHE = ROOT / "analysis/timing_index_20260920/index_daily.parquet"

IS = ("20110101", "20181231")
OOS1 = ("20190101", "20221231")
OOS2 = ("20230101", "20260918")

BENCHMARK = "000300.SH"
SLIPPAGE_GRID = (0.0, 5.0, 15.0, 30.0)
RANDOM_SEEDS = (11, 22, 33, 44, 55)


def load_benchmark(dates) -> pd.Series:
    """基准日收益。取不到就返回空 Series（beta 调整会被跳过）。"""
    if not INDEX_CACHE.exists():
        return pd.Series(dtype=float)
    frame = pd.read_parquet(INDEX_CACHE)
    sub = frame[frame["code"] == BENCHMARK].sort_values("date")
    if sub.empty:
        return pd.Series(dtype=float)
    ret = sub.set_index("date")["close"].astype(float).pct_change()
    return ret.reindex(dates).dropna()


def load_timing_index(code: str = BENCHMARK) -> pd.DataFrame:
    frame = pd.read_parquet(INDEX_CACHE)
    return (frame[frame["code"] == code]
            .drop(columns=["code"]).sort_values("date").reset_index(drop=True))


def random_alpha(panel: PanelData, seed: int):
    """随机分数，「关选股」线用。**机制全同，只是没有信号。**"""
    rng = np.random.default_rng(seed)
    frame = panel.long[["code", "date"]].copy()
    frame["alpha"] = rng.normal(size=len(frame))
    frame = transforms.preprocess(frame)
    return lambda p: frame


def signal_alpha(panel: PanelData, key: str, treatment: str):
    """返回 `alpha_fn`，与 `random_alpha` 的返回类型保持一致。

    引擎要求 `alpha_fn(panel) -> 长表`，所以这里必须返回**闭包**而不是
    直接的 DataFrame。两个工厂返回类型不一致的话，A/B 线（随机）能跑、
    C/D 线（信号）就崩 —— 而且是在跑了 8 分钟之后才崩。
    """
    raw = panel.long
    base = factor_lib.compute(raw, key)
    size = transforms.size_proxy(raw) if treatment == "size_neutral" else None
    processed = transforms.preprocess(
        base, size=size, neutralize=(treatment == "size_neutral"))
    return lambda p: processed


def timing_fn(panel: PanelData, variant: str, index_code: str):
    index = load_timing_index(index_code)
    exposure = timing.regime_series(index, variant)
    return lambda p: exposure


def make_engine(panel: PanelData, slippage_bps: float, n_holdings: int,
                rebalance_rule: str, execution: str = "open"):
    costs = CostModel(slippage_bps=slippage_bps)
    return PortfolioEngine(
        panel, initial_capital=1_000_000.0, costs=costs,
        n_holdings=n_holdings, rebalance_rule=rebalance_rule,
        buffer_entry=0.20, buffer_exit=0.40, weight_scheme="equal",
        execution=execution)


def summarize_equity(result, benchmark: pd.Series) -> dict:
    equity = result.equity
    bench = benchmark.reindex(equity.index).fillna(0.0)
    stats = metrics.summarize(equity, benchmark=bench)
    stats["avg_gross_exposure"] = result.diagnostics.get(
        "avg_gross_exposure", 1.0)
    stats["avg_turnover"] = result.diagnostics.get("avg_turnover", 0.0)
    stats["total_fees"] = result.diagnostics.get("total_fees", 0.0)
    stats["n_rebalances"] = result.diagnostics.get("n_rebalances", 0)
    return stats


def yearly_vs_benchmark(result, benchmark: pd.Series) -> list[tuple]:
    equity = result.equity
    bench = benchmark.reindex(equity.index).fillna(0.0)
    strategy = equity.pct_change().dropna()
    bench = bench.reindex(strategy.index)
    rows = []
    for year, sub in strategy.groupby(strategy.index.str[:4]):
        b = bench.loc[sub.index]
        s_ret = float((1 + sub).prod() - 1)
        b_ret = float((1 + b).prod() - 1)
        rows.append((year, b_ret, s_ret, s_ret - b_ret))
    return rows


def run_line(panel: PanelData, alpha_factory, timing_factory,
             benchmark: pd.Series, slippage_bps: float, n_holdings: int,
             rebalance_rule: str, start: str, end: str,
             execution: str = "open") -> dict:
    engine = make_engine(panel, slippage_bps, n_holdings, rebalance_rule,
                         execution=execution)
    # **把工厂本身传进去，不要先调用它。** `engine.run` 内部要调
    # `alpha_fn(panel)`；如果这里先调一次，传进去的就是 DataFrame 而不是
    # 可调用对象，引擎再调就 `TypeError: 'DataFrame' object is not callable`。
    #
    # 这个 bug 只在 C/D 线（信号）暴露，因为 A/B 线的 `random_alpha`
    # 返回的是闭包 —— 闭包被调用一次之后仍然是闭包，恰好绕过。
    result = engine.run(alpha_factory, timing_fn=timing_factory,
                        start=start, end=end)
    return {"result": result, "stats": summarize_equity(result, benchmark),
            "yearly": yearly_vs_benchmark(result, benchmark)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor", default="F2_reversal_1m")
    parser.add_argument("--treatment", default="size_neutral")
    parser.add_argument("--n-holdings", type=int, default=30)
    parser.add_argument("--rebalance", default="M")
    parser.add_argument("--timing-variant", default="rescaled")
    parser.add_argument("--window", default="IS", choices=["IS", "OOS1", "OOS2"])
    parser.add_argument("--universe", default="all",
                        help="all | csi500 | csi500main —— 限定股票池")
    parser.add_argument("--bounds", default=None,
                        help="覆盖区间，形如 20230901,20260918")
    parser.add_argument("--ablation", action="store_true")
    parser.add_argument("--slippage-grid", action="store_true")
    args = parser.parse_args()

    if not PANEL.exists():
        raise SystemExit(f"缺 {PANEL}")

    bounds = {"IS": IS, "OOS1": OOS1, "OOS2": OOS2}[args.window]
    if args.bounds:
        bounds = tuple(args.bounds.split(","))
    start, end = bounds

    # **向前留出预热期。** 直接从 start 切面板会让长周期因子在窗口前段
    # 全为空（F1/F5 需要 250 根日线）—— 引擎在 start 之后的前几个月
    # 根本没有 alpha 可用，回测结果会莫名其妙地差。
    # 面板多载一段，引擎仍然只跑 [start, end]。
    warmup = str(int(start[:4]) - 2) + start[4:]
    print(f"载入面板（预热自 {warmup}）...", flush=True)
    panel = PanelData.load(PANEL).between(warmup, end)
    # 显式映射，不用字符串拼接 —— 拼接会在池子改名时静默指向不存在的文件，
    # 或者更糟：指向一个同名的旧文件而看不出错。
    universe_files = {
        "csi500": "csi500_universe.json",
        "csi500main": "csi500_main_universe.json",
    }
    if args.universe in universe_files:
        codes = set(json.loads(
            (ROOT / "analysis/panel_20260920" / universe_files[args.universe])
            .read_text(encoding="utf-8"))["covered"])
        long = panel.long[panel.long["code"].isin(codes)]
        panel = PanelData.from_frame(long.reset_index(drop=True))
    print(f"  股票池 {args.universe}，{len(panel.long):,} 行，"
          f"{len(panel.codes):,} 只", flush=True)

    benchmark = load_benchmark(panel.dates)

    lines = [f"# Stage 4 组合回测（{args.window} {start}~{end}）", ""]
    summary_rows = []

    if args.ablation:
        print("四线消融 ...", flush=True)
        signal = signal_alpha(panel, args.factor, args.treatment)
        # `timing_fn(...)` **已经返回闭包**，不能再包一层 lambda ——
        # 包了的话引擎拿到的是一个「返回闭包的函数」，而不是「返回仓位的函数」，
        # 后续 `gross.get(date)` 会在一个 lambda 上调用而报错。
        timing_f = timing_fn(panel, args.timing_variant, BENCHMARK)

        # A：随机选股、不择时。多 seed 取平均，压掉随机性
        print("  A 线（随机选股，不择时）...", flush=True)
        a_stats = []
        for seed in RANDOM_SEEDS:
            out = run_line(panel, random_alpha(panel, seed), None,
                           benchmark, 5.0, args.n_holdings, args.rebalance,
                           start, end)
            a_stats.append(out["stats"])
        a_mean = {k: float(np.nanmean([s.get(k, np.nan) for s in a_stats]))
                  for k in a_stats[0]}

        print("  B 线（随机选股，择时）...", flush=True)
        b_stats = []
        for seed in RANDOM_SEEDS:
            out = run_line(panel, random_alpha(panel, seed),
                           timing_f, benchmark, 5.0, args.n_holdings,
                           args.rebalance, start, end)
            b_stats.append(out["stats"])
        b_mean = {k: float(np.nanmean([s.get(k, np.nan) for s in b_stats]))
                  for k in b_stats[0]}

        print("  C 线（信号选股，择时）...", flush=True)
        c = run_line(panel, signal, timing_f, benchmark, 5.0,
                     args.n_holdings, args.rebalance, start, end)

        print("  D 线（信号选股，不择时）...", flush=True)
        d = run_line(panel, signal, None, benchmark, 5.0,
                     args.n_holdings, args.rebalance, start, end)

        lines.extend([
            "## 四线消融", "",
            "「关选股」= 同一机制下的**随机 N 只**（5 个 seed 取平均），"
            "不是「等权全市场」—— 后者在 100 万资金下物理上不可行。",
            "",
            "| 线 | 选股 | 择时 | 年化 | 夏普 | 最大回撤 | 平均仓位 | 年化换手 |",
            "|---|---|---|---:|---:|---:|---:|---:|",
            f"| A | 关 | 关 | {a_mean.get('annual_return', float('nan')):+.2%} | "
            f"{a_mean.get('sharpe', float('nan')):.3f} | "
            f"{a_mean.get('max_drawdown', float('nan')):.2%} | "
            f"{a_mean.get('avg_gross_exposure', 1.0):.1%} | "
            f"{a_mean.get('avg_turnover', 0.0) * 12:.1%} |",
            f"| B | 关 | 开 | {b_mean.get('annual_return', float('nan')):+.2%} | "
            f"{b_mean.get('sharpe', float('nan')):.3f} | "
            f"{b_mean.get('max_drawdown', float('nan')):.2%} | "
            f"{b_mean.get('avg_gross_exposure', 1.0):.1%} | "
            f"{b_mean.get('avg_turnover', 0.0) * 12:.1%} |",
            f"| C | 开 | 开 | {c['stats'].get('annual_return', float('nan')):+.2%} | "
            f"{c['stats'].get('sharpe', float('nan')):.3f} | "
            f"{c['stats'].get('max_drawdown', float('nan')):.2%} | "
            f"{c['stats'].get('avg_gross_exposure', 1.0):.1%} | "
            f"{c['stats'].get('avg_turnover', 0.0) * 12:.1%} |",
            f"| D | 开 | 关 | {d['stats'].get('annual_return', float('nan')):+.2%} | "
            f"{d['stats'].get('sharpe', float('nan')):.3f} | "
            f"{d['stats'].get('max_drawdown', float('nan')):.2%} | "
            f"{d['stats'].get('avg_gross_exposure', 1.0):.1%} | "
            f"{d['stats'].get('avg_turnover', 0.0) * 12:.1%} |",
            "",
        ])

        bench_ret = benchmark.reindex(panel.dates).fillna(0.0)
        bench_stats = metrics.summarize((1 + bench_ret).cumprod(),
                                        benchmark=bench_ret)
        lines.extend([
            f"基准 `{BENCHMARK}`：年化 {bench_stats['annual_return']:+.2%}、"
            f"夏普 {bench_stats['sharpe']:.3f}、回撤 {bench_stats['max_drawdown']:.2%}",
            "",
            "## 2×2 归因（年化超额，成本后）",
            "",
            "| 效应 | 数值 |",
            "|---|---:|",
            f"| 选股（D − A） | "
            f"{d['stats'].get('annual_return', np.nan) - a_mean.get('annual_return', np.nan):+.2%} |",
            f"| 择时（B − A） | "
            f"{b_mean.get('annual_return', np.nan) - a_mean.get('annual_return', np.nan):+.2%} |",
            f"| 交互（C − D − B + A） | "
            f"{c['stats'].get('annual_return', np.nan) - d['stats'].get('annual_return', np.nan) - b_mean.get('annual_return', np.nan) + a_mean.get('annual_return', np.nan):+.2%} |",
            "",
        ])

        # beta 调整
        for label, out in (("C", c), ("D", d)):
            alpha_val = out["stats"].get("alpha", float("nan"))
            alpha_t = out["stats"].get("alpha_t", float("nan"))
            lines.append(f"- **{label} 线** 对基准的 beta 调整后 alpha："
                         f"**{alpha_val:+.2%}/年**（t = {alpha_t:+.2f}）")
        lines.append("")

        # 逐年 + corr
        lines.extend(["## 逐年（C 线 vs 基准）", "",
                      "| 年份 | 基准 | 策略 | 超额 |", "|---|---:|---:|---:|"])
        yearly = c["yearly"]
        for year, b, s, e in yearly:
            lines.append(f"| {year} | {b:+.1%} | {s:+.1%} | **{e:+.1%}** |")
        if len(yearly) >= 4:
            bench_series = pd.Series({y: b for y, b, _, _ in yearly})
            excess = pd.Series({y: e for y, _, _, e in yearly})
            corr = float(np.corrcoef(bench_series, excess)[0, 1])
            if corr < -0.7:
                verdict = (
                    "接近 −1 → 策略超额主要来自「大盘跌的年份少亏」，"
                    "那是保险不是收益引擎。在「收益最高」的目标下这是一个"
                    "**取舍，必须由你拍板**，不能替你把回撤说成收益。")
            else:
                verdict = ("没有明显接近 −1，策略超额不是纯粹的"
                           "「跌年少亏」形态。")
            lines.extend([
                "",
                f"**corr(当年基准收益, 当年策略超额) = {corr:+.2f}**"
                f"（n = {len(yearly)} 年）",
                "",
                verdict,
            ])

    if args.slippage_grid:
        print("成本敏感度 ...", flush=True)
        signal = signal_alpha(panel, args.factor, args.treatment)
        lines.extend(["", "## 成本敏感度", "",
                      "| 滑点(bp/单边) | 年化 | 夏普 | 最大回撤 | 总费用 |",
                      "|---|---:|---:|---:|---:|"])
        for bps in SLIPPAGE_GRID:
            out = run_line(panel, signal, None, benchmark, bps,
                           args.n_holdings, args.rebalance, start, end)
            st = out["stats"]
            lines.append(
                f"| {bps:.0f} | {st.get('annual_return', float('nan')):+.2%} | "
                f"{st.get('sharpe', float('nan')):.3f} | "
                f"{st.get('max_drawdown', float('nan')):.2%} | "
                f"{st.get('total_fees', 0.0):,.0f} |")

    print("执行口径对照（闸门 E）...", flush=True)
    signal = signal_alpha(panel, args.factor, args.treatment)
    open_out = run_line(panel, signal, None, benchmark, 5.0, args.n_holdings,
                        args.rebalance, start, end, execution="open")
    close_out = run_line(panel, signal, None, benchmark, 5.0, args.n_holdings,
                         args.rebalance, start, end, execution="close")

    open_excess = open_out["stats"].get("excess_annual", float("nan"))
    close_excess = close_out["stats"].get("excess_annual", float("nan"))
    if np.isfinite(close_excess) and abs(close_excess) > 1e-9:
        keep_ratio = open_excess / close_excess
    else:
        keep_ratio = float("nan")

    # **两个超额都为负时，比值毫无意义。**
    # −5.09% / −3.63% = 1.40，按「≥ 0.5」会判成通过 —— 但那只是
    # 「两个负数相除得正数」。闸门 E 要问的是「可实现版本还剩下多少正超额」，
    # 前提是那个超额本身为正。
    positive = np.isfinite(open_excess) and open_excess > 0
    gate_e = bool(positive and np.isfinite(keep_ratio) and keep_ratio >= 0.5)

    lines.extend([
        "", "## 执行口径对照（闸门 E）", "",
        "**闸门 E**：T+1 开盘成交版本的超额必须 ≥ T 日收盘成交版本的 50%。",
        "否则说明 alpha 全在隔夜跳空里 —— 那部分在实盘吃不到。",
        "",
        "| 执行口径 | 年化 | 超额（对基准） | 夏普 |",
        "|---|---:|---:|---:|",
        f"| T+1 开盘（可实现） | "
        f"{open_out['stats'].get('annual_return', float('nan')):+.2%} | "
        f"{open_excess:+.2%} | "
        f"{open_out['stats'].get('sharpe', float('nan')):.3f} |",
        f"| T 日收盘（不可实现） | "
        f"{close_out['stats'].get('annual_return', float('nan')):+.2%} | "
        f"{close_excess:+.2%} | "
        f"{close_out['stats'].get('sharpe', float('nan')):.3f} |",
        "",
        (f"**保留比例 = {keep_ratio:.0%}** → 闸门 E "
         f"{'✅ 通过' if gate_e else '❌ 未通过'}")
        if positive else
        ("**T+1 开盘版本的超额为负** —— 闸门 E 判定无意义，直接标 ❌。"
         "（两个负数相除会得正数，那不是「保留比例」）"),
        "",
        "T 日收盘版本在 15:00 前不可知收盘价，**不是可实现假设**，"
        "只作为「alpha 有多少藏在隔夜跳空里」的度量。",
    ])

    # **按股票池分目录。** 原来所有池子都往 `OUT/README_{window}.md` 写，
    # 换一个 `--universe` 重跑就会把上一个池子的报告**静默覆盖** ——
    # 而结论文档还在引用那个文件。信息不自洽，而且看不出来。
    out_dir = OUT if args.universe == "all" else OUT / args.universe
    out_dir.mkdir(parents=True, exist_ok=True)
    if summary_rows:
        pd.DataFrame(summary_rows).to_csv(out_dir / "summary.csv", index=False,
                                          encoding="utf-8")
    (out_dir / f"README_{args.window}.md").write_text("\n".join(lines),
                                                      encoding="utf-8")
    print(f"\n→ {out_dir / f'README_{args.window}.md'}")


if __name__ == "__main__":
    main()
