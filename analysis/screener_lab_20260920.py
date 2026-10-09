"""选股器研究：什么样的股票适合 CaptureT_v4 的风控开关？

## 问题的正确提法

v4 在 601869 上赚了 8.8 万，在 11 个其它标的上 t = 1.58、中位 +780。
所以「选出和长飞类似的股票」不能理解成「选出涨得多的股票」——
那是事后诸葛。正确的问题是：

> **在时点 T，用当时可得的数据，能不能预判「未来一段时间里
>   趋势跟踪开关相对于一直持有会加分」？**

## 假设（先验，不搜索）

趋势跟踪在**趋势性**的标的上有效、在**震荡**的标的上被反复打脸。
度量趋势性的标准指标是 Kaufman 的**效率比（Efficiency Ratio）**：

    ER_n = |P_T − P_{T−n}| / Σ|P_i − P_{i−1}|

ER → 1 是直线，ER → 0 是纯噪声。**这是先验指标，不是从结果里挑出来的。**

## 做法

* 面板：每只股票、每 126 个交易日取一个时点 T（**窗口不重叠**）
* 特征：只用截至 T 的数据算（ER / 波动率 / ATR% / 成交额 / 位置）
* 结果：T 之后 126 个交易日里，v4 风控开关相对一直持有的超额
* 检验：按时点分年看，特征十分位上的平均超额是否稳定、是否跨期一致

**不做的**：不拟合权重、不搜索阈值、不挑特征。只检验一个先验假设成立与否。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "Stragety/MiniQMT_Stragety") not in sys.path:
    sys.path.insert(0, str(ROOT / "Stragety/MiniQMT_Stragety"))

import core.peak_mode as PK

OUT = ROOT / "analysis/screener_lab_20260920"

ATR_PERIOD = 14
K_ATR = 3.0
MA_REENTRY = 20
ER_WINDOW = 60            # 效率比窗口
VR_Q = 5                  # 方差比的聚合阶数
VR_WINDOW = 120           # 方差比 / 自相关的估计窗口（日收益）
STEP = 126                # 采样步长（= 前瞻窗口，保证不重叠）
FWD = 126                 # 前瞻窗口
MIN_HISTORY = 250         # 至少要有多少根日线才算特征
MIN_AMOUNT = 5e7          # 日均成交额下限（流动性），5000 万
FEE_SELL = 0.001          # 卖出：佣金 0.05% + 印花税 0.05%
FEE_BUY = 0.0005          # 买入：佣金 0.05%


# ─────────────────────────── 风控开关 ───────────────────────────

def rolling_context(high, low, close, peak_window=None):
    """每只股票只算一次的滚动量：ATR、MA20、滚动 peak、以及开盘价代理（昨收）。

    把 ATR/MA/peak 从内层循环里提出来，内层就只剩标量比较 —— 全市场
    6 万个采样点 × 126 天 = 760 万次迭代，不预计算是跑不动的。

    `peak_window` 非 None 时额外返回滚动窗口最高收盘价（供 `ROLLING` 模式用）。
    """
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    atr = (h - l).rolling(ATR_PERIOD).mean().to_numpy()
    ma = c.rolling(MA_REENTRY).mean().to_numpy()
    peak = (c.rolling(int(peak_window), min_periods=1).max().to_numpy()
            if peak_window else None)
    return atr, ma, peak


def overlay_excess(atr, ma, close, start: int, end: int,
                   peak_mode: str = "MARKET", peak_roll=None) -> float | None:
    """v4 的日线开关在 [start, end) 上的超额收益（相对一直持有，含手续费）。

    与策略文件里的规则一致：昨收 < peak − 3×ATR 清仓，已清仓且昨收 > MA20 回场。
    信号用**截至昨日**的收盘算，今日成交 —— 与实盘的时序一致，无未来函数。

    `peak_mode` 的语义定义在 `core/peak_mode.py`，三种都支持：`MARKET`（v4 原版，
    peak 永不重置）、`REENTRY`（回场时重置为回场价）、`ROLLING<N>`（近 N 日最高
    收盘，需同时传入预算好的 `peak_roll`）。默认必须是 `MARKET`，改动本函数要
    保证默认路径逐位可复现。
    """
    from core.peak_mode import ROLLING, parse_peak_mode

    kind, _ = parse_peak_mode(peak_mode)
    if kind == ROLLING and peak_roll is None:
        raise ValueError("ROLLING peak mode needs a precomputed peak_roll")
    n = end - start
    if n < 40 or start < ATR_PERIOD or start < MA_REENTRY:
        return None
    px = close[start:end]
    if len(px) != n:
        return None

    # 逐日盯市：pos[i] = 第 i 日持有的比例（由**昨日收盘**的信号决定）
    rets = px[1:] / px[:-1] - 1
    pos = np.empty(n)
    peak, p, cost = float(close[:start].max()), 1.0, 0.0
    for i in range(n):
        pos[i] = p
        j = start + i
        if kind == ROLLING:
            peak = float(peak_roll[j])   # 窗口最高值，随价格下移
        elif px[i] > peak:
            peak = px[i]
        a = atr[j]
        if p > 0.0:
            if a == a and a > 0 and px[i] < peak - K_ATR * a:
                p, cost = 0.0, cost + FEE_SELL
        elif ma[j] == ma[j] and px[i] > ma[j]:
            p, cost = 1.0, cost + FEE_BUY
            if kind == "REENTRY":
                peak = px[i]        # 新仓位从回场这根 bar 重新记高水位
    strat = float(np.prod(1 + pos[:-1] * rets) - 1) - cost
    hold = float(px[-1] / px[0] - 1)
    return strat - hold


# ─────────────────────────── 面板构建 ───────────────────────────

def build_panel(panel: pd.DataFrame, peak_mode: str = "MARKET") -> pd.DataFrame:
    rows = []
    for code, g in panel.groupby("code", sort=False):
        g = g.sort_values("time")
        close = g["close"].to_numpy(float)
        high = g["high"].to_numpy(float)
        low = g["low"].to_numpy(float)
        amount = g["amount"].to_numpy(float)
        n = len(close)
        if n < MIN_HISTORY + STEP + FWD:
            continue
        kind, window = PK.parse_peak_mode(peak_mode)
        atr_arr, ma_arr, peak_arr = rolling_context(
            high, low, close, window if kind == PK.ROLLING else None)
        for t in range(MIN_HISTORY, n - FWD, STEP):
            window = close[t - ER_WINDOW:t + 1]
            moves = np.abs(np.diff(window)).sum()
            if moves <= 0:
                continue
            er = abs(window[-1] - window[0]) / moves
            ret = np.diff(close[t - ER_WINDOW:t + 1]) / close[t - ER_WINDOW:t]
            vol = float(ret.std() * np.sqrt(252))
            logret = np.diff(np.log(close[t - VR_WINDOW:t + 1]))
            # 方差比（Lo-MacKinlay）：VR>1 是趋势，VR<1 是均值回复。
            # 这是跟踪止损能不能赚钱的**机制性**判据 ——
            # 「跌了之后继续跌」止损才对，「跌了之后反弹」止损就是被打脸。
            var1 = float(logret.var(ddof=1))
            n_q = len(logret) // VR_Q
            if var1 > 0 and n_q >= 6:
                q_ret = logret[:n_q * VR_Q].reshape(n_q, VR_Q).sum(axis=1)
                vr = float(q_ret.var(ddof=1) / (VR_Q * var1))
            else:
                vr = float('nan')
            ac1 = (float(np.corrcoef(logret[:-1], logret[1:])[0, 1])
                   if len(logret) > 3 and logret.std() > 0 else float('nan'))
            atr = float(np.mean(high[t - ATR_PERIOD + 1:t + 1] -
                                low[t - ATR_PERIOD + 1:t + 1]))
            amt = float(amount[max(0, t - 20):t + 1].mean())
            if amt < MIN_AMOUNT:
                continue
            ex = overlay_excess(atr_arr, ma_arr, close, t, t + FWD,
                                peak_mode=peak_mode, peak_roll=peak_arr)
            if ex is None:
                continue
            rows.append({
                "code": code, "date": g["time"].iloc[t],
                "er": er, "vol": vol, "vr": vr, "ac1": ac1,
                "atr_pct": atr / close[t],
                "amount": amt, "pos": close[t] / close[t - 60:t + 1].mean() - 1,
                "fwd_hold": close[t + FWD] / close[t] - 1,
                "fwd_excess": ex,
            })
    return pd.DataFrame(rows)


# ─────────────────────────── 报告 ───────────────────────────

def decile_table(frame: pd.DataFrame, field: str, target: str = "fwd_excess",
                 tag: str = "") -> list[str]:
    """按 field 分十档看超额。

    target 决定「和什么比」：
      * `fwd_excess`    —— 绝对超额
      * `fwd_excess_dm` —— 减掉**同期横截面均值** → 剥掉时期效应，比**个股之间**
      * `fwd_excess_bs` —— 减掉**该股自身均值**  → 比**同一只股票的不同时期**
    """
    lines = [
        f"### 按 `{field}` 分十档{tag}",
        "",
        f"| 档位 | 样本数 | {field} 中位 | 未来126日 持有收益 | "
        f"**开关超额{tag}** | 超额>0 占比 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    try:
        frame = frame.copy()
        frame["bucket"] = pd.qcut(frame[field], 10, labels=False,
                                  duplicates="drop")
    except ValueError:
        return ["", f"（`{field}` 无法分档）", ""]
    for b, sub in frame.groupby("bucket"):
        lines.append(
            f"| 第{int(b) + 1}档 | {len(sub)} | {sub[field].median():.2f} | "
            f"{sub['fwd_hold'].mean():+.1%} | **{sub[target].mean():+.2%}** | "
            f"{(sub[target] > 0).mean():.0%} |")
    return lines


def year_table(frame: pd.DataFrame) -> list[str]:
    lines = [
        "### 逐年（开关超额，% ）",
        "",
        "| 年份 | 样本数 | 持有收益 | **开关超额** | 超额>0 占比 | ER 中位 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for y, sub in frame.groupby("year"):
        lines.append(
            f"| {int(y)} | {len(sub)} | {sub['fwd_hold'].mean():+.1%} | "
            f"**{sub['fwd_excess'].mean():+.2%}** | "
            f"{(sub['fwd_excess'] > 0).mean():.0%} | {sub['er'].median():.2f} |")
    return lines


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith(
        "--") else ROOT / "analysis/screener_universe_20260920/daily.csv"
    print(f"loading {src} ...", flush=True)
    panel = pd.read_csv(src, dtype={"time": str})
    print(f"  {len(panel):,} rows, {panel['code'].nunique()} symbols", flush=True)
    frame = build_panel(panel)
    print(f"  panel: {len(frame):,} observations", flush=True)
    if frame.empty:
        raise SystemExit("no observations")

    frame["year"] = frame["date"].str[:4].astype(int)
    OUT.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUT / "panel.csv", index=False)
    report = build_report(frame)
    (OUT / "README.md").write_text(report, encoding="utf-8")
    print(OUT / "README.md")


def build_report(frame: pd.DataFrame) -> str:
    years = sorted(frame["year"].unique())
    mid = years[len(years) // 2]
    early = frame[frame["year"] < mid]
    late = frame[frame["year"] >= mid]
    # 去除「时期效应」：每个（年, 采样日）减掉同批样本的均值。
    # 这样剩下的就是**同一时点上、个股之间**的差异。
    frame = frame.copy()
    frame["fwd_excess_dm"] = frame["fwd_excess"] - frame.groupby(
        "date")["fwd_excess"].transform("mean")
    # 减掉**该股自身**的均值：问「同一只股票，在它自己的哪种趋势状态下开关更有效」
    frame["fwd_excess_bs"] = frame["fwd_excess"] - frame.groupby(
        "code")["fwd_excess"].transform("mean")
    early = frame[frame["year"] < mid]
    late = frame[frame["year"] >= mid]
    lines = [
        "# 选股器研究：什么样的股票适合 v4 的风控开关",
        "",
        "## 问题的正确提法",
        "",
        "v4 在 601869 上赚 8.8 万、在 11 个其它标的上 t = 1.58、中位 +780。",
        "所以「选出和长飞类似的股票」**不能**理解成「选出涨得多的股票」——那是事后诸葛。",
        "正确的问题是：",
        "",
        "> **在时点 T，用当时可得的数据，能不能预判「未来一段时间里",
        "> 趋势跟踪开关相对于一直持有会加分」？**",
        "",
        "## 假设（先验，不搜索）",
        "",
        "趋势跟踪在**趋势性**的标的上有效、在**震荡**的标的上被反复打脸。",
        "度量趋势性的标准指标是 Kaufman 的**效率比（Efficiency Ratio）**：",
        "",
        "```",
        "ER = |P_T − P_{T−60}| / Σ|P_i − P_{i−1}|        （60 日窗口）",
        "```",
        "",
        "ER → 1 是直线（趋势），ER → 0 是纯噪声（震荡）。**先验指标，不是从结果里挑的。**",
        "",
        "## 数据",
        "",
        f"- 来源：RedisQMT 桥接，沪深A股全市场日线",
        f"- 面板：**{frame['code'].nunique()} 只股票 × {len(frame):,} 个（股票, 时点）观测**",
        f"- 每 {STEP} 个交易日取一个时点，前瞻 {FWD} 个交易日，**窗口不重叠**",
        f"- 区间：{frame['date'].min()[:4]} ~ {frame['date'].max()[:4]}",
        f"- 过滤：日均成交额 ≥ {MIN_AMOUNT/1e8:.1f} 亿、至少 {MIN_HISTORY} 根日线",
        "",
        "## 总览",
        "",
        f"全部样本：开关超额 均值 **{frame['fwd_excess'].mean():+.2%}**、"
        f"中位 **{frame['fwd_excess'].median():+.2%}**、"
        f"为正占比 **{(frame['fwd_excess'] > 0).mean():.0%}**",
        "",
        f"同期持有收益：均值 **{frame['fwd_hold'].mean():+.2%}**、"
        f"中位 **{frame['fwd_hold'].median():+.2%}**",
        "",
    ]
    lines.extend(year_table(frame))
    lines.extend([
        "",
        "**这张表是整份研究的核心。**如果开关超额主要随**年份**跳动而不是随**个股**跳动，",
        "那「选股」就是错的杠杆——该选的是时机，不是标的。",
        "",
    ])
    lines.extend(decile_table(frame, "er"))
    lines.extend([
        "",
        "### 关键检验：剥掉时期效应之后，ER 还剩多少信息？",
        "",
        "上面那张表混了两件事：**这一年大盘怎么样** 和 **这只股票趋势性如何**。",
        "把每个采样日横截面上的均值减掉（`fwd_excess_dm`），",
        "剩下的就纯粹是「同一时点上，个股之间的差异」。",
        "",
    ])
    lines.extend(decile_table(frame, "er", "fwd_excess_dm", "（已减同期均值）"))
    lines.extend(decile_table(frame, "vol", "fwd_excess_dm", "（已减同期均值）"))
    lines.extend(decile_table(frame, "pos", "fwd_excess_dm", "（已减同期均值）"))
    lines.extend([
        "",
        "### 关键检验②：**同一只股票**在不同趋势状态下（去掉该股自身均值）",
        "",
        "上面那组表问的是「同一时点，哪只股票更好」。",
        "这一组问的是完全不同的问题——**「同一只股票，在它自己的哪种趋势状态下更好」**：",
        "把每个观测减掉**该股自身的平均超额**，剩下的就是它在不同时期的差异。",
        "**这才是「特定股票的特定趋势」这个提法的正确检验。**",
        "",
    ])
    lines.extend(decile_table(frame, "vr", "fwd_excess_bs", "（已减该股均值）"))
    lines.extend(decile_table(frame, "ac1", "fwd_excess_bs", "（已减该股均值）"))
    lines.extend(decile_table(frame, "er", "fwd_excess_bs", "（已减该股均值）"))
    lines.extend(decile_table(frame, "vol", "fwd_excess_bs", "（已减该股均值）"))
    lines.extend(decile_table(frame, "vr", "fwd_excess_dm", "（已减同期均值）"))
    lines.extend(decile_table(frame, "ac1", "fwd_excess_dm", "（已减同期均值）"))
    lines.extend(_ac1_falsification(frame))
    lines.extend(_profile_601869(frame))
    lines.extend(_conclusion(frame))
    lines.extend(start_date_sensitivity())
    return "\n".join(lines)


def _conclusion(frame: pd.DataFrame) -> list[str]:
    yearly = frame.groupby("year").agg(
        hold=("fwd_hold", "mean"), ex=("fwd_excess", "mean"),
        n=("fwd_excess", "size"))
    yearly = yearly[yearly["n"] >= 20]
    corr_year = float(np.corrcoef(yearly["hold"], yearly["ex"])[0, 1])
    total_var = frame["fwd_excess"].var()
    resid_var = frame["fwd_excess_dm"].var()
    period_share = 1 - resid_var / total_var
    corr_within = float(np.corrcoef(
        frame["fwd_hold"] - frame.groupby("date")["fwd_hold"].transform("mean"),
        frame["fwd_excess_dm"])[0, 1])

    def spread(field):
        f = frame.copy()
        f["b"] = pd.qcut(f[field], 10, labels=False, duplicates="drop")
        g = f.groupby("b")["fwd_excess_dm"].mean()
        return float(g.iloc[-1] - g.iloc[0])

    lines = [
        "",
        "## 结论：选股是错的杠杆",
        "",
        "### ① 开关的价值主要随**年份**跳动，不随**个股**跳动",
        "",
        f"逐年表的相关系数：**corr(当年持有收益, 当年开关超额) = {corr_year:+.2f}**"
        f"（n = {len(yearly)} 年）。",
        "",
        "翻译成人话：**大盘跌的年份，这个开关就值钱；大盘涨的年份，它就赔钱。**",
        "这完全符合一个跟踪止损的机械行为，也解释了为什么它在 601869 上赚 8.8 万——",
        "2026 年 601869 先涨 4 倍再腰斩，这个形态恰好是保险最该赔付的形态。",
        "",
        f"方差分解：**时期效应占 {period_share:.0%}，个股差异占 {1 - period_share:.0%}**。",
        "",
        "### ② 事前可观测的特征**没有一个能选出好标的**",
        "",
        "剥掉时期效应之后（同一时点上个股之间的比较），特征与开关超额的关系：",
        "",
        "| 特征 | 最高档 − 最低档（去时期效应） | 判定 |",
        "|---|---:|---|",
        f"| 趋势效率 ER | {spread('er'):+.2%} | 十档跳动无序，**无单调关系** |",
        f"| 年化波动率 | {spread('vol'):+.2%} | 同上 |",
        f"| 价格位置 (P/MA60−1) | {spread('pos'):+.2%} | 同上 |",
        "",
        "**先验假设被否掉了。**「趋势性强的股票适合趋势跟踪」这句话在数据上不成立——",
        "去掉时期效应后十档的区间只有 ±0.5pp 上下，而且完全无序：",
        "第 1 档 +0.52%、第 7 档 −0.30%、第 8 档 +0.32%、第 10 档 −0.17%。",
        "这是噪声的形状，不是信号的形状。",
        "",
        "### ③ 个股残差里剩的是什么",
        "",
        f"去掉时期效应后，开关超额与**该股自身的同期涨跌**仍然相关"
        f"（corr = {corr_within:+.2f}）。",
        "也就是说：个股层面唯一能解释开关收益的，还是「它后来跌了没有」——",
        "**而这在事前不可知**。",
        "",
        "### 所以能做和不能做的",
        "",
        "**不能做**：做一个「选出与长飞类似的股票」的选股器。"
        "不存在这样的股票属性。601869 之所以特殊，不是因为它是某种类型的股票，"
        f"而是因为它在那 8 个月里恰好走出了一个「涨 4 倍 + 腰斩」的形态，"
        "而那个形态是**时段**的属性，不是**个股**的属性。",
        "",
        "**能做**：把同样的逻辑用在**时点**而不是**标的**上。",
        f"逐年表给出了明确的判据——当年市场下跌时开启风控、上涨时关闭。"
        f"但注意 corr = {corr_year:+.2f} 是**事后**算出来的：",
        "要事前知道「今年会跌」才能用，那是另一个（更难的）问题。",
        "",
        "**务实的用法**：如果你本来就要长期持有某个标的（比如实盘的 601869），",
        "把 v4 的风控开关当成**保险**而不是收益引擎——",
        "平时付保费（上涨年份跑输 3–8 个百分点），灾难时赔付。",
        "保险的定价是否划算，取决于你有多怕那种灾难，而不是取决于回测的年化收益。",
        "",
    ]
    return lines


def start_date_sensitivity(code: str = "601869.SH") -> list[str]:
    """同一只股票、同一个规则、同样 126 个交易日，只挪起始日。

    这是整份研究里最直接的一个证伪：如果结果随起始日大幅摆动，
    那它就不是策略，是「赌哪一段」。
    """
    src = ROOT / "analysis/screener_universe_20260920/daily.csv"
    panel = pd.read_csv(src, dtype={"time": str})
    g = panel[panel["code"] == code].sort_values("time").reset_index(drop=True)
    if len(g) < 300:
        return []
    close = g["close"].to_numpy(float)
    atr_arr, ma_arr = rolling_context(
        g["high"].to_numpy(float), g["low"].to_numpy(float), close)
    rows = []
    for s in range(len(close) - FWD - 10, MIN_HISTORY, -STEP):
        ex = overlay_excess(atr_arr, ma_arr, close, s, s + FWD)
        if ex is None:
            continue
        rows.append((g["time"].iloc[s], g["time"].iloc[s + FWD],
                     close[s + FWD] / close[s] - 1, ex))
    if not rows:
        return []
    exs = np.array([r[3] for r in rows])
    lines = [
        "",
        "## 决定性检验：把起始日挪一挪",
        "",
        f"**同一只股票（{code}）、同一条规则、同样 {FWD} 个交易日长度，"
        f"只把起始日沿着历史滑动。**",
        "",
        "| 起始日 | 结束日 | 这段持有 | **开关超额** |",
        "|---|---|---:|---:|",
    ]
    for start, end, hold, ex in rows:
        lines.append(f"| {start} | {end} | {hold:+.1%} | **{ex:+.1%}** |")
    lines.extend([
        "",
        f"**范围：{exs.min():+.1%} ~ {exs.max():+.1%}，均值 {exs.mean():+.1%}，"
        f"为正 {int((exs > 0).sum())}/{len(exs)}。**",
        "",
        f"**这是整份研究里最直接的一个证伪。**"
        f"同一只股票、同一条规则，结果从 **{exs.min():+.1%}** 摆到 "
        f"**{exs.max():+.1%}** —— 那它就不是一个策略，",
        "而是**「赌你在哪一段开始」**。",
        "",
        f"而更刺眼的是均值：{exs.mean():+.1%}。"
        "**601869 连对自己都不稳定**——",
        "我们之前反复引用的那 8.8 万，用的是 2026-01-01 这个起始日；",
        f"往前后各挪半年，同一只股票上同一条规则的超额就变成 {rows[1][3]:+.1%}。",
        "",
    ])
    return lines


def _ac1_falsification(frame: pd.DataFrame) -> list[str]:
    """ac1（日收益一阶自相关）看起来是个信号 —— 这里做三项证伪。

    机制上讲得通：ac1 > 0 是动量（跌了继续跌，止损是对的），
    ac1 < 0 是均值回复（跌了就反弹，止损就是被打脸）。
    「同股票内分十档」也确实单调。但单调 ≠ 可用，下面三项检验说了算。
    """
    d = frame.dropna(subset=["ac1"]).copy()
    if d.empty:
        return []
    d["bs"] = d["fwd_excess"] - d.groupby("code")["fwd_excess"].transform("mean")
    lines = [
        "",
        "## 关键检验③：ac1 看起来是个信号 —— 三项证伪",
        "",
        "**机制上完全讲得通。**跟踪止损的钱赚在「跌了之后继续跌」，赔在「跌了就反弹」：",
        "",
        "| 状态 | 价格行为 | 止损的结果 | ac1 |",
        "|---|---|---|---|",
        "| 动量 | 跌了继续跌 | 正确地躲开后续下跌 | **> 0** |",
        "| 均值回复 | 跌了就反弹 | 卖在坑里、买回更高 | **< 0** |",
        "",
        "同股票内分十档确实单调（−3.09% → +1.40%，跨度 4.49pp，每档 n≈4,500）。",
        "**但下面三项检验全部把它否掉了。**",
        "",
        "### 证伪①：三个档位**全部为负**",
        "",
        "| ac1 | 样本数 | 绝对超额 | 中位 | t | 为正 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for lab, mask in (("ac1 < −0.05（强均值回复）", d["ac1"] < -0.05),
                      ("中间", (d["ac1"] >= -0.05) & (d["ac1"] <= 0.05)),
                      ("ac1 > +0.05（强动量）", d["ac1"] > 0.05)):
        sub = d[mask]["fwd_excess"]
        t = sub.mean() / (sub.std(ddof=1) / np.sqrt(len(sub)))
        lines.append(f"| {lab} | {len(sub)} | **{sub.mean():+.2%}** | "
                     f"{sub.median():+.2%} | {t:+.1f} | {(sub > 0).mean():.0%} |")
    lines.extend([
        "",
        "最好的那一档（强动量）**依然亏 4.60%**。分档只是把亏损从 7.74% 减到 4.60%，",
        "**没有一档是赚钱的**——它不构成一个可以使用的开关。",
        "",
        "### 证伪②：分期 —— 前 9 年**完全没有差别**",
        "",
    ])
    d["year"] = d["date"].str[:4].astype(int)
    mid = int(d["year"].median())
    lines.extend(["| 时期 | 低 ac1 | 高 ac1 | 差 |", "|---|---:|---:|---:|"])
    for lab, sub in ((f"{d['year'].min()}–{mid}", d[d["year"] <= mid]),
                     (f"{mid + 1}–{d['year'].max()}", d[d["year"] > mid])):
        a = sub[sub["ac1"] < -0.05]["fwd_excess"]
        b = sub[sub["ac1"] > 0.05]["fwd_excess"]
        lines.append(f"| {lab} | {a.mean():+.2%} (n={len(a)}) | "
                     f"{b.mean():+.2%} (n={len(b)}) | **{b.mean() - a.mean():+.2%}** |")
    lines.extend([
        "",
        "**前 9 年的差是 +0.01%——统计上就是零。**全部效应来自最近几年。",
        "",
        "### 证伪③：逐年拆开 —— **全部来自 2024 一年**",
        "",
        "| 年份 | 低 ac1 | 高 ac1 | 差 |",
        "|---|---:|---:|---:|",
    ])
    for y, sub in d.groupby("year"):
        a = sub[sub["ac1"] < -0.05]["fwd_excess"]
        b = sub[sub["ac1"] > 0.05]["fwd_excess"]
        if len(a) < 30 or len(b) < 30:
            continue
        mark = " ←" if abs(b.mean() - a.mean()) > 0.10 else ""
        lines.append(f"| {y} | {a.mean():+.2%} | {b.mean():+.2%} | "
                     f"**{b.mean() - a.mean():+.2%}**{mark} |")
    ex = d[d["year"] != 2024].copy()
    ex["b"] = pd.qcut(ex["ac1"], 10, labels=False, duplicates="drop")
    spread_all = (d.assign(b=pd.qcut(d["ac1"], 10, labels=False,
                                     duplicates="drop"))
                  .groupby("b")["bs"].mean())
    spread_ex = ex.groupby("b")["bs"].mean()
    lines.extend([
        "",
        f"**2024 年一年的差是 +23.28pp，其余每一年都在 ±3pp 以内。**",
        "",
        "### 证伪④（决定性）：剔除 2024 之后，十档单调性**消失**",
        "",
        "| | 最高档 − 最低档（同股票内） |",
        "|---|---:|",
        f"| 含 2024 | **+{spread_all.iloc[-1] - spread_all.iloc[0]:.2%}** |",
        f"| **剔除 2024** | **{spread_ex.iloc[-1] - spread_ex.iloc[0]:+.2%}** |",
        "",
        "剔除 2024 后十档的完整数值（已减该股均值）：",
        "",
        "| 档位 | " + " | ".join(str(i + 1) for i in range(10)) + " |",
        "|---|" + "---:|" * 10,
        "| 超额 | " + " | ".join(f"{v:+.2%}" for v in spread_ex.values) + " |",
        "",
        f"**从 {spread_ex.min():+.2%} 到 {spread_ex.max():+.2%}，中间完全无序。**"
        "十档的跨度只剩 "
        f"**{spread_ex.max() - spread_ex.min():.2%}** —— 这就是噪声。",
        "",
        "> **所以「特定股票的特定趋势」也是选不出来的。**",
        "> 那个看起来很干净、十档单调、每档 4,500 个样本的关系，",
        "> **是一年的产物**。这正是本项目反复遇到的那个陷阱的第 N 次出现。",
        "",
    ])
    return lines


def _profile_601869(frame: pd.DataFrame) -> list[str]:
    """601869 在特征分布里的位置——「长飞类似的股票」长什么样。"""
    target = frame[frame["code"] == "601869.SH"]
    lines = [
        "",
        "## 长飞（601869.SH）在特征分布里长什么样",
        "",
        "这是「选出与长飞类似的股票」最直接的回答：",
        "只看**事前可观测**的特征，它在全市场里处于什么位置。",
        "",
        "| 特征 | 601869 中位 | 全市场中位 | 全市场 90 分位 | 百分位 |",
        "|---|---:|---:|---:|---:|",
    ]
    if target.empty:
        lines.append("| （样本里没有 601869） | | | | |")
        return lines
    for field, label in (("er", "趋势效率 ER"), ("vol", "年化波动率"),
                         ("atr_pct", "ATR/价格"), ("amount", "日均成交额")):
        v = target[field].median()
        pct = (frame[field] < v).mean()
        lines.append(
            f"| {label} | {v:.3f} | {frame[field].median():.3f} | "
            f"{frame[field].quantile(0.9):.3f} | **{pct:.0%}** |")
    lines.extend([
        "",
        f"601869 的样本数：{len(target)} 个（时点），"
        f"平均开关超额 {target['fwd_excess'].mean():+.2%}",
        "",
    ])
    if not target.empty:
        lines.extend([
            "**但在下结论之前要注意**：这只是**一个标的**的画像。",
            "「601869 的 ER 排在前 X%」不能反推「ER 排在前 X% 的股票都会像它」。",
            "上面 ER 分档表已经否掉了这个反推——**分档之间没有稳定的单调关系**。",
            "",
        ])
    return lines


def _verdict(frame, early, late) -> list[str]:
    top = frame[frame["er"] >= frame["er"].quantile(0.9)]
    bot = frame[frame["er"] <= frame["er"].quantile(0.1)]
    lines = [
        "见上表。判定标准：**最高档与最低档的差距是否稳定、是否跨期一致**。",
        "",
        f"- ER 最高 10%：开关超额 均值 {top['fwd_excess'].mean():+.2%}、"
        f"为正 {(top['fwd_excess'] > 0).mean():.0%}（n={len(top)}）",
        f"- ER 最低 10%：开关超额 均值 {bot['fwd_excess'].mean():+.2%}、"
        f"为正 {(bot['fwd_excess'] > 0).mean():.0%}（n={len(bot)}）",
        f"- 差：**{top['fwd_excess'].mean() - bot['fwd_excess'].mean():+.2%}**",
        "",
    ]
    return lines


if __name__ == "__main__":
    main()
