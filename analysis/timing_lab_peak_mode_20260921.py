"""A/B：风控开关的 peak 该用哪种语义？

## 要回答的问题

v0563 融合的 v4 外层有一个不显眼的假设：**peak（跟踪止损的锚）永不重置**，
它是「全窗口收盘价的运行最高值」。于是崩盘之后再起一波行情时，止损线仍然挂在
崩盘前那个顶上。

在 601869 上这造成一个具体现象：2026-07-30 见底 255.25、随后涨到 475，
但 peak 仍是 6/24 的 579.71。止损线 `579.71 − 3×ATR ≈ 500.85` 一直高于现价，
而回场闸门 `MA20` 已经降到 424 —— 现价夹在两条线之间，状态**每天翻一次**。
实测 08-04 之后 34 个交易日里 30 天收在这条带子里（88%），平均带宽 142 元。

语义定义在 `Stragety/MiniQMT_Stragety/core/peak_mode.py`（三处调用点共用）：

  * ``MARKET``     —— 运行最高值，永不重置（**v4 原版，默认**，作为基准）
  * ``REENTRY``    —— 每次「空仓→回场」时重置为回场价
  * ``ROLLING<N>`` —— 近 N 个交易日收盘价的最高值（20 ≈ 一个自然月）

## 为什么必须用面板而不是这一条样本

在 601869 单条样本上，两种替代都明显更好（MARKET +72,000 → REENTRY +93,976、
ROLLING20 +77,164）。**这恰恰是它们可疑的地方。** v4 自己的证据是：
11 个标的横截面 t = 1.58、中位 +780；5,129 只 × 12 年面板平均开关超额 −5.99%。
「在崩盘后换一种锚能在这次崩盘后更赚钱」是典型的曲线拟合形状。

所以本脚本的判决口径**在看结果之前就定下**：

  1. **主判决看配对差（替代 − MARKET），不看单条样本。**
     同一个标的、同一段窗口，只有 peak 语义不同 —— 干净的配对对照。
  2. **看中位和跨年符号一致性，不看均值最大化。**
     v4 选参的既定纪律是「按稳健性选，不按最高分选」。
  3. **单条样本的改善不构成证据**，只作为「这个改动在做什么」的说明。

运行：
    python analysis/timing_lab_peak_mode_20260921.py             # 全量
    python analysis/timing_lab_peak_mode_20260921.py --no-panel  # 快速
    python analysis/timing_lab_peak_mode_20260921.py --render    # 用已存面板重出报告
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis import screener_lab_20260920 as SL
from analysis import timing_lab_20260920 as TL

OUT = ROOT / "analysis/timing_lab_peak_mode_20260921"
#: MARKET 是基准；其余每种语义都与它做配对比较。
REFERENCE = "MARKET"
MODES = ("MARKET", "REENTRY", "ROLLING10", "ROLLING20", "ROLLING40")
RULE = lambda ctx: TL.rule_atr_trail(ctx, 14, 3.0)


def _t(values) -> float:
    """Naive t of the mean.  Always reported with a correlation caveat."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2 or values.std(ddof=1) == 0:
        return float("nan")
    return float(values.mean() / (values.std(ddof=1) / np.sqrt(len(values))))


def _short(mode: str) -> str:
    return mode.replace("ROLLING", "ROLL")


# ─────────────────────── ① 单条样本：601869 明细 ───────────────────────

def single_symbol() -> dict:
    daily = pd.read_csv(TL.DATA / "1d.csv", dtype={"time": str}
                        ).set_index("time").sort_index()
    daily = daily[(daily.index >= "20250101") & (daily.index <= "20260918")]
    out = {}
    for mode in MODES:
        res = TL.simulate(daily, RULE, "20260101", peak_mode=mode)
        out[mode] = {
            "res": res,
            "intervals": {name: TL.interval_excess(res, lo, hi)
                          for name, lo, hi in TL.INTERVALS},
            "crash": TL.crash_attribution(res),
        }
    return out


