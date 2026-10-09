"""Stage 3.5：**尾部检验** —— IC 为正不代表只做多能赚钱。

## 为什么必须补这一步

IC 筛完之后我拿 IC 最高的 F2（一月反转）跑了一次组合回测，
结果是**年化 −60%**。但 IC 明明是 +0.065、t = 4.9、逐年同号 100%。

原因是 IC 和「只做多前十档」看的是**不同的东西**：

* IC 度量的是**全体股票的横截面**关系
* 只做多组合买的只是**最极端的那 30 只**

实测 F2 的十分位表：最大赢家那一档 −0.58%，其余九档全在 +0.4%~+1.4%。
也就是说**反转的预测力几乎全部来自「避开最大的赢家」**，
而买「最大的输家」那一档只有 +1.33% —— 相对全体均值 +1.24% 几乎没有超额。

而实际要买的前 30 只：均值 +0.42%、中位 −0.89%、为正比例 48% ——
**低于全体均值**。所以 IC 为正、组合巨亏，两件事可以同时成立。

**结论：IC 是必要条件，不是充分条件。** 只做多策略必须直接检验尾部。

## 本脚本做什么

对每个因子、每种处理，直接算：

1. **十分位表** —— 效应集中在哪一档？
2. **前 N 只（N=30/50/100）的等权未来收益** —— 均值、中位、为正比例
3. **与全体均值的差** —— 这才是只做多能拿到的超额

## 判定（在 Stage 4 之前）

尾部检验不过的因子不进入组合回测 —— 它的 IC 再高，也只是空头端的信号。

用法：
    python analysis/topn_diagnostic_20260920.py
    python analysis/topn_diagnostic_20260920.py --factors F6_max_effect
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import factors as factor_lib          # noqa: E402
from portfolio import transforms                     # noqa: E402
from portfolio.panel import PanelData                # noqa: E402


PANEL = ROOT / "analysis/panel_20260920/panel.parquet"
OUT = ROOT / "analysis/topn_diagnostic_20260920"

IS = ("20110101", "20181231")
OOS1 = ("20190101", "20221231")
HORIZON = 21                # 与月度调仓对齐
TOP_N = (30, 50, 100)


def forward_returns(long: pd.DataFrame, horizon: int) -> pd.DataFrame:
    frame = long[["code", "date", "adj_close"]].sort_values(["code", "date"])
    frame["fwd"] = (frame.groupby("code")["adj_close"]
                    .transform(lambda s: s.shift(-horizon) / s - 1.0))
    return frame[["code", "date", "fwd"]]


def sample_dates(dates: list[str], horizon: int) -> list[str]:
    return list(dates)[::horizon]


def diagnose(merged: pd.DataFrame, window: tuple[str, str],
             label: str) -> dict:
    start, end = window
    sub = merged[(merged["date"] >= start) & (merged["date"] <= end)]
    if sub.empty:
        return {}
    sub = sub.assign(
        decile=sub.groupby("date")["alpha"]
        .transform(lambda s: pd.qcut(s.rank(method="first"), 10,
                                     labels=False)))

    decile = sub.groupby("decile")["fwd"].mean()
    universe = float(sub["fwd"].mean())

    out = {"label": label, "universe_mean": universe, "n_obs": len(sub)}
    for i in range(10):
        out[f"d{i}"] = float(decile.get(i, np.nan))

    for n in TOP_N:
        # 每个调仓日取 alpha 最高的 n 只，等权
        picks = (sub.sort_values("alpha", ascending=False)
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
    parser.add_argument("--horizon", type=int, default=HORIZON)
    args = parser.parse_args()

    if not PANEL.exists():
        raise SystemExit(f"缺 {PANEL}")

    print("载入面板 ...", flush=True)
    panel = PanelData.load(PANEL)
    long = panel.long
    forward = forward_returns(long, args.horizon)
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
        for treatment in ("raw", "size_neutral"):
            processed = transforms.preprocess(
                base, size=size, neutralize=(treatment == "size_neutral"))
            if processed.empty:
                continue
            merged = processed.merge(forward, on=["code", "date"], how="inner")
            for window, tag in ((IS, "IS"), (OOS1, "OOS1")):
                stats = diagnose(merged, window, f"{key}|{treatment}|{tag}")
                if stats:
                    stats.update({"factor": key, "treatment": treatment,
                                  "window": tag})
                    rows.append(stats)
        print(f"  {key} 完成（{time.time() - t0:.0f} 秒）", flush=True)

    frame = pd.DataFrame(rows)

    # ── 报告 ──
    lines = [
        "# Stage 3.5：尾部检验（只做多能不能赚）",
        "",
        f"前瞻窗口 **{args.horizon} 个交易日**，与月度调仓对齐。",
        "",
        "## 为什么要有这一步",
        "",
        "IC 筛完之后，IC 最高的 F2（一月反转）跑组合回测得到**年化 −60%**，",
        "而它的 IC 是 +0.065、t = 4.9、逐年同号 100%。",
        "",
        "原因是 **IC 与只做多组合看的不是同一件事**：",
        "IC 度量全体股票的横截面关系，只做多组合只买最极端的那 30 只。",
        "",
        "实测 F2 的十分位均值：最大赢家那档 −0.58%，其余九档全在 +0.4%~+1.4%。",
        "**反转的预测力几乎全部来自「避开最大的赢家」**，",
        "而买「最大的输家」那一档相对全体均值几乎没有超额。",
        "",
        "**IC 是必要条件，不是充分条件。**",
        "",
        "## 十分位均值（IS）",
        "",
        "| 因子 | 处理 | d0 | d1 | d2 | d3 | d4 | d5 | d6 | d7 | d8 | d9 | 全体 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    is_rows = frame[frame["window"] == "IS"]
    for _, r in is_rows.iterrows():
        cells = " | ".join(f"{r[f'd{i}']:+.3f}" for i in range(10))
        lines.append(f"| {r['factor']} | {r['treatment']} | {cells} | "
                     f"{r['universe_mean']:+.3f} |")

    lines.extend([
        "",
        "## 前 N 只等权组合（IS）",
        "",
        "**这是只做多真正能拿到的** —— `超额` 是相对全体均值的差。",
        "",
        "| 因子 | 处理 | N | 均值 | 中位 | 为正 | **超额** | 判定 |",
        "|---|---|---:|---:|---:|---:|---:|:---:|",
    ])
    for _, r in is_rows.iterrows():
        for n in TOP_N:
            verdict = "✓" if r[f"top{n}_excess"] > 0 else "✗"
            lines.append(
                f"| {r['factor']} | {r['treatment']} | {n} | "
                f"{r[f'top{n}_mean']:+.3%} | {r[f'top{n}_median']:+.3%} | "
                f"{r[f'top{n}_positive']:.0%} | "
                f"**{r[f'top{n}_excess']:+.3%}** | {verdict} |")

    lines.extend([
        "",
        "## OOS-1 的 top-30 超额",
        "",
        "| 因子 | 处理 | IS top30 超额 | OOS-1 top30 超额 | 两期同号 |",
        "|---|---|---:|---:|:---:|",
    ])
    oos_rows = frame[frame["window"] == "OOS1"]
    for _, r in is_rows[is_rows["treatment"].isin(["raw", "size_neutral"])] \
            .iterrows():
        match = oos_rows[(oos_rows["factor"] == r["factor"])
                         & (oos_rows["treatment"] == r["treatment"])]
        if match.empty:
            continue
        oos_excess = float(match.iloc[0]["top30_excess"])
        same = np.sign(r["top30_excess"]) == np.sign(oos_excess)
        lines.append(f"| {r['factor']} | {r['treatment']} | "
                     f"{r['top30_excess']:+.3%} | {oos_excess:+.3%} | "
                     f"{'✓' if same else '✗'} |")

    lines.extend([
        "",
        "## 判定",
        "",
        "进入组合回测（Stage 4）的条件：**top-30 超额为正，且 IS 与 OOS-1 同号。**",
        "",
        "不满足的因子即使 IC 很高也不进入 —— 它的信号在空头端，",
        "只做多组合吃不到。**这不是调参能解决的。**",
    ])

    OUT.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT / "topn.csv", index=False, encoding="utf-8")
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {OUT / 'README.md'}")


if __name__ == "__main__":
    main()
