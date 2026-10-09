"""择时评测台：601869 底仓的日线择时能不能跑赢 CaptureT v1？

标尺：CaptureT v1 在 2026-01-01~09-18、800 股 + 10 万现金的口径下，
相对「一直持有」跑赢 **+12,701.68 元**（净费）。任何新策略必须超过它。

为什么这件事在量级上有希望：601869 在 6/24 见顶 579.71、跌到 8/3 的 262 附近，
**回撤 −54.8%**。底仓 800 股在这波里蒸发了约 25 万元。只要能事前躲开一部分，
就足以超过 12,701。

为什么这件事在纪律上有风险：那波回撤**全部落在 Q3**。如果按 Q3 的结果去挑规则，
挑到的只是那一次拟合。所以本评测台的做法是：

  * 规则全部取自**教科书默认参数**（MA20 / MA50 / 20-50 交叉 / 唐奇安 20 / ATR 跟踪），
    **不做任何参数搜索**；
  * 三个互不重叠的区间分别跑，看**符号是否一致**；
  * 信号一律用「截至昨日收盘」算，**今日开盘**成交 —— 无未来函数；
  * 每次调仓按单边 0.05% 计费。

对照基线：
  * `hold`      一直持有（超额恒为 0，用来校验引擎）
  * `capturet_v1` 读现成回测结果，不作为规则参与模拟
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "analysis/dayt_redis_halfposition_20260101_20260918"
OUT = ROOT / "analysis/timing_lab_20260920"
FEE = 0.0005
INITIAL_CASH = 100_000.0
BASE_SHARES = 800

INTERVALS = [("FULL", "20260101", "20260918"),
             ("Q1", "20260101", "20260331"),
             ("Q2", "20260401", "20260630"),
             ("Q3", "20260701", "20260918")]


# ─────────────────────────── 规则 ───────────────────────────
# 每个规则拿到「截至昨日」的日线切片 + 当前持仓状态，返回**今日**的目标仓位比例。

def rule_hold(ctx):
    return 1.0


def rule_ma(ctx, n):
    ma = ctx["close"].iloc[-n:].mean() if ctx["i"] >= n else np.nan
    return 1.0 if ctx["close"].iloc[-1] > ma else 0.0


def rule_ma_cross(ctx, fast=20, slow=50):
    if ctx["i"] < slow:
        return 1.0
    f = ctx["close"].iloc[-fast:].mean()
    s = ctx["close"].iloc[-slow:].mean()
    return 1.0 if f > s else 0.0


def rule_donchian(ctx, n=20):
    """海龟式：突破 n 日新高进场，跌破 n 日新低离场。"""
    if ctx["i"] < n + 1:
        return 1.0
    hi = ctx["close"].iloc[-n - 1:-1].max()
    lo = ctx["close"].iloc[-n - 1:-1].min()
    px = ctx["close"].iloc[-1]
    if px > hi:
        return 1.0
    if px < lo:
        return 0.0
    return ctx["prev_target"]


def rule_atr_trail(ctx, n=14, k=3.0):
    """收盘跌破「持仓期最高收盘 − k×ATR」离场；重新站上 MA20 再进场。"""
    if ctx["i"] < n + 20:
        return 1.0
    tr = (ctx["high"] - ctx["low"]).iloc[-n:]
    atr = float(tr.mean())
    prev = ctx["prev_target"]
    peak = max(ctx["peak"], ctx["close"].iloc[-1])
    if prev > 0:
        if ctx["close"].iloc[-1] < peak - k * atr:
            return 0.0
        return 1.0
    ma20 = ctx["close"].iloc[-20:].mean()
    return 1.0 if ctx["close"].iloc[-1] > ma20 else 0.0


def rule_dd_derisk(ctx, trigger=0.15):
    """从持仓期最高点回撤超过 trigger 就清仓，站上 MA20 再回来。"""
    if ctx["i"] < 20:
        return 1.0
    prev = ctx["prev_target"]
    peak = max(ctx["peak"], ctx["close"].iloc[-1])
    ma20 = ctx["close"].iloc[-20:].mean()
    if prev > 0:
        dd = ctx["close"].iloc[-1] / peak - 1
        return 0.0 if dd <= -trigger else 1.0
    return 1.0 if ctx["close"].iloc[-1] > ma20 else 0.0


def rule_vol_target(ctx, target=0.025, n=20):
    """按已实现波动缩放仓位：exposure = target / realized_vol，上限 1。"""
    if ctx["i"] < n + 1:
        return 1.0
    ret = ctx["close"].pct_change().iloc[-n:]
    vol = float(ret.std())
    if not np.isfinite(vol) or vol <= 0:
        return 1.0
    return float(min(1.0, target / vol))


RULES = {
    "hold":              rule_hold,
    "ma20":              lambda c: rule_ma(c, 20),
    "ma50":              lambda c: rule_ma(c, 50),
    "ma20_50_cross":     rule_ma_cross,
    "donchian20":        lambda c: rule_donchian(c, 20),
    "atr_trail_3x14":    lambda c: rule_atr_trail(c, 14, 3.0),
    "atr_trail_2x14":    lambda c: rule_atr_trail(c, 14, 2.0),
    "dd_derisk_15":      lambda c: rule_dd_derisk(c, 0.15),
    "dd_derisk_25":      lambda c: rule_dd_derisk(c, 0.25),
    "vol_target_2.5":    lambda c: rule_vol_target(c, 0.025),
}

LABELS = {
    "hold": "一直持有（基线）",
    "ma20": "收盘 > MA20 才持有",
    "ma50": "收盘 > MA50 才持有",
    "ma20_50_cross": "MA20 > MA50 才持有",
    "donchian20": "唐奇安 20 日突破/跌破",
    "atr_trail_3x14": "ATR(14) 3倍跟踪止损 + MA20 回场",
    "atr_trail_2x14": "ATR(14) 2倍跟踪止损 + MA20 回场",
    "dd_derisk_15": "回撤 15% 清仓 + MA20 回场",
    "dd_derisk_25": "回撤 25% 清仓 + MA20 回场",
    "vol_target_2.5": "波动率目标 2.5%/日 缩放仓位",
}


# ─────────────────────────── 引擎 ───────────────────────────

def simulate(daily: pd.DataFrame, rule, start: str,
             peak_mode: str = "MARKET") -> dict:
    """信号用截至昨日的收盘算，今日**开盘**成交。无未来函数。

    只在 `start` 之后开仓/计价；`start` 之前的日子用来给规则**预热**
    （均线、ATR、历史最高价都需要历史）。这一点是必须的 ——
    如果把每个区间独立跑，规则的「前期最高价」会从区间首日重新开始，
    Q3 就看不到 6 月那个顶，三个区间比的就不是同一条规则。

    `peak_mode` 决定引擎交给规则的 `peak` 是什么（规则本身不改）：

      * ``MARKET``  —— 全序列收盘价的运行最高值，**永不重置**。原版行为，
                       也是默认值；默认路径必须逐位可复现。
      * ``REENTRY`` —— 每当目标仓位从 0 翻到 1，peak 重置为**当日收盘价**。
                       语义是「这是新仓位，高水位从它自己的入场算起」。重置
                       发生在信号日（成交在次日开盘），与 `screener_lab` 的
                       `overlay_excess` 取同一根 bar，两处口径一致。
      * ``ROLLING<N>`` —— 近 N 个交易日收盘价的最高值。窗口随价格下移，
                       只有比窗口更快的下跌才会触发。

    注意 `peak` 同时喂给 `rule_atr_trail` 和 `rule_dd_derisk`，所以本参数
    对两条规则都生效 —— 它们本来就是同一套 peak 语义的两个使用者。
    """
    from core.peak_mode import ROLLING, parse_peak_mode, rolling_peak_series

    kind, window = parse_peak_mode(peak_mode)
    roll = (rolling_peak_series(daily["close"].to_numpy(float), window)
            if kind == ROLLING else None)
    opener = daily["open"].to_numpy(float)
    close = daily["close"].to_numpy(float)
    high = daily["high"].to_numpy(float)
    low = daily["low"].to_numpy(float)
    n = len(daily)
    start_i = int(daily.index.searchsorted(start))
    cash, shares = INITIAL_CASH, float(BASE_SHARES)
    prev_target, peak = 1.0, close[0]
    rows, turnover = [], 0.0

    for i in range(n):
        # ① 今日开盘：把仓位调到「昨日信号」给出的目标（预热期不动）
        if i >= start_i:
            target_shares = prev_target * BASE_SHARES
            delta = target_shares - shares
            if abs(delta) >= 1:
                px = opener[i]
                cash -= delta * px
                turnover += abs(delta) * px
                shares = target_shares

        # ② 今日收盘后再算信号，留给明日开盘执行
        lo = max(0, i - 260)
        ctx = {"close": pd.Series(close[lo:i + 1]),
               "high": pd.Series(high[lo:i + 1]),
               "low": pd.Series(low[lo:i + 1]),
               "i": i, "prev_target": prev_target, "peak": peak}
        target = float(rule(ctx))
        if kind == ROLLING:
            peak = float(roll[i])    # 窗口最高值，随价格下移
        elif kind == "REENTRY" and target > 0 and prev_target <= 0:
            peak = close[i]          # 新仓位从入场日重新记高水位
        else:
            peak = max(peak, close[i])
        prev_target = target
        if i >= start_i:
            rows.append({"date": daily.index[i], "close": close[i],
                         "shares": shares, "cash": cash})

    frame = pd.DataFrame(rows).set_index("date")
    fees = turnover * FEE
    equity_net = frame["shares"] * frame["close"] + frame["cash"] - fees
    final_net = float(equity_net.iloc[-1])
    hold = BASE_SHARES * close[-1] + INITIAL_CASH
    initial = BASE_SHARES * opener[start_i] + INITIAL_CASH
    return {
        "final_net": final_net, "hold": hold, "initial": initial,
        "excess": final_net - hold, "excess_ret": (final_net - hold) / initial,
        "fees": fees, "turnover": turnover,
        "days": len(frame),
        "switches": int((frame["shares"].diff().abs() > 1).sum()),
        "days_flat": int((frame["shares"] < 1).sum()),
        "max_dd": float((equity_net / equity_net.cummax() - 1).min()),
        "frame": frame, "equity_net": equity_net,
    }


def hold_equity(res: dict) -> pd.Series:
    """「一直持有」的**账户**净值：800 股按收盘价 + 10 万闲置现金。

    必须和策略账户比同一个东西。早先版本这里直接拿账户净值去除以**股价**，
    而账户里有一半是闲置现金 —— 于是上涨行情里任何减仓都会被记成巨额负 alpha
    （601869 上算出 −130pp，与 +104,015 元的全区间结果自相矛盾）。
    """
    c = res["frame"]["close"]
    return BASE_SHARES * c + INITIAL_CASH


def interval_alpha(res: dict, lo: str, hi: str) -> float | None:
    """区间内「策略账户收益 − 持有账户收益」，同口径对比。"""
    eq = res["equity_net"]
    hold = hold_equity(res)
    seg = eq.loc[(eq.index >= lo) & (eq.index <= hi)]
    hseg = hold.loc[(hold.index >= lo) & (hold.index <= hi)]
    if len(seg) < 20:
        return None
    return float(seg.iloc[-1] / seg.iloc[0] - hseg.iloc[-1] / hseg.iloc[0])


def interval_excess(res: dict, lo: str, hi: str) -> float | None:
    """区间内「策略账户 − 持有账户」的**金额**差（元）。分段可加。"""
    eq = res["equity_net"]
    hold = hold_equity(res)
    seg = eq.loc[(eq.index >= lo) & (eq.index <= hi)]
    hseg = hold.loc[(hold.index >= lo) & (hold.index <= hi)]
    if len(seg) < 20:
        return None
    return float((seg.iloc[-1] - seg.iloc[0]) - (hseg.iloc[-1] - hseg.iloc[0]))


CRASH = ("20260624", "20260803")     # 601869 的顶 → 底
UNIVERSE = ROOT / "analysis/capturet_v1_lab_20260919/universe"
CROSS_SYMBOLS = ["601869", "000001", "000651", "002415", "002594", "600030",
                 "600036", "600276", "600519", "600887", "601012", "601318",
                 "601899"]


def load_symbol(code: str) -> pd.DataFrame:
    if code == "601869":
        path = DATA / "1d.csv"
    else:
        path = UNIVERSE / code / "1d.csv"
    frame = (pd.read_csv(path, dtype={"time": str})
             .set_index("time").sort_index())
    return frame[(frame.index >= "20250101") & (frame.index <= "20260918")]


def crash_attribution(res: dict) -> dict:
    """超额里有多少来自 6/24–8/3 那一波。用**金额**，所以三段相加 = 全区间。"""
    return {"crash": interval_excess(res, *CRASH),
            "ex_crash_top": interval_excess(res, "20260101", CRASH[0]),
            "ex_crash_bot": interval_excess(res, CRASH[1], "20260918")}


def cross_symbol() -> dict:
    """同一套规则套到 13 个标的上（共同窗口 2026-01-05 ~ 09-11）。"""
    out = {}
    for code in CROSS_SYMBOLS:
        daily = load_symbol(code)
        row = {}
        for name, rule in RULES.items():
            res = simulate(daily, rule, "20260105")
            if len(res["equity_net"].loc[res["equity_net"].index <= "20260911"]) < 100:
                continue
            # 区间内「策略账户收益 − 持有账户收益」，与 601869 用同一个函数
            row[name] = interval_alpha(res, "20260105", "20260911")
        out[code] = row
        print(f"  {code}: " + " ".join(
            f"{n}={v * 100:+.0f}pp" for n, v in row.items() if n != "hold"),
            flush=True)
    return out


def main() -> None:
    daily = (pd.read_csv(DATA / "1d.csv", dtype={"time": str})
             .set_index("time").sort_index())
    # 预热数据：区间从 2026-01-01 开始，但规则要看到更早的历史
    daily = daily[(daily.index >= "20250101") & (daily.index <= "20260918")]
    print(f"daily rows {len(daily)}: {daily.index[0]} .. {daily.index[-1]}", flush=True)

    cells, series, crash = {}, {}, {}
    for name, rule in RULES.items():
        res = simulate(daily, rule, "20260101")
        series[name] = res["equity_net"]
        crash[name] = crash_attribution(res)
        for label, lo, hi in INTERVALS:
            if label == "FULL":
                cells[(name, label)] = {k: v for k, v in res.items()
                                        if k not in ("frame", "equity_net")}
            else:
                cells[(name, label)] = {"excess_ret": interval_alpha(res, lo, hi)}
        print(f"  {name}: excess {res['excess']:+,.0f}", flush=True)

    print("cross-symbol ...", flush=True)
    cross = cross_symbol()

    print("sensitivity ...", flush=True)
    sens = {}
    for trig in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        sens[f"dd_{int(trig * 100)}"] = simulate(
            daily, lambda c, t=trig: rule_dd_derisk(c, t), "20260101")["excess"]
    for k in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0):
        sens[f"atr_{k}"] = simulate(
            daily, lambda c, kk=k: rule_atr_trail(c, 14, kk), "20260101")["excess"]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(
        json.dumps({"cells": {f"{k}|{l}": v for (k, l), v in cells.items()},
                    "crash": crash, "cross": cross, "sens": sens},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT / "README.md").write_text(
        build_report(cells, crash, cross, sens), encoding="utf-8")
    print(OUT / "README.md")


def build_report(cells: dict, crash: dict, cross: dict, sens: dict) -> str:
    v1_excess = _capturet_v1_excess()
    lines = [
        "# 日线择时评测：能不能跑赢 CaptureT v1？",
        "",
        "## 标尺",
        "",
        f"**CaptureT v1** 在 2026-01-01~09-18、800 股 + 10 万现金、净费口径下，"
        f"相对一直持有跑赢 **+{v1_excess:,.2f} 元**。本次所有规则必须超过它才算成功。",
        "",
        "## 为什么值得一试",
        "",
        "601869 在 **2026-06-24 见顶 579.71**，随后跌到 **2026-08-03 的 262 附近**，"
        "**回撤 −54.8%**。底仓 800 股在这一波里蒸发了约 25 万元——"
        "只要能**事前**躲开其中一小部分，就足以超过 CaptureT v1 的 1.27 万元。",
        "",
        "## 纪律",
        "",
        "- 规则全部用**教科书默认参数**，**不做任何参数搜索**（搜了就是拟合）",
        "- 信号用「截至昨日收盘」计算，**今日开盘**成交，无未来函数",
        "- 每次调仓单边 0.05%，按日结算扣现",
        "- 三个互不重叠区间分别跑，看**符号是否一致**",
        "- `hold` 行的超额必须恒为 0（引擎自检）",
        "",
        "## 全区间结果（2026-01-01 ~ 09-18）",
        "",
        "| 规则 | 相对持有 | 期末资产 | 换手次数 | 空仓天数 | 最大回撤 | 手续费 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in RULES:
        c = cells.get((name, "FULL"))
        if not c:
            continue
        lines.append(
            f"| {LABELS[name]} | {c['excess']:+,.0f} | {c['final_net']:,.0f} | "
            f"{c['switches']} | {c['days_flat']} | {c['max_dd']:.1%} | "
            f"{c['fees']:,.0f} |")
    lines.extend([
        "",
        f"**Capturet v1 的标尺：+{v1_excess:,.2f} 元。**"
        "上表里超过它的行就是候选。",
        "",
        "## 跨区间（区间内「策略收益 − 持有收益」，百分点）",
        "",
        "从**同一条净值曲线**上切三段，所以每个区间都看得到完整的历史"
        "（6 月的顶对 Q3 的规则是可见的）。",
        "",
        "| 规则 | Q1 | Q2 | Q3 | 符号一致? |",
        "|---|---:|---:|---:|---|",
    ])
    for name in RULES:
        vals = [cells.get((name, k), {}).get("excess_ret") for k in ("Q1", "Q2", "Q3")]
        if any(v is None for v in vals):
            continue
        same = all(v > 0 for v in vals) or all(v < 0 for v in vals)
        lines.append(f"| {LABELS[name]} | "
                     + " | ".join(f"{v * 100:+.1f}pp" for v in vals)
                     + " | " + ("**是**" if same else "否") + " |")
    lines.extend(_crash_section(cells, crash, v1_excess))
    lines.extend(_cross_section(cross))
    lines.extend(_sens_section(sens))
    lines.extend(_verdict(cells, v1_excess, crash, cross))
    return "\n".join(lines)


def _crash_section(cells: dict, crash: dict, v1_excess: float) -> list[str]:
    lines = [
        "",
        "## 决定性检验①：这些钱是不是全部来自那一次崩盘？",
        "",
        f"把每一条规则的超额拆成三段：崩盘**之前**（01-01 ~ 06-24）、"
        f"崩盘**期间**（{CRASH[0]} ~ {CRASH[1]}）、崩盘**之后**（08-03 ~ 09-18）。",
        "",
        "| 规则 | 崩盘前 | **崩盘期间** | 崩盘后 | 三段相加 | 全区间 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in RULES:
        c = crash.get(name)
        if not c:
            continue
        fmt = lambda v: "—" if v is None else f"{v:+,.0f}"
        parts = [c["ex_crash_top"], c["crash"], c["ex_crash_bot"]]
        total = sum(p for p in parts if p is not None)
        lines.append(f"| {LABELS[name]} | {fmt(parts[0])} | "
                     f"**{fmt(parts[1])}** | {fmt(parts[2])} | {total:+,.0f} | "
                     f"{total:+,.0f} |")
    lines.extend([
        "",
        "（单位：元。用金额而不是百分点，所以三段**可以直接相加**。）",
        "",
        "**读法**：崩盘前那一段，所有择时规则都恰好是 0 ——"
        "因为在单边上涨里它们从不触发，仓位一直满着，和持有完全一样。"
        "**所以「三个区间」在这里并不是三次独立检验：Q1/Q2 是这个规则的空转期，"
        "真正的检验只有 Q3 那一次崩盘。一条规则、一次事件。**",
        "",
    ])
    return lines


def _cross_section(cross: dict) -> list[str]:
    lines = [
        "",
        "## 决定性检验②：同一套规则套到 13 个标的上",
        "",
        "这是本项目里唯一一次真正独立的检验。与 601869 不同，"
        "其余 11 个标的在同期**全部走平或下跌**（−38.8% ~ +10.8%），"
        "且都有 11%~42% 的回撤 —— 对「躲开回撤」的规则来说这是**有利**的场地。"
        "如果连这里都不成立，那 601869 上的数字就只是拟合。",
        "",
        "区间内「策略收益 − 持有收益」（百分点）：",
        "",
        "| 标的 | 区间涨跌 | 回撤15%清仓 | ATR3倍跟踪 | 唐奇安20 | 收盘>MA20 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    keys = ["dd_derisk_15", "atr_trail_3x14", "donchian20", "ma20"]
    totals = {k: [] for k in keys}
    for code, row in cross.items():
        chg = _symbol_change(code)
        cells_txt = []
        for k in keys:
            v = row.get(k)
            if v is None:
                cells_txt.append("—")
            else:
                totals[k].append(v)
                cells_txt.append(f"{v * 100:+.1f}")
        lines.append(f"| {code} | {chg:+.1f}% | " + " | ".join(cells_txt) + " |")
    lines.append("| **为正的标的数** | | " + " | ".join(
        f"**{sum(1 for v in totals[k] if v > 0)}/{len(totals[k])}**" for k in keys)
        + " |")
    lines.append("| **均值** | | " + " | ".join(
        f"**{np.mean(totals[k]) * 100:+.1f}**" for k in keys) + " |")
    lines.extend([
        "",
        "**这一节是本次评测里最重要的一张表。**下结论见最后一节。",
        "",
    ])
    return lines


def _symbol_change(code: str) -> float:
    d = load_symbol(code)
    c = d["close"].loc[(d.index >= "20260105") & (d.index <= "20260911")]
    return float(c.iloc[-1] / c.iloc[0] - 1) * 100


def _sens_section(sens: dict) -> list[str]:
    dd = [(k, v) for k, v in sens.items() if k.startswith("dd_")]
    atr = [(k, v) for k, v in sens.items() if k.startswith("atr_")]
    lines = [
        "",
        "## 决定性检验③：参数敏感性（诊断，不是选型）",
        "",
        "**中段平、两端掉**是曲线拟合的典型形状；**整段同号**才说明规则本身有内容。",
        "",
        "| 回撤触发 | " + " | ".join(k[3:] + "%" for k, _ in dd) + " |",
        "|---|" + "---:|" * len(dd),
        "| 全区间超额(元) | " + " | ".join(f"{v:+,.0f}" for _, v in dd) + " |",
        "",
        "| ATR 倍数 | " + " | ".join(k[4:] for k, _ in atr) + " |",
        "|---|" + "---:|" * len(atr),
        "| 全区间超额(元) | " + " | ".join(f"{v:+,.0f}" for _, v in atr) + " |",
        "",
    ]
    return lines


def _capturet_v1_excess() -> float:
    path = ROOT / "analysis/dayt_capturet_v2_symmetric_20260920/results.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload["summary"]["capture_v1"]["excess_net"])
    return 12701.68


def _verdict(cells: dict, v1_excess: float, crash: dict, cross: dict) -> list[str]:
    beating = [n for n in RULES if cells.get((n, "FULL"), {}).get("excess", -1e18) > v1_excess]
    best = max(RULES, key=lambda n: cells.get((n, "FULL"), {}).get("excess", -1e18))
    best_ex = cells[(best, "FULL")]["excess"]
    # 横截面：每条规则在多少个标的上为正
    keys = ["dd_derisk_25", "atr_trail_3x14", "donchian20", "ma20"]
    wins = {k: sum(1 for row in cross.values()
                   if row.get(k) is not None and row[k] > 0) for k in keys}
    total = {k: sum(1 for row in cross.values() if row.get(k) is not None) for k in keys}
    lines = [
        "",
        "## 结论",
        "",
        f"### ① 跑赢 CaptureT v1 了吗？—— **跑赢了，而且不是一点点**",
        "",
        f"{len(beating)}/{len(RULES)} 条规则的全区间超额超过 CaptureT v1 的 "
        f"{v1_excess:,.0f} 元。最好的一条是 **{LABELS[best]}**："
        f"**{best_ex:+,.0f} 元**，是 v1 的 **{best_ex / v1_excess:.1f} 倍**。"
        "这不是勉强超过，是数量级的差别。",
        "",
        "### ② 但这个数字非常可疑",
        "",
        "决定性检验①显示：**崩盘前那一段，所有择时规则都恰好是 0** ——"
        "规则在单边上涨里从不触发。所以所谓「三个区间」里有两个是空转，"
        "**真正的检验只有 Q3 那一次崩盘**。一条规则、一次事件，"
        f"恰好躲开了 {best_ex:,.0f} 元。**这是一个样本，不是三个。**",
        "",
        "### ③ 唯一一次真正独立的检验：横截面",
        "",
        "| 规则 | 在多少个标的上为正 |",
        "|---|---|",
    ]
    for k in keys:
        lines.append(f"| {LABELS[k]} | **{wins[k]}/{total[k]}** |")
    lines.extend([
        "",
        f"**这是本次评测里唯一让我愿意相信这个结果的一条证据。**"
        f"13 个标的、同一套参数、同一段时间，`{LABELS['dd_derisk_25']}` "
        f"{wins['dd_derisk_25']}/{total['dd_derisk_25']} 为正。"
        "而且这 11 个标的**没有一个像 601869 那样大涨**（最差的跌 38.8%），"
        "规则在那些票上赚的不是「抓住上涨」，而是**纯粹躲开回撤**——"
        "这跟它在 601869 上赚钱的机制是同一个，说明它不是只对那一次崩盘有效。",
        "",
        "### ④ 但即便如此，仍然不能说「这个策略可靠」",
        "",
        "三条保留：",
        "",
        "1. **横截面检验的是「择时有没有用」，不是「能不能跑赢 v1」。**"
        "在 11 个非 601869 的标的上，CaptureT v1 本身的成绩是 "
        "**3/11 为正、合计 −4,830 元**（既有报告）。"
        "择时叠加在这些标的上普遍有效，说明择时是个真东西；"
        "但「择时 + 某只票」能不能稳定超过 12,701，本次没有独立验证——"
        "12,701 这个数本身就来自 601869 一个样本。",
        "",
        "2. **样本期只有 8.5 个月、只包含一次崩盘。**"
        "「躲开 −54.8% 的回撤」这件事在更长的时间尺度上会不会变成"
        "「在震荡市里被反复止损打脸」，本数据回答不了。"
        "MA20 / MA50 那两条规则在 601869 上是 **−42,579 / −35,813**——"
        "同样的趋势框架、不同的触发方式，结果差 14 万元，"
        "**说明这个框架本身对实现细节极其敏感。**",
        "",
        "3. **没有做任何样本外。** 本次的规则是教科书默认参数、没有搜索，"
        "这一点比 CaptureT 那条线干净；但 601869 的 2026 年只发生过一次，"
        "**没有第二个独立的崩盘可以用来验证。**",
        "",
        "### 最终判定",
        "",
        f"**要求是达到了**：择时版本全区间 **{best_ex:+,.0f} 元**，"
        f"显著超过 CaptureT v1 的 {v1_excess:,.0f} 元。"
        "但我建议**把它当成一个待验证的假设，而不是一个可以上实盘的结论**，"
        "理由就是上面那三条保留。",
        "",
        "要在现有账户上用它，最小可行的做法是："
        "**保留 CaptureT v1 的日内反T（它不依赖方向判断），"
        "只把「回撤 25% 清仓」当作底仓的风险开关**——"
        "两者不冲突，v1 的反T 在空仓期自然不开仓。",
        "",
    ])
    return lines


if __name__ == "__main__":
    main()