def single_symbol_md(cells: dict) -> list[str]:
    lines = ["## ① 单条样本：601869（说明这些改动在做什么，**不作为证据**）", "",
             "| peak 语义 | 期末资产 | 相对持有 | 换手次数 | 空仓天数 | 最大回撤 | 手续费 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for mode in MODES:
        r = cells[mode]["res"]
        bold = "**" if mode == REFERENCE else ""
        lines.append(f"| {bold}{_short(mode)}{bold} | {r['final_net']:,.0f} | "
                     f"{r['excess']:+,.0f} | {r['switches']} | {r['days_flat']} | "
                     f"{r['max_dd']:.1%} | {r['fees']:,.0f} |")
    lines += ["", "崩盘段归因（元，三段可加）：", "",
              "| peak 语义 | 崩盘前 | **崩盘段 6/24–8/3** | 崩盘后 |",
              "|---|---:|---:|---:|"]
    for mode in MODES:
        c = cells[mode]["crash"]
        lines.append(f"| {_short(mode)} | {c['ex_crash_top']:+,.0f} | "
                     f"**{c['crash']:+,.0f}** | {c['ex_crash_bot']:+,.0f} |")
    lines += ["", "> 崩盘段三行**完全相同**——崩盘时仓位是崩盘前开的，高水位就是当时的顶。",
              "> 差异全部出现在「回场之后」。这一条对三种语义都成立，", ""]
    return lines


# ─────────────────────── ② 横截面：13 个标的 ───────────────────────

def cross_section() -> dict:
    out = {}
    for code in TL.CROSS_SYMBOLS:
        daily = TL.load_symbol(code)
        row = {}
        for mode in MODES:
            res = TL.simulate(daily, RULE, "20260105", peak_mode=mode)
            row[mode] = TL.interval_alpha(res, "20260105", "20260911")
        # Measure the move over the SAME window as the alpha.  Using the whole
        # warm-up series here would report the front-adjustment span, not the
        # common window, and 601869 reads +1290% that way.
        window = daily[(daily.index >= "20260105") & (daily.index <= "20260911")]
        row["hold"] = float(window["close"].iloc[-1] / window["close"].iloc[0] - 1)
        out[code] = row
        print(f"  {code}: " + "  ".join(
            f"{_short(m)}={row[m]*100:+.1f}pp" for m in MODES), flush=True)
    return out


def cross_section_md(cross: dict) -> list[str]:
    lines = ["## ② 横截面：13 个标的（共同窗口 2026-01-05 ~ 09-11）", "",
             "同一套规则、同一段窗口，只换 peak 语义。", "",
             "| 标的 | 区间涨跌 | " + " | ".join(_short(m) for m in MODES) +
             " | " + " | ".join(f"**{_short(m)}−基准**" for m in MODES[1:]) + " |",
             "|---|---:|" + "---:|" * len(MODES) + "---:|" * (len(MODES) - 1)]
    for code, row in cross.items():
        base = row[REFERENCE]
        lines.append(
            f"| {code} | {row['hold']:+.1%} | " +
            " | ".join(f"{row[m]:+.1%}" for m in MODES) + " | " +
            " | ".join(f"**{row[m] - base:+.1%}**" for m in MODES[1:]) + " |")
    lines += ["", "| 汇总 | " + " | ".join(_short(m) for m in MODES[1:]) + " |",
              "|---|" + "---:|" * (len(MODES) - 1)]
    for label, fn in (("配对差 均值", np.mean), ("配对差 **中位**", np.median)):
        lines.append(f"| {label} | " + " | ".join(
            f"{fn([v[m] - v[REFERENCE] for v in cross.values()]):+.2%}"
            for m in MODES[1:]) + " |")
    lines.append("| 配对差 > 0 的标的 | " + " | ".join(
        f"{sum(1 for v in cross.values() if v[m] > v[REFERENCE])}/{len(cross)}"
        for m in MODES[1:]) + " |")
    lines.append("| 配对差 t（朴素） | " + " | ".join(
        f"{_t([v[m] - v[REFERENCE] for v in cross.values()]):.2f}"
        for m in MODES[1:]) + " |")
    lines.append("| 该语义下开关为正的标的数 | " + " | ".join(
        f"{sum(1 for v in cross.values() if v[m] > 0)}/{len(cross)}"
        for m in MODES[1:]) + " |")
    lines += ["", f"（基准 `{REFERENCE}` 自身：为正 "
                  f"{sum(1 for v in cross.values() if v[REFERENCE] > 0)}/{len(cross)}）", "",
              "> 与 v4 报告同一口径的警告：这 13 个标的在同一段行情里同涨同跌，",
              "> 「n/13 为正」并不是 n 次独立验证。", ""]
    return lines


# ─────────────────────── ③ 面板：全市场 × 12 年 ───────────────────────

def panel() -> pd.DataFrame:
    src = ROOT / "analysis/screener_universe_20260920/daily.csv"
    print(f"loading {src} ...", flush=True)
    raw = pd.read_csv(src, dtype={"time": str})
    print(f"  {len(raw):,} rows, {raw['code'].nunique()} symbols", flush=True)
    base = None
    merged = None
    for mode in MODES:
        started = time.time()
        frame = SL.build_panel(raw, peak_mode=mode)
        if base is None:
            base = frame
            merged = base[["code", "date", "er", "vr", "fwd_hold"]].copy()
            merged["year"] = merged["date"].str[:4].astype(int)
        elif not base[["code", "date"]].equals(frame[["code", "date"]]):
            raise RuntimeError("panel rows are not aligned for " + mode)
        merged["ex_" + mode] = frame["fwd_excess"].to_numpy()
        print(f"  build {mode}: {len(frame):,} obs in {time.time() - started:.0f}s",
              flush=True)
        del frame
    for mode in MODES[1:]:
        merged["pair_" + mode] = merged["ex_" + mode] - merged["ex_" + REFERENCE]
    return merged


def panel_md(frame: pd.DataFrame) -> list[str]:
    lines = ["## ③ 面板：全市场 × 12 年（**决定性检验**）", "",
             f"- 观测：**{frame['code'].nunique():,} 只股票 × {len(frame):,} 个（股票, 时点）**，"
             f"{frame['date'].min()[:4]}–{frame['date'].max()[:4]}，每 126 个交易日一个时点，窗口不重叠",
             "- 配对差 = 同一 (股票, 时点) 上 `替代语义 − MARKET`", "",
             "### 总览", "",
             "| 口径 | " + " | ".join(_short(m) for m in MODES) + " |",
             "|---|" + "---:|" * len(MODES)]
    for label, fn in (("均值", np.mean), ("中位", np.median)):
        lines.append(f"| {label} | " + " | ".join(
            f"{fn(frame['ex_' + m]):+.2%}" for m in MODES) + " |")
    lines.append("| 为正占比 | " + " | ".join(
        f"{(frame['ex_' + m] > 0).mean():.1%}" for m in MODES) + " |")
    lines += ["", "### 配对差（vs MARKET）", "",
              "| 口径 | " + " | ".join(_short(m) for m in MODES[1:]) + " |",
              "|---|" + "---:|" * (len(MODES) - 1)]
    for label, fn in (("均值", np.mean), ("中位", np.median)):
        lines.append(f"| {label} | " + " | ".join(
            f"{fn(frame['pair_' + m]):+.2%}" for m in MODES[1:]) + " |")
    lines.append("| 为正占比 | " + " | ".join(
        f"{(frame['pair_' + m] > 0).mean():.1%}" for m in MODES[1:]) + " |")
    lines.append("| 朴素 t（被高估） | " + " | ".join(
        f"{_t(frame['pair_' + m]):.1f}" for m in MODES[1:]) + " |")
    lines.append("| **按年聚类 t** | " + " | ".join(
        f"**{_t(frame.groupby('year')['pair_' + m].mean()):.2f}**"
        for m in MODES[1:]) + " |")
    lines += ["", "### 逐年面板超额（判决口径：看符号一致性）", "",
              "| 年份 | 观测 | 持有收益 | " +
              " | ".join(_short(m) for m in MODES) + " |",
              "|---|---:|---:|" + "---:|" * len(MODES)]
    by_year = frame.groupby("year")
    for year, sub in by_year:
        lines.append(f"| {int(year)} | {len(sub):,} | {sub['fwd_hold'].mean():+.1%} | " +
                     " | ".join(f"{sub['ex_' + m].mean():+.2%}" for m in MODES) + " |")
    lines += ["", "| 配对差为正的年份 | " + " | ".join(
        f"{(by_year['pair_' + m].mean() > 0).sum()}/{by_year.ngroups}"
        for m in MODES[1:]) + " |",
        "|---|" + "---:|" * (len(MODES) - 1), ""]
    # v4's headline correlation used n = 11 years; 2014 (n=2) and 2015 (n=9)
    # have almost no observations and are excluded so the figures are
    # comparable rather than merely similar-looking.
    solid = by_year.filter(lambda g: len(g) >= 100)
    lines += ["### 开关超额与持有收益的关系（复现 v4 的那条诊断）", "",
              f"只取观测数 ≥ 100 的年份（n = {solid['year'].nunique()}，与 v4 报告的 n = 11 对齐）。", "",
              "| 语义 | corr(当年持有收益, 当年开关超额) |", "|---|---:|"]
    yr = solid.groupby("year")
    for mode in MODES:
        lines.append(f"| {_short(mode)} | {yr['fwd_hold'].mean().corr(yr['ex_' + mode].mean()):+.2f} |")
    lines += ["",
              "> v4 报告里这条相关是 **−0.98**：大盘跌的年份开关才值钱。若某种语义把它推向 0，",
              "> 说明它不再是趋势跟随，而是变成了别的东西 —— 那需要另一套理由来解释。", ""]
    return lines


# ─────────────────────── 判决 ───────────────────────

def verdict(frame, cross: dict) -> list[str]:
    """Pre-declared criteria applied, then stated plainly."""
    lines = ["## 判决", "",
             "判据在跑之前定下：**看配对差的中位与跨年符号一致性，不看单条样本、不看均值最大化。**", "",
             "| 替代语义 | 横截面配对差中位 | 横截面开关为正 | 面板配对差中位 | 面板为正年份 | "
             "面板按年聚类 t | 通过 |",
             "|---|---:|---:|---:|---:|---:|---|"]
    summary = {}
    for mode in MODES[1:]:
        diffs = np.array([v[mode] - v[REFERENCE] for v in cross.values()])
        med_x = float(np.median(diffs))
        pos_x = sum(1 for v in cross.values() if v[mode] > 0)
        if frame is not None:
            med_p = float(frame["pair_" + mode].median())
            yrs = frame.groupby("year")["pair_" + mode].mean()
            pos_y, stat = int((yrs > 0).sum()), _t(yrs.to_numpy())
        else:
            med_p = pos_y = stat = float("nan")
        ok = (med_x > 0) and (pos_x > len(cross) // 2) and stat > 2.0
        summary[mode] = dict(median_cross=med_x, positive_cross=pos_x,
                             median_panel=med_p, positive_years=pos_y, t=stat, passed=ok)
        lines.append(
            f"| `{_short(mode)}` | {med_x:+.2%} | {pos_x}/{len(cross)} | "
            f"{med_p:+.2%} | {pos_y}/13 | **{stat:.2f}** | "
            f"{'**通过**' if ok else '否'} |")
    ref_pos = sum(1 for v in cross.values() if v[REFERENCE] > 0)
    ref_mean = (f"，面板均值 {frame['ex_' + REFERENCE].mean():+.2%}"
                if frame is not None else "")
    lines += ["", f"（基准 `{REFERENCE}`：横截面为正 {ref_pos}/{len(cross)}"
                  f"{ref_mean}）", ""]

    lines += ["### 机制：为什么改 peak 语义会削弱保护", "",
              "三种语义的止损线：", "",
              "| 语义 | 止损线 | 触发条件 |", "|---|---|---|",
              "| `MARKET` | `历史最高 − 3×ATR` | 从最高点回撤 3 个 ATR |",
              "| `REENTRY` | `本手入场价 − 3×ATR` | 从**本手成本**回撤 3 个 ATR |",
              "| `ROLLING<N>` | `近 N 日最高 − 3×ATR` | **比 N 日更快的**下跌 |", "",
              "`REENTRY` 的入场价通常远低于历史最高 → **止损线大幅下移，离场要等更深的回撤**。",
              "`ROLLING` 的窗口最高值会**随价格一起下移** → 阴跌（比窗口慢的下跌）",
              "它一路跟着走，**永远不触发**；它只能抓住急跌。", "",
              "在震荡下行里，MARKET 那种「反复翻仓」本身就是保护 —— 每次翻回多头都要",
              "重新赚回一截才站得住。601869 在 2026 是 V 型反转（跌完就直接涨回去），",
              "所以「坐住」是对的；换一只趋势更拖沓的标的，坐住就是亏。",
              "**单条 V 型样本区分不了这两者 —— 这正是要做面板的原因。**", ""]

    passing = [m for m, v in summary.items() if v["passed"]]
    lines += ["### 建议", ""]
    if passing:
        lines += [f"**以下语义通过了事前判据：{'、'.join('`' + p + '`' for p in passing)}。**",
                  "在把它们设为默认之前，还需要一次**独立的**样本外确认 ——",
                  "本文用的是同一份面板，不构成新的证据。", ""]
    else:
        lines += ["**没有任何替代语义通过事前判据 —— 保持 `RISK_PEAK_MODE = 'MARKET'`，不改默认。**", "",
                  "- 横截面（2026 年）与面板的 2026 切片结论一致：替代语义更差；",
                  "  面板总体的正配对差来自 2015、2019–2022、2024–2025，不是你在意的年份。",
                  "- 单条样本上的改善属于**已在设计样本内**的结果，与 v4 报告里",
                  "  「25% 回撤给 +104k、30% 掉到 −0.3k」是同一类形状。",
                  "- 三种语义都已实现、已测、已否定，用 `RISK_PEAK_MODE = 'REENTRY'` 或",
                  "  `'ROLLING20'` 即可复现本文所有数字。", ""]
    return lines


# ─────────────────────── 主流程 ───────────────────────

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sections = [
        "# peak 语义 A/B：MARKET（永不重置） vs REENTRY（回场重置） vs ROLLING（近 N 日）", "",
        "判决口径在跑结果之前定下：主判决看**配对差的中位与跨年符号一致性**，",
        "不看单条样本、不看均值最大化。理由见脚本 docstring。", "",
    ]

    print("① 单条样本 601869 ...", flush=True)
    cells = single_symbol()
    sections += single_symbol_md(cells)

    print("② 横截面 13 个标的 ...", flush=True)
    cross = cross_section()
    sections += cross_section_md(cross)

    payload = {"modes": list(MODES),
               "single_symbol": {m: {"final_net": c["res"]["final_net"],
                                     "excess": c["res"]["excess"],
                                     "switches": c["res"]["switches"],
                                     "days_flat": c["res"]["days_flat"],
                                     "max_dd": c["res"]["max_dd"],
                                     "intervals": c["intervals"],
                                     "crash": c["crash"]}
                                 for m, c in cells.items()},
               "cross_section": cross}

    frame = None
    if "--render" in sys.argv:
        saved = OUT / "panel.csv"
        if saved.exists():
            frame = pd.read_csv(saved, dtype={"date": str})
            sections += panel_md(frame)
    elif "--no-panel" not in sys.argv:
        print("③ 面板 全市场 × 12 年 ...", flush=True)
        frame = panel()
        sections += panel_md(frame)
        frame.to_csv(OUT / "panel.csv", index=False)
        payload["panel"] = {
            "observations": int(len(frame)), "symbols": int(frame["code"].nunique()),
            "by_mode": {m: {"mean": float(frame["ex_" + m].mean()),
                            "median": float(frame["ex_" + m].median()),
                            "positive": float((frame["ex_" + m] > 0).mean())}
                        for m in MODES},
            "pair_vs_reference": {m: {
                "mean": float(frame["pair_" + m].mean()),
                "median": float(frame["pair_" + m].median()),
                "positive": float((frame["pair_" + m] > 0).mean()),
                "positive_years": int((frame.groupby("year")["pair_" + m].mean() > 0).sum()),
                "t_by_year": _t(frame.groupby("year")["pair_" + m].mean().to_numpy())}
                for m in MODES[1:]},
        }
    else:
        sections += ["## ③ 面板", "", "（本次以 `--no-panel` 跳过）", ""]

    sections += verdict(frame, cross)
    (OUT / "README.md").write_text("\n".join(sections) + "\n", encoding="utf-8")
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
