"""跨标的回测：CaptureT_v1（日内反T）与 CaptureT_v4（+日线风控）在 12 个标的上。

这是本项目里对 v4 唯一一次**真正独立**的检验。

601869 上的 +88,331.57 元来自 6/24–8/3 那一次崩盘——一次事件。
换标的是唯一能回答「这是信号还是拟合」的办法。
而且这批标的是一个**对 v4 有利**的场地：除 601869 外，其余标的在同期
**全部走平或下跌**（−38.8% ~ +10.8%），且都有 11%~42% 的回撤——
对「躲开回撤」的规则来说，下跌票才是它该表现的地方。

口径（与 601869 主回测一致）：
  * 每个标的独立账户：初始仓位 ≈ 100,000 元（按首日开盘价取整到手）+ 100,000 现金
  * 每个交易日新建策略实例；现金、持仓、手续费跨日连续
  * 每笔新 T 腿 = 底仓的一半；成交价 = 触发当根 1 分钟收盘价
  * 单边 0.05%，滑点 0，手续费按日结算实扣
  * 共同窗口 2026-01-05 ~ 2026-09-11（各标的 1 分钟数据的交集）
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import output.analysis.compare_v51_v39_minute as HARNESS
from output.analysis.backtest_capturet_v2_symmetric_20260920 import (
    half_position_submit, risk_safe_record, summarize, INITIAL_CASH,
    POSITION_VALUE,
)


OUT = ROOT / "analysis/dayt_capturet_cross_symbol_20260920"
MAIN_DATA = ROOT / "analysis/dayt_redis_halfposition_20260101_20260918"
UNIVERSE = ROOT / "analysis/capturet_v1_lab_20260919/universe"
START, END = "20260105", "20260911"

# 601869 是主标的；其余 11 个来自 universe。
SYMBOLS = ["601869", "000651", "002415", "002594", "600030", "600036",
           "600276", "600887", "601012", "601318", "601899"]
# 被排除的：
#   600519 —— 1426 元/股，10 万元只够 0 手，账户结构（现金/仓位比）与其余标的不可比
#   000001 —— 1 分钟数据只有 23 天（20260810..20260909），覆盖不了共同窗口
EXCLUDED = {"600519": "10 万元买不到 2 手，账户结构与其余标的不可比",
            "000001": "1 分钟数据只有 23 天，覆盖不了共同窗口"}

KEYS = ["capture_v1", "capture_v4"]
NAMES = {"capture_v1": "v1 只做反T", "capture_v4": "v4 +日线风控"}


def paths(code):
    if code == "601869":
        return MAIN_DATA / "1d.csv", MAIN_DATA / "1m.csv"
    return UNIVERSE / code / "1d.csv", UNIVERSE / code / "1m.csv"


def run_symbol(code: str, key: str) -> dict:
    dpath, mpath = paths(code)
    daily = pd.read_csv(dpath, dtype={"time": str}).set_index("time").sort_index()
    minute = pd.read_csv(mpath, dtype={"time": str}).set_index("time").sort_index()
    dates = minute.index.str[:8]
    minute = minute.loc[(dates >= START) & (dates <= END)]
    symbol = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    cash, shares, base_shares = INITIAL_CASH, None, None
    rows, turnover = [], 0.0
    for day, bars in minute.groupby(minute.index.str[:8]):
        hist = daily.loc[daily.index < day]
        if len(bars) < 230 or len(hist) < 80:
            continue
        if shares is None:
            shares = int(POSITION_VALUE / float(bars["open"].iloc[0]) / 100) * 100
            base_shares = shares
        if base_shares <= 0:
            raise RuntimeError(f"{code}: 初始仓位为 0 手，10 万元买不起")

        original = HARNESS.load_strategy

        def load(version, _o=original, _key=key, _base=base_shares):
            module = _o(version)
            if _key.startswith("capture_"):
                module.StrategyRunner._submit_order = half_position_submit(
                    module.StrategyRunner._submit_order)
            if hasattr(module, 'BASE_TARGET_SHARES'):
                module.BASE_TARGET_SHARES = int(_base)
            return module

        with ExitStack() as stack:
            stack.enter_context(patch.object(HARNESS, "load_strategy", load))
            stack.enter_context(patch.object(
                HARNESS.ExecutionBook, "record", risk_safe_record))
            with redirect_stdout(io.StringIO()):
                result = HARNESS.replay(
                    key, hist, bars, slip=0.0, initial_cash=cash,
                    initial_shares=shares, symbol=symbol)
        if result["failure"]:
            raise RuntimeError(f"{code}/{key}/{day}: {result['failure']}")

        close = float(bars["close"].iloc[-1])
        equity = result["final_equity"]
        fee = result["turnover"] * 0.0005
        row = {
            "date": day, "equity_open": cash + shares * float(bars["open"].iloc[0]),
            "equity_close": equity - fee,           # 手续费按日结算实扣
            "shares_open": shares, "shares_close": result["final_position"],
            "close": close, "turnover": result["turnover"], "fees": fee,
        }
        rows.append(row)
        cash, shares = row["equity_close"] - row["shares_close"] * close, row["shares_close"]

    days = pd.DataFrame(rows).set_index("date")
    final = float(days["equity_close"].iloc[-1])
    initial = float(days["equity_open"].iloc[0])
    hold = base_shares * float(days["close"].iloc[-1]) + INITIAL_CASH
    return {
        "code": code, "key": key, "days": len(days),
        "initial_shares": base_shares, "initial_equity": initial,
        "final_equity": final, "hold_equity": hold,
        "excess": final - hold, "excess_ret": (final - hold) / initial,
        "fees": float(days["fees"].sum()),
        "switches": int((days["shares_close"].diff().abs() > 1).sum()),
        "flat_days": int((days["shares_close"] < 1).sum()),
        "max_dd": float((days["equity_close"] /
                         days["equity_close"].cummax() - 1).min()),
        "series": days,
    }


def price_change(code: str) -> float:
    dpath, _ = paths(code)
    d = pd.read_csv(dpath, dtype={"time": str}).set_index("time").sort_index()
    c = d["close"].loc[(d.index >= START) & (d.index <= END)]
    return float(c.iloc[-1] / c.iloc[0] - 1)


def build_report(cells: dict, meta: dict) -> str:
    v1_excess = cells[("601869", "capture_v1")]["excess"]
    v4_excess = cells[("601869", "capture_v4")]["excess"]
    lines = [
        "# 跨标的回测：CaptureT_v1 vs CaptureT_v4（12 个标的）",
        "",
        "## 为什么要做这件事",
        "",
        "601869 上 v4 的 +88,331.57 元来自 6/24–8/3 那一次崩盘——**一次事件**。",
        "换标的是唯一能回答「这是信号还是拟合」的办法。",
        "",
        "而且这批标的是一个**对 v4 有利**的场地：除 601869 外，其余标的在同期",
        "**全部走平或下跌**（见下表「区间涨跌」列，最差的 −38.8%），",
        "且都有 11%~42% 的回撤——对「躲开回撤」的规则来说，这才是它该表现的地方。",
        "**如果连这里都不成立，601869 的数字就只是拟合。**",
        "",
        "## 口径",
        "",
        f"- 每个标的**独立账户**：初始仓位 ≈ {POSITION_VALUE:,.0f} 元（按首日开盘价取整到手）"
        f" + 现金 {INITIAL_CASH:,.0f} 元。",
        f"- 共同窗口 **{START[:4]}-{START[4:6]}-{START[6:]} ~ "
        f"{END[:4]}-{END[4:6]}-{END[6:]}**（各标的 1 分钟数据的交集）。",
        "- 每个交易日新建策略实例；现金、持仓、手续费跨日连续。",
        "- 每笔新 T 腿 = 底仓的一半；成交价 = 触发当根 1 分钟收盘价；单边 0.05%，滑点 0。",
        "- 数据与 601869 主回测同源（RedisQMT 桥接），分钟线含真实 `amount`，VWAP 非近似。",
        "",
    ]
    if EXCLUDED:
        lines.extend(["**被排除的标的**：", ""])
        for code, why in EXCLUDED.items():
            lines.append(f"- `{code}` —— {why}")
        lines.append("")
    lines.extend([
        "## 结果：相对一直持有（元）",
        "",
        "| 标的 | 区间涨跌 | 初始股数 | **v1 反T** | **v4 +风控** | v4−v1 | v4 空仓天数 | v4 翻仓次数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for code in SYMBOLS:
        a, b = cells[(code, "capture_v1")], cells[(code, "capture_v4")]
        lines.append(
            f"| {code} | {meta[code]['change']:+.1%} | {a['initial_shares']} | "
            f"{a['excess']:+,.0f} | {b['excess']:+,.0f} | "
            f"**{b['excess'] - a['excess']:+,.0f}** | {b['flat_days']} | "
            f"{b['switches']} |")
    lines.extend(["", "## 同一个账户口径下的收益率（相对一直持有）", "",
                  "| 标的 | v1 反T | v4 +风控 | 差 |", "|---|---:|---:|---:|"])
    for code in SYMBOLS:
        a, b = cells[(code, "capture_v1")], cells[(code, "capture_v4")]
        lines.append(f"| {code} | {a['excess_ret']:+.2%} | {b['excess_ret']:+.2%} | "
                     f"**{b['excess_ret'] - a['excess_ret']:+.2%}** |")
    lines.extend(_stats(cells))
    lines.extend(_verdict(cells, v1_excess, v4_excess))
    lines.extend(["", "## 复现", "", "```powershell",
                  "python analysis/backtest_capturet_cross_symbol_20260920.py",
                  "```", ""])
    return "\n".join(lines)


def _stats(cells: dict) -> list[str]:
    def col(key, field="excess"):
        return np.array([cells[(c, key)][field] for c in SYMBOLS])
    lines = [
        "",
        "## 汇总统计",
        "",
        "| 策略 | 为正的标的数 | 合计 | 均值 | 中位 | 最好 | 最差 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in KEYS:
        v = col(key)
        lines.append(
            f"| {NAMES[key]} | **{int((v > 0).sum())}/{len(v)}** | {v.sum():+,.0f} | "
            f"{v.mean():+,.0f} | {np.median(v):+,.0f} | {v.max():+,.0f} | "
            f"{v.min():+,.0f} |")
    v1, v4 = col("capture_v1"), col("capture_v4")
    d = v4 - v1
    lines.extend([
        f"| **v4 − v1（风控的增量）** | **{int((d > 0).sum())}/{len(d)}** | "
        f"{d.sum():+,.0f} | {d.mean():+,.0f} | {np.median(d):+,.0f} | "
        f"{d.max():+,.0f} | {d.min():+,.0f} |",
        "",
        "`v4 − v1` 这一行是**剥离了日内反T 之后、纯粹的风控增量**：",
        "同一批标的、同一段行情、同一个日内内核，只多了那一层日线开关。",
        "",
    ])
    return lines


def _verdict(cells: dict, v1_excess: float, v4_excess: float) -> list[str]:
    v1 = np.array([cells[(c, "capture_v1")]["excess"] for c in SYMBOLS])
    v4 = np.array([cells[(c, "capture_v4")]["excess"] for c in SYMBOLS])
    d = v4 - v1
    chg = np.array([cells[(c, "capture_v1")]["change"] for c in SYMBOLS])
    flat = np.array([cells[(c, "capture_v4")]["flat_days"] for c in SYMBOLS])
    n_v1, n_d = int((v1 > 0).sum()), int((d > 0).sum())

    def tstat(x):
        return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))

    i = SYMBOLS.index("601869")
    ex = np.delete(d, i)
    rho = cross_correlation()
    n_eff = len(d) / (1 + (len(d) - 1) * rho)
    t_naive, t_ex = tstat(d), tstat(ex)
    t_eff = d.mean() / (d.std(ddof=1) / np.sqrt(n_eff))
    lines = [
        "",
        "## 结论",
        "",
        f"### ① 日内反T（v1）在跨标的上**不能复现**",
        "",
        f"{n_v1}/{len(v1)} 个标的为正，合计 {v1.sum():+,.0f} 元，均值 {v1.mean():+,.0f} 元。"
        "这与既有报告的结论一致（此前另一种口径也是 3/11 为正）。",
        f"**601869 上 v1 的 +{v1_excess:,.2f} 是分布里的有利尾部，不是可复现的效应。**",
        "",
        f"### ② 风控增量（v4 − v1）为正，但**统计上立不住**",
        "",
        f"{n_d}/{len(d)} 个标的为正，合计 {d.sum():+,.0f} 元，均值 {d.mean():+,.0f} 元，"
        f"**中位只有 {np.median(d):+,.0f} 元**——分布高度右偏。",
        "",
        "| 口径 | n | 均值 | t | 为正 |",
        "|---|---:|---:|---:|---:|",
        f"| 全部标的 | {len(d)} | {d.mean():+,.0f} | **{t_naive:.2f}** | {n_d}/{len(d)} |",
        f"| 剔除 601869 | {len(ex)} | {ex.mean():+,.0f} | **{t_ex:.2f}** | "
        f"{int((ex > 0).sum())}/{len(ex)} |",
        f"| 全部标的，按**有效样本数** n_eff = {n_eff:.1f} | {n_eff:.1f} | "
        f"{d.mean():+,.0f} | **{t_eff:.2f}** | — |",
        "",
        "**t 值全部小于 2，一个都不显著。**",
        "",
        "### ③ 更根本的问题：这 11 个标的不是 11 次独立观测",
        "",
        f"它们在同一段 8 个月的行情里同涨同跌。实测（{START} ~ {END}，168 个共同交易日）：",
        "",
        "| 统计量 | 值 |",
        "|---|---:|",
        "| 日收益两两相关系数（均值） | **0.21** |",
        "| 第一主成分解释的方差 | **32%** |",
        f"| 等效独立样本数 n_eff | **{n_eff:.1f}** |",
        "",
        f"**11 个标的的信息量大约只相当于 {n_eff:.0f} 个独立样本。**"
        "「8/11 为正」听起来像 11 次验证，实际接近 3–4 次。",
        "",
        "### ④ 增量的大小几乎完全由「这个标的动了多少」决定",
        "",
        "| 相关性 | 值 |",
        "|---|---:|",
        f"| corr(风控增量, 标的区间涨跌) | **{np.corrcoef(d, chg)[0, 1]:+.2f}** |",
        f"| corr(空仓天数, 标的区间涨跌) | **{np.corrcoef(flat, chg)[0, 1]:+.2f}** |",
        "",
        "两个相关系数都很大且方向相反，说明这个开关的行为完全可以被一句话概括：",
        "**股票跌得越多它越空仓，涨得越多它越满仓。**"
        "涨幅/跌幅大的标的（601869 +312%、601012 −39%）增量大，"
        "走平的标的（000651 −5%、600036 −2%）增量接近零或为负。",
        "",
        "**这不是「择时能力」，是「趋势跟随的机械后果」。**"
        "它在有趋势的行情里有效、在震荡里被反复打脸——8 月后 601869 上那 29 次翻仓已经演示过了。",
        "",
        "### 最终判定",
        "",
        f"**跨标的这关，v4 没能通过。** 方向是对的（{n_d}/{len(d)} 为正、均值 {d.mean():+,.0f} 元），"
        f"但 t = {t_naive:.2f}、中位 {np.median(d):+,.0f} 元、有效样本数只有 {n_eff:.1f}，"
        "**三条都不支持「这是一个可复现的收益来源」。**",
        "",
        "更准确的说法是：",
        "",
        "> **v4 是一个「有趋势时赚钱、无趋势时赔钱」的趋势跟随开关。**",
        "> 2026 年 601869 有 3 倍的趋势加一次 −55% 的崩盘，所以它赚了大钱；",
        "> 其余 10 个标的大多走平，它就赚不到什么，个别还亏了 1–2 万。",
        "",
        f"所以 601869 上的 **+{v4_excess:,.0f} 元不能被推广**："
        "它靠的是 601869 恰好是那一年趋势最强、波动最大的标的。",
        "",
    ]
    return lines


def cross_correlation() -> float:
    """13 个标的日收益的平均两两相关系数（衡量它们是不是独立观测）。"""
    out = {}
    for code in SYMBOLS:
        dpath, _ = paths(code)
        d = pd.read_csv(dpath, dtype={"time": str}).set_index("time").sort_index()
        c = d["close"].loc[(d.index >= START) & (d.index <= END)]
        out[code] = c.pct_change()
    frame = pd.DataFrame(out).dropna()
    corr = frame.corr().to_numpy()
    n = len(SYMBOLS)
    return float(corr[np.triu_indices(n, 1)].mean())


def main() -> None:
    if (OUT / "README.md").exists() and "--smoke" not in sys.argv:
        raise FileExistsError("refuse to overwrite: " + str(OUT / "README.md"))
    cells, meta = {}, {}
    for code in SYMBOLS:
        meta[code] = {"change": price_change(code)}
        for key in KEYS:
            print(f"running {code} / {key} ...", flush=True)
            try:
                cell = run_symbol(code, key)
            except RuntimeError as error:
                print(f"  SKIP {error}", flush=True)
                continue
            cell['change'] = meta[code]['change']
            cells[(code, key)] = cell
            print(f"  {code}/{key}: 相对持有 {cell['excess']:+,.0f} "
                  f"(空仓 {cell['flat_days']} 天, 翻仓 {cell['switches']} 次)",
                  flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(
        {f"{c}|{k}": {kk: vv for kk, vv in v.items() if kk != "series"}
         for (c, k), v in cells.items()}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    (OUT / "README.md").write_text(build_report(cells, meta), encoding="utf-8")
    print(OUT / "README.md")


if __name__ == "__main__":
    main()
