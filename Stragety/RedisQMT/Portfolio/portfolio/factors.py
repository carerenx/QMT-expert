"""预注册因子库 —— 全部先验驱动，符号在看结果之前写死。

## 范围声明（必须写在报告最前面）

数据只有 OHLCV + 成交额。**测不了价值 / 质量 / 盈利因子**，
而 A 股已发表证据最强的恰恰是价值（Liu-Stambaugh-Yuan 2019 的 CH-3
里 EP 是最强因子）与质量。所以本批只能找到「价量类」alpha。

**如果价量因子在样本外全灭，诚实的下一步是补基本面，不是继续在
OHLCV 里挖。** 这句话是给自己设的边界。

## 与仓库既有证伪的关系

`analysis/选股器研究结论.md` 已经否掉了 ER / 年化波动率 / 价格位置 /
方差比 VR / 日收益一阶自相关 ac1 —— 那些是「什么样的股票适合趋势跟踪」，
问题提法不同。**本库不重复测那五个**，测的是横截面定价因子。

## 符号是预注册的，不允许事后翻转

闸门 A 会检查 `sign(mean IC) == 预注册符号`。反了判 `无效`，
**不允许翻符号重报** —— 这一条专堵「反向使用」这个动作。

## 已剔除的因子

**F9 换手率**（Miller 1977 异质信念）—— 桥接拿不到历史流通股本，
`get_divid_factors` 也返回空，所以换手率算不出来。已从预注册清单剔除，
并在报告里写明原因。**这是数据源缺口导致的剔除，不是结果驱动的。**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Factor:
    key: str
    prior: str               # "+" / "-"，**底层特征**的方向（文档用）
    description: str
    mechanism: str
    fn: Callable[[pd.DataFrame, "pd.Series | None"], pd.Series]

    @property
    def expected_ic_sign(self) -> int:
        """预期 IC 的符号 —— **恒为 +1**。

        每个 `fn` 都已经把方向内置了：F2 返回 `-(过去20日收益)`、
        F6 返回 `-max5`、F11 返回 `-beta`。所以「因子按预注册方向有效」
        一律表现为**正的截面 IC**（高 alpha → 高未来收益）。

        `prior` 描述的是**底层特征**的方向（「过去收益对未来收益是负向的」），
        不是构造出来的 alpha 的符号。把 `prior` 直接拿去比对 IC 符号
        是错的 —— 那等于要求反转因子产生负 IC，所有反转类因子都会被误判。
        """
        return 1


# F6 MAX 效应的窗口与取最值个数
WINDOW = 20
TOP_K = 5


# ─────────────────────────── 工具 ───────────────────────────

def _by_code(panel: pd.DataFrame, column: str) -> pd.Series:
    """取 `adj_close` 之类，按 code 分组、按 date 排序后的 Series。"""
    frame = panel[["code", "date", column]].sort_values(["code", "date"])
    return frame.set_index(["code", "date"])[column]


def _grouped(value: pd.Series, func: str, window: int,
             **kwargs) -> pd.Series:
    """按 code 分组做滚动运算。"""
    rolled = value.groupby(level="code").transform(
        lambda s: getattr(s, func)(window, **kwargs))
    return rolled


def _market_return(panel: pd.DataFrame) -> pd.Series:
    """等权全市场日收益 —— 作为市场组合的代理。

    用横截面均值而不是拉指数，好处是**自包含**：不引入第二个数据源，
    也就不会因为指数数据缺失或口径不同引入不一致。
    代价是它更接近等权小盘市场，不是市值加权 —— 对 beta/特质波动率
    这类相对量，这个差别不影响排序。
    """
    frame = panel[["date", "total_ret"]].copy()
    return frame.groupby("date")["total_ret"].mean()


def _residual_stats(panel: pd.DataFrame, market: pd.Series,
                    window: int = 60) -> tuple[pd.Series, pd.Series]:
    """按股票滚动回归市场收益，返回 (beta, 残差波动率)。

    逐股跑滚动 OLS 代价太高，用协方差的等价形式：

        beta   = Cov(r_i, r_m) / Var(r_m)
        resid  = Var(r_i) − beta² × Var(r_m)
        ivol   = sqrt(resid)

    这与逐窗回归的**系数**等价，只有截距项被略去 —— 对横截面排序无影响。
    """
    frame = panel[["code", "date", "total_ret"]].sort_values(["code", "date"])
    frame = frame.merge(market.rename("mkt"), left_on="date", right_index=True,
                        how="left")
    frame["ri_rm"] = frame["total_ret"] * frame["mkt"]

    grouped = frame.groupby("code", sort=False)

    def roll(column):
        return (grouped[column]
                .transform(lambda s: s.rolling(window, min_periods=window // 2).mean()))

    mean_i = roll("total_ret")
    mean_m = roll("mkt")
    mean_im = roll("ri_rm")

    var_m = (grouped["mkt"].transform(
        lambda s: s.rolling(window, min_periods=window // 2).var()))
    var_i = (grouped["total_ret"].transform(
        lambda s: s.rolling(window, min_periods=window // 2).var()))

    covariance = mean_im - mean_i * mean_m
    beta = covariance / var_m.replace(0.0, np.nan)
    resid_var = (var_i - beta ** 2 * var_m).clip(lower=0.0)
    ivol = np.sqrt(resid_var) * np.sqrt(252.0)

    frame["beta"] = beta.to_numpy()
    frame["ivol"] = ivol.to_numpy()
    return frame.set_index(["code", "date"])["beta"], \
        frame.set_index(["code", "date"])["ivol"]


def _to_alpha(series: pd.Series) -> pd.DataFrame:
    """把 (code, date) 索引的 Series 变成引擎要的长表。"""
    frame = series.rename("alpha").reset_index()
    return frame.dropna(subset=["alpha"])


# ─────────────────────────── 因子实现 ───────────────────────────

def f_mom_12_1(panel, market=None) -> pd.Series:
    """F1 12-1 动量：`adj_close[t-20] / adj_close[t-250] - 1`。

    跳过最近 20 日（Jegadeesh-Titman 的 standard 做法），避开短期反转。
    先验为正，但**A 股证据很弱**（Liu-Stambaugh-Yuan 2019 把动量从
    CH-3 里剔除了）。测它是为了拿一个明确的负结果。
    """
    close = _by_code(panel, "adj_close")
    return close.groupby(level="code").transform(
        lambda s: s.shift(20) / s.shift(250) - 1.0)


def f_reversal_1m(panel, market=None) -> pd.Series:
    """F2 1 月短期反转：`-(adj_close[t] / adj_close[t-20] - 1)`。**头号候选。**

    散户主导使 A 股短期反转异常强（Jegadeesh 1990 / Lehmann 1990 的机制
    在 A 股被放大）。取负号意味着**过去跌的股票得高分**。
    """
    close = _by_code(panel, "adj_close")
    return -close.groupby(level="code").transform(lambda s: s / s.shift(20) - 1.0)


def f_reversal_1w(panel, market=None) -> pd.Series:
    """F3 1 周反转：`-(adj_close[t] / adj_close[t-5] - 1)`。"""
    close = _by_code(panel, "adj_close")
    return -close.groupby(level="code").transform(lambda s: s / s.shift(5) - 1.0)


def f_tsmom(panel, market=None) -> pd.Series:
    """F4 截面 TSMOM：`sign(adj_close[t] / adj_close[t-120] - 1)`。

    注意与仓库既有研究的分工：那里测的是**择时规则**（单标的开关），
    这里测的是**截面分数**（同一时点哪只股票更高分）。两个是不同的假设。
    """
    close = _by_code(panel, "adj_close")
    raw = close.groupby(level="code").transform(lambda s: s / s.shift(120) - 1.0)
    return np.sign(raw)


def f_high_52w(panel, market=None) -> pd.Series:
    """F5 52 周高点接近度：`adj_close / max(adj_high, 250日)`。

    George-Hwang 2004 的锚定效应：离前高越近，突破的阻力越小。
    """
    close = _by_code(panel, "adj_close")
    high = _by_code(panel, "adj_high")
    peak = high.groupby(level="code").transform(
        lambda s: s.rolling(250, min_periods=120).max())
    return close / peak.replace(0.0, np.nan)


def f_max_effect(panel, market=None) -> pd.Series:
    """F6 MAX：过去 20 日**最大 5 个日收益的均值**，取负。

    Bali-Cakici-Whitelaw 2011 的彩票偏好：散户偏好彩票型股票，
    推高价格、压低未来收益。A 股散户占比高，这个效应的先验较强。

    **必须向量化。** 直觉写法 `rolling(20).apply(lambda w: sort(w)[-5:].mean())`
    是逐窗调用一次 Python 函数 —— 全市场约 1000 万行就是 1000 万次调用，
    要跑几小时。用 `sliding_window_view` 把「每个窗口排序取前 5」变成
    整组一次的矩阵运算。
    """
    frame = (panel[["code", "date", "total_ret"]]
             .sort_values(["code", "date"])
             .set_index(["code", "date"]))
    values = frame["total_ret"].to_numpy(float)
    codes = frame.index.get_level_values("code").to_numpy()
    out = np.full(len(values), np.nan)

    # 按股票切边界（数据已按 code 排序，所以边界是连续的）
    starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
    stops = np.r_[starts[1:], len(codes)]

    for start, stop in zip(starts, stops):
        segment = values[start:stop]
        if len(segment) < WINDOW:
            continue
        windows = np.lib.stride_tricks.sliding_window_view(segment, WINDOW)
        valid = np.isfinite(windows).sum(axis=1) >= WINDOW - 2
        ordered = np.sort(np.where(np.isfinite(windows), windows, -np.inf),
                          axis=1)
        top_mean = ordered[:, -TOP_K:].mean(axis=1)
        # 窗口 i 覆盖 [i, i+WINDOW)，落在第 i+WINDOW-1 行
        out[start + WINDOW - 1:stop] = np.where(valid, top_mean, np.nan)

    return -pd.Series(out, index=frame.index)


def f_ivol(panel, market=None) -> pd.Series:
    """F7 特质波动率，取负。Ang et al. 2006 的 IVOL 异象。"""
    if market is None:
        market = _market_return(panel)
    _, ivol = _residual_stats(panel, market)
    return -ivol


def f_amihud(panel, market=None) -> pd.Series:
    """F8 Amihud 非流动性：`mean(|ret| / 成交额)`，60 日。

    Amihud 2002。**这是规模的代理**，必须做规模中性化后再看 ——
    否则测出来的是小市值效应而不是流动性效应。中性化由
    `transforms.py` 统一处理。
    """
    frame = panel[["code", "date", "total_ret", "amount"]].sort_values(
        ["code", "date"]).copy()
    frame["illiq"] = frame["total_ret"].abs() / frame["amount"].replace(0.0, np.nan)
    frame = frame.set_index(["code", "date"])
    return frame.groupby(level="code")["illiq"].transform(
        lambda s: s.rolling(60, min_periods=30).mean())


def f_overnight_intraday(panel, market=None) -> pd.Series:
    """F10 隔夜/日内分解：`Σ20(隔夜收益) − Σ20(日内收益)`。

    Lou-Polk-Skouras 2019 的「拔河」。隔夜部分常被解读为对信息的
    反应、日内部分常被解读为流动性的代价，两者对未来的预测方向相反。

    **这个因子与「T+1 开盘成交」的可实施性直接相关** —— 它决定了
    我们究竟能不能吃到这部分 alpha。若某个因子的超额主要来自
    `T → T+1` 的跳空，闸门 E 会把它判死。
    """
    frame = panel[["code", "date", "open", "close", "preclose"]].sort_values(
        ["code", "date"]).copy()
    frame["overnight"] = frame["open"] / frame["preclose"].replace(0.0, np.nan) - 1.0
    frame["intraday"] = frame["close"] / frame["open"].replace(0.0, np.nan) - 1.0
    frame = frame.set_index(["code", "date"])
    on = frame.groupby(level="code")["overnight"].transform(
        lambda s: s.rolling(20, min_periods=15).sum())
    idy = frame.groupby(level="code")["intraday"].transform(
        lambda s: s.rolling(20, min_periods=15).sum())
    return on - idy


def f_low_beta(panel, market=None) -> pd.Series:
    """F11 低 beta，取负。Frazzini-Pedersen 2014 的 BAB。"""
    if market is None:
        market = _market_return(panel)
    beta, _ = _residual_stats(panel, market)
    return -beta


def f_nominal_price(panel, market=None) -> pd.Series:
    """F12 名义价格：`close` 的倒数。

    Bali-Brown-Murray-Tang 2014 的名义价格幻觉。**成本敏感，只作诊断** ——
    低价股的 tick 占比大、滑点高，回测里很难真实成交。
    """
    close = _by_code(panel, "close")
    return 1.0 / close.replace(0.0, np.nan)


# ─────────────────────────── 注册表 ───────────────────────────

FACTORS: dict[str, Factor] = {
    "F1_mom_12_1": Factor(
        "F1_mom_12_1", "+",
        "12-1 动量：adj_close[t-20]/adj_close[t-250]-1",
        "Jegadeesh-Titman 1993；A 股证据弱，预期即为负结果",
        f_mom_12_1),
    "F2_reversal_1m": Factor(
        "F2_reversal_1m", "-",
        "1 月反转：-(adj_close[t]/adj_close[t-20]-1)",
        "Jegadeesh 1990 / Lehmann 1990；散户主导放大 A 股短期反转",
        f_reversal_1m),
    "F3_reversal_1w": Factor(
        "F3_reversal_1w", "-",
        "1 周反转：-(adj_close[t]/adj_close[t-5]-1)",
        "Lehmann 1990",
        f_reversal_1w),
    "F4_tsmom": Factor(
        "F4_tsmom", "+",
        "截面 TSMOM：sign(adj_close[t]/adj_close[t-120]-1)",
        "Moskowitz-Ooi-Pedersen 2012（注意：仓库测过的是择时规则，不是截面分数）",
        f_tsmom),
    "F5_high_52w": Factor(
        "F5_high_52w", "+",
        "52 周高点接近度：adj_close / max(adj_high, 250)",
        "George-Hwang 2004 锚定效应",
        f_high_52w),
    "F6_max_effect": Factor(
        "F6_max_effect", "-",
        "MAX：过去 20 日最大 5 个日收益的均值的负值",
        "Bali-Cakici-Whitelaw 2011 彩票偏好",
        f_max_effect),
    "F7_ivol": Factor(
        "F7_ivol", "-",
        "特质波动率（对等权市场回归残差，60 日，年化）的负值",
        "Ang et al. 2006",
        f_ivol),
    "F8_amihud": Factor(
        "F8_amihud", "+",
        "Amihud 非流动性：mean(|ret|/成交额)，60 日",
        "Amihud 2002；是规模代理，必须规模中性化后再看",
        f_amihud),
    "F10_overnight_intraday": Factor(
        "F10_overnight_intraday", "+",
        "隔夜/日内分解：Σ20(隔夜) − Σ20(日内)",
        "Lou-Polk-Skouras 2019；与 T+1 开盘成交的可实施性直接相关",
        f_overnight_intraday),
    "F11_low_beta": Factor(
        "F11_low_beta", "-",
        "beta（对等权市场，60 日）的负值",
        "Frazzini-Pedersen 2014 BAB",
        f_low_beta),
    "F12_nominal_price": Factor(
        "F12_nominal_price", "+",
        "名义价格：1/close",
        "Bali et al. 2014 名义价格幻觉；成本敏感，只作诊断",
        f_nominal_price),
}


def compute(panel: pd.DataFrame, key: str) -> pd.DataFrame:
    """算一个因子，返回引擎要的 `(code, date, alpha)` 长表。"""
    factor = FACTORS[key]
    market = None
    if key in ("F7_ivol", "F11_low_beta"):
        market = _market_return(panel)
    return _to_alpha(factor.fn(panel, market))
