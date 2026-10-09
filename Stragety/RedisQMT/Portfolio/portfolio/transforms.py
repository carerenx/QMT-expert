"""截面变换：缩尾、标准化、中性化。

## 为什么中性化是必要的，而不是「更精细的调参」

`F8 Amihud 非流动性` 是**规模的代理** —— 成交额小的股票天然非流动性高。
不做规模中性化，「低流动性溢价」和「小市值溢价」就分不开，
会得出「非流动性因子有效」这种没有信息量的结论。

## 规模代理只能用成交额

真正的规模是流通市值 = 股价 × 流通股本。桥接的 `get_instrument_detail`
给得出**当前**流通股本，但拿不到历史 —— 而流通股本会因解禁、增发而变，
用当前值回填历史是错的。

所以用 `log(20 日均成交额)` 作规模代理。它与市值高度相关（相关性通常在
0.8 以上），但不是市值本身。**这是数据源限制导致的近似，报告里必须标注。**

## 行业中性化的缺失

原计划用 baostock 的 `query_stock_industry` 做行业中性化，但 baostock
账号被锁，桥接也没有现成的历史行业分类接口。所以**本批研究不做行业中性化**,
这是与计划的一处偏差，在预注册文件里写明。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


WINSOR_Q = 0.01          # 双侧各 1%


def winsorize(series: pd.Series, q: float = WINSOR_Q) -> pd.Series:
    """按分位数缩尾。极端值对 z-score 的影响是平方级的。"""
    if series.dropna().empty:
        return series
    low = series.quantile(q)
    high = series.quantile(1.0 - q)
    return series.clip(lower=low, upper=high)


def zscore(series: pd.Series) -> pd.Series:
    """截面标准化。标准差为 0 时返回全 0 而不是 NaN。"""
    std = series.std(ddof=0)
    if not np.isfinite(std) or std <= 1e-12:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def neutralize_by(frame: pd.DataFrame, target: str,
                  by: str = "log_amount") -> pd.Series:
    """对 `by` 做截面回归，返回残差。

    逐日、逐截面跑 OLS。用解析解 `r - beta × x`（`x` 已标准化），
    避免每天调用一次 `np.linalg.lstsq`。

    `frame` 必须含 `date`、`target`、`by` 三列，长表格式。
    """
    # 显式循环而不是 groupby.apply —— apply 会把分组列带进函数体
    # （pandas 2.2+ 的 FutureWarning），而且逐日构造 Series 的开销更大。
    #
    # **但必须自己按日期排序**：`preprocess` 里先做过 merge，行序是按
    # code 排的，日期并不连续。直接在原顺序上切「同一天」的边界会切错。
    result = np.full(len(frame), np.nan)
    values = frame[target].to_numpy(float)
    by_values = frame[by].to_numpy(float)
    dates = frame["date"].to_numpy()

    order = np.argsort(dates, kind="stable")
    ordered_dates = dates[order]
    starts = np.flatnonzero(np.r_[True, ordered_dates[1:] != ordered_dates[:-1]])
    stops = np.r_[starts[1:], len(ordered_dates)]

    for start, stop in zip(starts, stops):
        idx = order[start:stop]
        y = values[idx]
        x = by_values[idx]
        mask = np.isfinite(y) & np.isfinite(x)
        if mask.sum() < 10:
            continue
        yv = y[mask]
        xv = zscore(winsorize(pd.Series(x[mask]))).to_numpy()
        denom = float((xv ** 2).mean())
        if denom <= 1e-12:
            continue
        # 单变量 OLS：x 已标准化 → beta = E[xy] / E[x²]
        beta = float((xv * yv).mean()) / denom
        result[idx[mask]] = yv - beta * xv

    return pd.Series(result, index=frame.index)


def preprocess(alpha: pd.DataFrame, size: pd.DataFrame | None = None,
               neutralize: bool = False) -> pd.DataFrame:
    """把一个因子的原始值变成可比较的截面分数。

    `alpha` 是 `(code, date, alpha)` 长表。`size` 是 `(code, date, size)`
    的规模代理。`neutralize=True` 时对规模做截面回归取残差。

    顺序：缩尾 → 标准化 → （可选）规模中性化 → 再标准化。
    中性化之后必须重新标准化 —— 回归残差的尺度已经变了。
    """
    frame = alpha.copy()
    frame = frame[np.isfinite(frame["alpha"])]
    if frame.empty:
        return frame

    frame["alpha"] = (frame.groupby("date")["alpha"]
                      .transform(lambda s: zscore(winsorize(s))))

    if neutralize:
        if size is None:
            raise ValueError("neutralize=True 时必须提供 size")
        frame = frame.merge(size, on=["code", "date"], how="left")
        frame["alpha"] = neutralize_by(frame, "alpha", size.columns[-1])
        frame = frame[np.isfinite(frame["alpha"])]
        if frame.empty:
            return frame[["code", "date", "alpha"]]
        frame["alpha"] = (frame.groupby("date")["alpha"]
                          .transform(lambda s: zscore(winsorize(s))))

    return frame[["code", "date", "alpha"]]


def size_proxy(panel: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """规模代理：`log(20 日均成交额)`，按 (code, date)。

    全局中心化（减去全样本均值）只是为了让数值好读，
    不影响截面排序。
    """
    frame = panel[["code", "date", "amount"]].sort_values(["code", "date"])
    frame["log_amount"] = np.log(frame["amount"].replace(0.0, np.nan))
    frame["log_amount"] = frame.groupby("code")["log_amount"].transform(
        lambda s: s.rolling(window, min_periods=window // 2).mean())
    return frame[["code", "date", "log_amount"]].dropna()
