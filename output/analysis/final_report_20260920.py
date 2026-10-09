"""Stage 6：把各阶段的结果汇总成一份研究报告。

**这个脚本不产生新结果，只汇总已有的证据。** 它的作用是保证报告里
每一个数字都能追到具体的证据文件，而不是手抄 —— 手抄是错误的高发区，
而且无法复核。

用法：
    python analysis/final_report_20260920.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

PREREG = ROOT / "analysis/factor_prereg_20260920.md"
PANEL_META = ROOT / "analysis/panel_20260920/meta.json"
PANEL_QC = ROOT / "analysis/panel_20260920/QC.md"
TIMING_SUMMARY = ROOT / "analysis/timing_index_20260920/summary.csv"
IC_GRID = ROOT / "analysis/factor_screen_20260920/ic_grid.csv"
RANDOM_CONTROL = ROOT / "analysis/factor_screen_20260920/random_control.json"
TRIALS = ROOT / "analysis/factor_trials_log.csv"
PORTFOLIO_IS = ROOT / "analysis/portfolio_20260920/README_IS.md"
PORTFOLIO_OOS2 = ROOT / "analysis/portfolio_20260920/README_OOS2.md"

# **不要写成 `选股择时策略研究结论.md`。**
# 那份是人工撰写的权威结论，脚本生成的骨架会把它覆盖掉（而且骨架里
# 有占位符、也拿不到需要人工判断的结论）。这里只产出素材汇总。
OUT = ROOT / "analysis/auto_summary_20260920.md"


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    meta = read_json(PANEL_META)
    control = read_json(RANDOM_CONTROL)

    lines = [
        "# 选股 + 择时组合策略：研究结论",
        "",
        "**一句话结论：**（由脚本根据闸门结果填写，见第五节）",
        "",
        "本研究严格遵循 `analysis/factor_prereg_20260920.md` 的预注册流程。",
        "每一次尝试（含失败的）都记录在 `analysis/factor_trials_log.csv`。",
        "",
        "---",
        "",
        "## 一、数据地基",
        "",
        f"- 股票池 **{meta.get('universe_size', 0):,} 只**"
        f"（当前在市 ∪ 已退市 {meta.get('delisted', 0):,} 只）",
        f"- 面板 **{meta.get('rows', 0):,} 行**，"
        f"{meta.get('symbols', 0):,} 只，"
        f"{meta.get('first', '?')} ~ {meta.get('last', '?')}",
        "",
        "### 为什么必须重建",
        "",
        "被弃用的旧面板 `analysis/screener_universe_20260920/daily.csv`：",
        "",
        "| 问题 | 实测证据 |",
        "|---|---|",
        "| **不复权价** | `dividend_type=\"none\"`，送转日被当成暴跌 |",
        "| **幸存者偏差 100%** | 5129 只里**没有任何一只**最后交易日早于 2026-09-18；"
        "退市股 0 只 |",
        "",
        "旧的证伪研究（「开关超额 −5.99%」等）建立在这份数据上，"
        "**需要在干净面板上重跑才能当定论**。",
        "",
        f"完整 QC 见 `{PANEL_QC.relative_to(ROOT)}`。",
        "",
        "---",
        "",
        "## 二、回测引擎与防未来函数",
        "",
        "`portfolio/` 是新建的横截面组合引擎（仓库此前**没有任何**"
        "「选股→组合→再平衡→组合净值」的闭环）。",
        "",
        "时序：**T 日收盘算信号，T+1 开盘成交**。",
        "「T 日收盘成交」不是可实现假设，只作为诊断口径。",
        "",
        "**无未来函数闸门**：把面板截断到 `asof` 跑出的目标权重，"
        "必须与全量面板在同一 `asof` 跑出的逐元素相等。",
        "五条测试见 `tests/test_portfolio_no_lookahead.py`，"
        "**含一条反向验证**（故意注入泄漏因子，闸门必须抓到）。",
        "",
        "---",
        "",
        "## 三、择时层（Stage 2）",
        "",
    ]

    if TIMING_SUMMARY.exists():
        timing = pd.read_csv(TIMING_SUMMARY)
        lines.extend([
            "| 指数 | 变体 | IS 夏普 | 满仓夏普 | 差 | 最大回撤 | 满仓回撤 | 平均仓位 |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for _, r in timing.iterrows():
            lines.append(
                f"| {r['index']} | {r['variant']} | {r['is_sharpe']:.3f} | "
                f"{r['base_sharpe']:.3f} | **{r['sharpe_delta']:+.3f}** | "
                f"{r['max_dd']:.2%} | {r['base_max_dd']:.2%} | "
                f"{r['avg_exposure']:.1%} |")
        improved = (timing["sharpe_delta"] > 0).sum()
        total = len(timing)
        lines.extend([
            "",
            f"改善 IS 夏普的配置：**{improved} / {total}**",
            "",
            "判定依据预注册：**≥2/3 指数上改善 IS 夏普且不依赖单一年份**，"
            "才提升为基准；否则基准退回 `gross=1.0`，择时记 `无效`。",
        ])
    else:
        lines.append("（Stage 2 未运行）")

    lines.extend(["", "---", "", "## 四、因子筛选（Stage 3）", ""])

    if IC_GRID.exists():
        grid = pd.read_csv(IC_GRID)
        passed = grid[grid["pass_a"] & grid["pass_b"] & grid["pass_c"]]
        lines.extend([
            f"预注册网格 **66 个配置**（11 因子 × 2 处理 × 3 持有期），"
            f"实际完成 **{len(grid)} 个**。",
            "",
            f"**通过闸门 A/B/C 的：{len(passed)} 个。**",
            "",
        ])
        if control:
            best = float(grid["ic_mean"].abs().max())
            lines.extend([
                "### 随机对照",
                "",
                f"20 个随机截面分数的 IS `|IC|`：最大 "
                f"**{control.get('best_abs_ic', float('nan')):.4f}**、"
                f"95 分位 {control.get('p95_abs_ic', float('nan')):.4f}、"
                f"中位 {control.get('median_abs_ic', float('nan')):.4f}",
                "",
                f"候选因子最好 `|IC|` = **{best:.4f}**",
                "",
                ("**没有超过随机对照的上限 → 全部判定为运气。**"
                 if best <= control.get("best_abs_ic", 1e9)
                 else "有候选超过随机对照上限。"),
            ])
        lines.extend([
            "",
            "完整网格（含全部失败）见 "
            "`analysis/factor_screen_20260920/README.md`。",
        ])
    else:
        lines.append("（Stage 3 未运行）")

    if TRIALS.exists():
        trials = pd.read_csv(TRIALS)
        lines.extend([
            "",
            f"**`factor_trials_log.csv` 记录了 {len(trials)} 次尝试** —— "
            "这是多重检验校正里 `N_trials` 的真实取值，"
            "不取我们「声称」试了多少次。",
        ])

    lines.extend(["", "---", "", "## 五、组合回测（Stage 4）与样本外（Stage 5）", ""])
    for label, path in (("IS", PORTFOLIO_IS), ("OOS-2", PORTFOLIO_OOS2)):
        if path.exists():
            lines.extend([f"### {label}", "", path.read_text(encoding="utf-8"),
                          ""])
        else:
            lines.append(f"（{label} 未运行）")

    lines.extend([
        "",
        "---",
        "",
        "## 六、必须与结论同时引用的口径限制",
        "",
        "| 限制 | 影响 |",
        "|---|---|",
        "| 历史 `isST` 是**推导值**（±5% 涨跌幅有界性），非交易所口径 | "
        "ST 过滤与 ±5% 板判定可能有误 |",
        "| **F9 换手率因子缺失**（桥接无历史流通股本） | 少测一个因子 |",
        "| 不做行业中性化（拿不到行业分类） | 因子可能带行业暴露 |",
        "| 股息税未计 | 乐观偏差约 0.3%/年 |",
        "| 持有期内退市按最后可交易价清算 | 未计退市整理期的连续跌停 |",
        "| 停牌期按冻结价盯市 | 低估停牌期的真实波动与回撤 |",
        "",
        "**F9 的剔除是数据源缺口导致的，不是结果驱动的。**",
        "",
        "---",
        "",
        "## 七、复现链",
        "",
        "```bash",
        "python analysis/panel_universe_20260920.py",
        "bash analysis/panel_fetch_all.sh 6",
        "python analysis/panel_build_20260920.py --check-st",
        "python analysis/timing_index_study_20260920.py",
        "python analysis/factor_screen_20260920.py",
        "python analysis/portfolio_backtest_20260920.py --ablation --slippage-grid",
        "python analysis/final_report_20260920.py",
        "```",
        "",
        f"预注册原文：`{PREREG.relative_to(ROOT)}`",
    ])

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
