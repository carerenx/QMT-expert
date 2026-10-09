"""绩效指标、信息系数、以及多重检验的校正。

## 为什么这里要有 Deflated Sharpe

预注册的网格是 66 + 72 个配置。在 138 次尝试里挑出最好的那个，
它的 Sharpe **天然会高** —— 这是选择偏差，不是技能。

Bailey & López de Prado 的 Deflated Sharpe Ratio 把「试了多少次」
折进显著性里：

    DSR = Φ( (SR − SR*) × sqrt(T−1) / sqrt(1 − γ₃·SR + (γ₄−1)/4·SR²) )

其中 `SR*` 是**在 N 次独立尝试下、纯运气能达到的期望最大 Sharpe**：

    SR* = sqrt(Var(SR)) × ( (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) )

`γ` 是欧拉常数。**N 取 `trials_log.csv` 的实际行数**，
不取我们「声称」试了多少次 —— 这是本模块存在的全部理由。

## 随机对照

比 DSR 更直观、也更难糊弄的一道检查：用**同一套管线**跑一批随机
截面分数，看它们的 best-of-N Sharpe 分布落在哪。
候选因子若落在分布之内，它就只是运气。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


EULER_GAMMA = 0.5772156649015329
TRADING_DAYS = 243.0      # A 股年均交易日（用实际交易日数更准，见 periods_per_year）


def periods_per_year(dates) -> float:
    """用实际日历推算年化因子，而不是写死 252。"""
    dates = pd.Index(sorted(set(dates)))
    if len(dates) < 2:
        return TRADING_DAYS
    span = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
    if span <= 0:
        return TRADING_DAYS
    return len(dates) / (span / 365.25)


# ─────────────────────────── 基础指标 ───────────────────────────

def annualized_return(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    total = equity.iloc[-1] / equity.iloc[0]
    years = periods_per_year(equity.index)
    span_years = (pd.Timestamp(equity.index[-1])
                  - pd.Timestamp(equity.index[0])).days / 365.25
    if span_years <= 0:
        return float("nan")
    return float(total ** (1.0 / span_years) - 1.0)


def annualized_vol(returns: pd.Series) -> float:
    if len(returns) < 2:
        return float("nan")
    return float(returns.std(ddof=1) * np.sqrt(periods_per_year(returns.index)))


def sharpe(returns: pd.Series, rf: float = 0.0) -> float:
    """年化夏普。`rf` 是年化无风险利率，A 股研究里通常取 0。"""
    if len(returns) < 2:
        return float("nan")
    ann_ret = returns.mean() * periods_per_year(returns.index) - rf
    vol = annualized_vol(returns)
    if not np.isfinite(vol) or vol <= 1e-12:
        return float("nan")
    return float(ann_ret / vol)


def max_drawdown(equity: pd.Series) -> float:
    if len(equity) < 2:
        return float("nan")
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def calmar(equity: pd.Series) -> float:
    mdd = abs(max_drawdown(equity))
    if mdd <= 1e-12:
        return float("nan")
    return float(annualized_return(equity) / mdd)


def alpha_beta(returns: pd.Series, benchmark: pd.Series) -> tuple[float, float]:
    """对基准回归，返回 (年化 alpha, alpha 的 t 值)。

    这是识破「拿 beta 冒充 alpha」的关键工具。择时层改变平均仓位，
    朴素比较会被 beta 污染 —— 平均仓位 60% 的策略在牛市里看起来
    「增加了收益」，其实什么都没做。
    """
    frame = pd.concat([returns.rename("r"), benchmark.rename("b")],
                      axis=1).dropna()
    if len(frame) < 30:
        return float("nan"), float("nan")
    x = frame["b"].to_numpy(float)
    y = frame["r"].to_numpy(float)
    design = np.column_stack([np.ones_like(x), x])
    coef, residuals, _, _ = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ coef
    resid = y - fitted
    dof = len(y) - 2
    if dof <= 0:
        return float("nan"), float("nan")
    sigma2 = float(resid @ resid) / dof
    xtx_inv = np.linalg.inv(design.T @ design)
    se_alpha = np.sqrt(sigma2 * xtx_inv[0, 0])
    ann_alpha = float(coef[0] * periods_per_year(frame.index))
    t_alpha = float(coef[0] / se_alpha) if se_alpha > 0 else float("nan")
    return ann_alpha, t_alpha


def summarize(equity: pd.Series, benchmark: pd.Series | None = None) -> dict:
    """把一组常用指标打成一个 dict。"""
    returns = equity.pct_change().dropna()
    out = {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1.0)
        if len(equity) > 1 else float("nan"),
        "annual_return": annualized_return(equity),
        "annual_vol": annualized_vol(returns),
        "sharpe": sharpe(returns),
        "max_drawdown": max_drawdown(equity),
        "calmar": calmar(equity),
        "n_days": int(len(equity)),
    }
    if benchmark is not None:
        aligned = benchmark.reindex(returns.index)
        excess = returns - aligned
        out["excess_annual"] = float(
            excess.mean() * periods_per_year(excess.index))
        out["tracking_error"] = annualized_vol(excess.dropna())
        te = out["tracking_error"]
        out["information_ratio"] = (
            float(out["excess_annual"] / te)
            if np.isfinite(te) and te > 1e-12 else float("nan"))
        out["alpha"], out["alpha_t"] = alpha_beta(returns, aligned)
    return out


# ─────────────────────────── 信息系数 ───────────────────────────

def spearman_ic(scores: pd.Series, forward: pd.Series,
                min_names: int = 20) -> float:
    """截面 Spearman 相关。样本太少时返回 NaN。"""
    frame = pd.concat([scores.rename("s"), forward.rename("f")],
                      axis=1).dropna()
    if len(frame) < min_names:
        return float("nan")
    if frame["s"].nunique() < 3 or frame["f"].nunique() < 3:
        return float("nan")
    rho, _ = stats.spearmanr(frame["s"], frame["f"])
    return float(rho)


def ic_table(alpha: pd.DataFrame, panel: pd.DataFrame, horizon: int,
             step: int | None = None, price_field: str = "adj_close",
             start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """逐时点算 IC。**只用不重叠的时点**，避免自相关虚高显著性。

    `alpha` 是 `(code, date, alpha)`。`horizon` 是前瞻交易日数。
    `step` 默认等于 `horizon`（不重叠）。
    """
    step = step or horizon
    close = panel[["code", "date", price_field]].sort_values(["code", "date"])
    close["forward"] = (close.groupby("code")[price_field]
                        .transform(lambda s: s.shift(-horizon) / s - 1.0))
    merged = alpha.merge(close[["code", "date", "forward"]],
                         on=["code", "date"], how="inner")
    if start:
        merged = merged[merged["date"] >= start]
    if end:
        merged = merged[merged["date"] <= end]

    dates = sorted(merged["date"].unique())[::step]
    rows = []
    for date in dates:
        day = merged[merged["date"] == date]
        rows.append({"date": date,
                     "ic": spearman_ic(day.set_index("code")["alpha"],
                                       day.set_index("code")["forward"]),
                     "n": len(day)})
    return pd.DataFrame(rows).dropna(subset=["ic"])


def newey_west_t(values: np.ndarray, lags: int = 5) -> float:
    """IC 序列的 t 值，带 Newey-West 校正。

    IC 序列即使按不重叠时点采样，仍可能有残余自相关。
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 3:
        return float("nan")
    demeaned = values - values.mean()
    gamma0 = float(demeaned @ demeaned) / n
    variance = gamma0
    for lag in range(1, min(lags, n - 1) + 1):
        weight = 1.0 - lag / (lags + 1.0)
        cov = float(demeaned[lag:] @ demeaned[:-lag]) / n
        variance += 2.0 * weight * cov
    if variance <= 0:
        return float("nan")
    se = np.sqrt(variance / n)
    return float(values.mean() / se) if se > 0 else float("nan")


