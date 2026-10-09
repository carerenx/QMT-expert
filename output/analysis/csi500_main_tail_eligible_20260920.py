"""诊断：尾部检验的「全域」到底该是什么？

## 要解释的不一致

收窄池（415 只）上：

* **尾部层**：F10 隔夜 / size_neutral / 21日，top30 超额 **+0.257%/月**
  ≈ **+3.1%/年（毛利）**
* **引擎层**：同一个因子配置（n=30）年化 **+18.0%**

6 倍的差距。要么引擎里有东西尾部检验没抓到，要么尾部检验的基准选错了。

## 最可疑的一处

`top30_excess = mean(top30 下期收益) − mean(全池下期收益)`。

但**引擎根本不在全池里选** —— 它只在 `panel.eligible()` 之后的池子里选：
上市 ≥ 250 日、20 日均成交额 ≥ 5000 万、股价 ≤ 100万/30/100 = 333 元。

如果被 `eligible` 挡掉的那部分股票收益**偏低**，那么拿全池当基准
会把超额压低甚至压成负数 —— 而引擎实际面对的可选域里，超额可能大得多。

本脚本把尾部检验在**两个基准**上各跑一遍：

1. `all` —— 全池（原口径）
2. `eligible` —— 与引擎相同的可交易域

如果 `eligible` 口径下超额大幅上升，那 6 倍差距就是**基准选错**，
不是引擎有问题；反之则说明引擎的收益另有来源，必须查清才敢上实盘。

用法：
    python analysis/csi500_main_tail_eligible_20260920.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import factors as factor_lib          # noqa: E402
from portfolio import metrics, transforms            # noqa: E402
from portfolio.construct import select_holdings, target_weights  # noqa: E402
from portfolio.panel import PanelData                # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
UNIVERSE = ROOT / "analysis/panel_20260920/csi500_main_universe.json"
OUT = ROOT / "analysis/csi500_main_study_20260920"

START, END, WARMUP = "20230901", "20260918", "20210101"
EQUITY = 1_000_000.0
N_HOLDINGS = 30

CHECKS = (("F10_overnight_intraday", "size_neutral"),
          ("F6_max_effect", "raw"),
          ("F6_max_effect", "size_neutral"),
          ("F7_ivol", "size_neutral"))


def load_panel() -> PanelData:
    codes = set(json.loads(UNIVERSE.read_text(encoding="utf-8"))["codes"])
    panel = PanelData.load(PANEL).between(WARMUP, END)
    long = panel.long[panel.long["code"].isin(codes)]
    return PanelData.from_frame(long.reset_index(drop=True))


def main() -> None:
    panel = load_panel()
    long = panel.long
    size = transforms.size_proxy(long)

    frame = long[["code", "date", "adj_close"]].sort_values(["code", "date"])
    frame["fwd"] = (frame.groupby("code")["adj_close"]
                    .transform(lambda s: s.shift(-21) / s - 1.0))
    forward = frame[["code", "date", "fwd"]]

    # 每个交易日的可交易域（与引擎同口径，含整手价格上限）
    dates = [d for d in panel.dates if START <= d <= END]
    print(f"窗口 {dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）", flush=True)
    elig: dict[str, set[str]] = {}
    for d in dates:
        elig[d] = set(panel.eligible(d, min_listed_days=250, require_liquid=True,
                                     max_price=EQUITY / N_HOLDINGS / 100.0))
    sizes = np.array([len(elig[d]) for d in dates])
    print(f"  可交易域：中位 {int(np.median(sizes))} 只 / 全池 "
          f"{len(panel.codes)} 只", flush=True)

    lines = [
        "# 尾部检验的基准口径诊断",
        "",
        "同一套因子，在两个基准上算 top30 超额：",
        "",
        "* `all` —— 全池平均（原口径）",
        "* `eligible` —— **与引擎相同的可交易域**",
        "",
        "若两者差异很大，说明原口径的基准选错了 —— 引擎不在全池里选，",
        "拿全池当基准会把超额系统性地压低。",
        "",
        f"窗口 {dates[0]} ~ {dates[-1]}，n_holdings = {N_HOLDINGS}。",
        "",
        "| 因子 | 处理 | 全池 top30 超额 | **可交易域 top30 超额** | 可交易域超额(年化) | 全池均收益 | 可交易域均收益 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]

    for key, treatment in CHECKS:
        base = factor_lib.compute(long, key)
        alpha = transforms.preprocess(
            base, size=size, neutralize=(treatment == "size_neutral"))
        merged = alpha.merge(forward, on=["code", "date"], how="inner")
        merged = merged[(merged["date"] >= START) & (merged["date"] <= END)]

        rows_all, rows_elig, univ_all, univ_elig = [], [], [], []
        for date, day in merged.groupby("date", sort=True):
            day = day.dropna(subset=["alpha", "fwd"])
            if len(day) < N_HOLDINGS + 1:
                continue
            univ_all.append(day["fwd"].mean())
            picks = day.nlargest(N_HOLDINGS, "alpha")
            rows_all.append(picks["fwd"].mean() - day["fwd"].mean())

            allowed = elig.get(date, set())
            sub = day[day["code"].isin(allowed)]
            if len(sub) < N_HOLDINGS + 1:
                continue
            univ_elig.append(sub["fwd"].mean())
            sub_picks = sub.nlargest(N_HOLDINGS, "alpha")
            rows_elig.append(sub_picks["fwd"].mean() - sub["fwd"].mean())

        a = float(np.mean(rows_all))
        e = float(np.mean(rows_elig))
        lines.append(
            f"| {key} | {treatment} | {a:+.3%} | **{e:+.3%}** | "
            f"{e * 12:+.1%} | {np.mean(univ_all):+.3%} | "
            f"{np.mean(univ_elig):+.3%} |")
        print(f"  {key}/{treatment}: 全池 {a:+.3%} → 可交易域 {e:+.3%}"
              f"（年化 {e * 12:+.1%}）", flush=True)

    lines.extend([
        "",
        "## 怎么读",
        "",
        "* 若**可交易域超额显著大于全池超额** → 原尾部检验的基准选错了，"
        "6 倍差距是口径问题，**引擎结果需要重新解释**。",
        "* 若两者接近 → 差距不是基准造成的，引擎那 +18% 另有来源"
        "（集中度运气、少数几期），**不能在没查清之前上实盘**。",
    ])

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tail_eligible_diagnostic.md").write_text("\n".join(lines),
                                                     encoding="utf-8")
    print(f"\n→ {OUT / 'tail_eligible_diagnostic.md'}")


if __name__ == "__main__":
    main()
