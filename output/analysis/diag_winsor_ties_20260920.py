"""诊断：缩尾并列导致的选择任意性，对引擎结果有多大影响？

## 问题

`transforms.preprocess` 里 `winsorize` 被用了**两次**：

```python
frame["alpha"] = zscore(winsorize(s))        # 缩尾 → 标准化
frame["alpha"] = neutralize_by(...)          # 规模中性化
frame["alpha"] = zscore(winsorize(s))        # **再缩尾一次**
```

`WINSOR_Q = 0.01`，收窄池 ~409 只 → 顶部 ~5 只被压到**同一个值**。
实测 739 个截面里**每一个都恰好 5 只并列**，且**全部落在 top-10 内**。

`select_holdings` 收到的 `ranked` 是 `sort_values(ascending=False)` 的结果，
pandas 的稳定排序在并列时**保留原始行序** —— 面板按 `(code, date)` 排过，
所以并列处的取舍实质上是**按代码字母序**。

## 这意味着什么

* **不是偏差**：并列的 5 只因子值相同，经济上近似等价，选谁都不算「错」
* **是噪声**：取舍与未来收益无关（字母序），等于在近似等价的候选里**随机抽**
* 后果：回测结果多了一层与信号无关的随机性 —— 观测到的收益更可能是运气

## 本脚本测什么

把 `winsorize` 换成恒等函数（**完全不缩尾**），其余一切不变，重跑引擎。
如果结果与缩尾版**差很多**，说明之前的数字有相当一部分是并列取舍的运气。

用法：
    python analysis/diag_winsor_ties_20260920.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import factors as factor_lib                    # noqa: E402
from portfolio import metrics, transforms                      # noqa: E402
from portfolio.costs import CostModel                          # noqa: E402
from portfolio.engine import PortfolioEngine                   # noqa: E402
from portfolio.panel import PanelData                          # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
UNIVERSE = ROOT / "analysis/panel_20260920/csi500_main_universe.json"
OUT = ROOT / "analysis/csi500_main_study_20260920"

START, END, WARMUP = "20230901", "20260918", "20210101"

CHECKS = (("F7_ivol", "size_neutral", 10),
          ("F10_overnight_intraday", "size_neutral", 30),
          ("F6_max_effect", "raw", 30))


def load_panel() -> PanelData:
    codes = set(json.loads(UNIVERSE.read_text(encoding="utf-8"))["codes"])
    panel = PanelData.load(PANEL).between(WARMUP, END)
    long = panel.long[panel.long["code"].isin(codes)]
    return PanelData.from_frame(long.reset_index(drop=True))


def run(panel, alpha, n_holdings, slippage_bps=5.0) -> dict:
    engine = PortfolioEngine(panel, costs=CostModel(slippage_bps=slippage_bps),
                             n_holdings=n_holdings, rebalance_rule="M",
                             execution="open")
    result = engine.run(lambda p, a=alpha: a, None, start=START, end=END)
    curve = result.equity
    return {"ann": metrics.annualized_return(curve),
            "sharpe": metrics.sharpe(result.returns),
            "dd": metrics.max_drawdown(curve),
            "ret": float(curve.iloc[-1] / curve.iloc[0] - 1.0)}


def main() -> None:
    panel = load_panel()
    long = panel.long
    size = transforms.size_proxy(long)

    original = transforms.winsorize
    rows = []
    for key, treatment, n_holdings in CHECKS:
        base = factor_lib.compute(long, key)
        for label, fn in (("缩尾 1%（原）", original),
                          ("不缩尾", lambda s, q=0.0: s)):
            transforms.winsorize = fn
            alpha = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            out = run(panel, alpha, n_holdings)
            rows.append({"因子": key, "处理": treatment, "持仓": n_holdings,
                         "口径": label, **out})
            print(f"  {key}/{treatment}/n={n_holdings}  [{label}] "
                  f"年化 {out['ann']:+.1%} 夏普 {out['sharpe']:.2f}", flush=True)
        transforms.winsorize = original

    grid = pd.DataFrame(rows)
    lines = [
        "# 缩尾并列的选择任意性：敏感度测试",
        "",
        "`preprocess` 里 `winsorize` 用了**两次**（中性化前后各一次），",
        "`WINSOR_Q = 0.01` → 收窄池 ~409 只里**顶部 5 只被压成同一个值**。",
        "实测 739 个截面**每一个都恰好 5 只并列**，且全部落在 top-10 内。",
        "",
        "`select_holdings` 在并列处保留原始行序 → 实质**按代码字母序**取舍。",
        "**不是偏差（并列者因子值相同），是噪声** —— 取舍与未来收益无关。",
        "",
        "## 把「缩尾」整个关掉，其余不变",
        "",
        "| 因子 | 处理 | 持仓 | 口径 | 年化 | 夏普 | 最大回撤 |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for _, r in grid.iterrows():
        lines.append(
            f"| {r['因子']} | {r['处理']} | {r['持仓']} | {r['口径']} | "
            f"**{r['ann']:+.1%}** | {r['sharpe']:.2f} | {r['dd']:.1%} |")

    lines.extend(["", "## 差了多少", "",
                  "| 因子 | 处理 | 持仓 | 缩尾 1% | 不缩尾 | 差 |",
                  "|---|---|---:|---:|---:|---:|"])
    for (key, treatment, n), g in grid.groupby(["因子", "处理", "持仓"],
                                               sort=False):
        a = g[g["口径"] == "缩尾 1%（原）"]["ann"].iloc[0]
        b = g[g["口径"] == "不缩尾"]["ann"].iloc[0]
        lines.append(f"| {key} | {treatment} | {n} | {a:+.1%} | {b:+.1%} | "
                     f"**{b - a:+.1f}pp** |")

    lines.extend([
        "",
        "## 怎么读",
        "",
        "缩尾的作用是压住极端值对 z-score 的平方级影响，**本身是合理的**。",
        "问题在于**中性化之后又缩了一次**，而这次缩尾把顶部 5 只的信息抹平了。",
        "",
        "* 若两者差得少 → 并列取舍不是主因，之前的数字不受此影响",
        "* 若两者差得多 → **之前的引擎数字里混了相当一部分并列取舍的运气**",
        "",
        "**修法**（本轮未改，仅记录）：用**缩尾前的原始因子值**做并列时的",
        "次级排序键，而不是让行序决定。这样并列仍有唯一解，且解是有经济含义的。",
    ])

    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_csv(OUT / "winsor_ties_sensitivity.csv", index=False,
                encoding="utf-8")
    (OUT / "winsor_ties_diagnostic.md").write_text("\n".join(lines),
                                                   encoding="utf-8")
    print(f"\n→ {OUT / 'winsor_ties_diagnostic.md'}")


if __name__ == "__main__":
    main()