# ─────────────────────────── 多重检验校正 ───────────────────────────

def deflated_sharpe(observed_sharpe: float, n_trials: int, n_observations: int,
                    skew: float = 0.0, kurtosis: float = 3.0,
                    sharpe_variance: float | None = None) -> float:
    """Deflated Sharpe Ratio。返回「考虑试了 n_trials 次之后」的显著性概率。

    `sharpe_variance` 是各次尝试的 Sharpe 的方差。拿不到时用一个保守的
    默认值（0.5），并在报告里注明。
    """
    if not np.isfinite(observed_sharpe) or n_trials < 2 or n_observations < 10:
        return float("nan")

    var = 0.5 if sharpe_variance is None or not np.isfinite(sharpe_variance) \
        else max(sharpe_variance, 1e-6)
    # 纯运气下的期望最大 Sharpe
    expected_max = np.sqrt(var) * (
        (1.0 - EULER_GAMMA) * stats.norm.ppf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e)))

    denominator = np.sqrt(
        max(1.0 - skew * observed_sharpe
            + (kurtosis - 1.0) / 4.0 * observed_sharpe ** 2, 1e-12))
    z = ((observed_sharpe - expected_max) * np.sqrt(n_observations - 1.0)
         / denominator)
    return float(stats.norm.cdf(z))


def bonferroni_threshold(n_trials: int, alpha: float = 0.05) -> tuple[float, float]:
    """族级 Bonferroni 阈值 → (p 阈值, 对应的 |t| 阈值)。"""
    p = alpha / max(n_trials, 1)
    return p, float(abs(stats.norm.ppf(p / 2.0)))


def block_bootstrap_sharpe(returns: pd.Series, n_samples: int = 1000,
                           block: int = 20, seed: int = 20260920) -> np.ndarray:
    """分块自助重采样下的 Sharpe 分布。

    用分块而不是逐点重采样，是为了保留收益的短期自相关 ——
    逐点重采样会把真实存在的序列相关洗掉，从而低估不确定性。
    """
    values = returns.dropna().to_numpy(float)
    n = len(values)
    if n < block * 2:
        return np.array([])
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = np.arange(0, n - block + 1)
    out = np.empty(n_samples)
    for i in range(n_samples):
        picked = rng.choice(starts, size=n_blocks, replace=True)
        sample = np.concatenate([values[s:s + block] for s in picked])[:n]
        std = sample.std(ddof=1)
        out[i] = sample.mean() / std if std > 1e-12 else np.nan
    return out[np.isfinite(out)]
