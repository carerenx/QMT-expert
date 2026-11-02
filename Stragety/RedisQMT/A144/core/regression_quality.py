# -*- coding: utf-8 -*-
"""Signed R-squared of trailing log-price trends, ending at signal close."""
import numpy as np


def signed_r_squared(values):
    x = np.arange(len(values), dtype=float)
    x -= x.mean()
    y = values - values.mean()
    denominator = (x @ x) * (y @ y)
    if denominator <= 1e-12:
        return np.nan
    product = x @ y
    return float(np.sign(product) * np.clip(product ** 2 / denominator, 0, 1))


def regression_quality(frame):
    result = frame.sort_values(["code", "date"]).copy()
    result["log_close"] = np.log(result.adj_close.where(result.adj_close > 0))
    group = result.groupby("code", sort=False)
    for window in (20, 60):
        field = "regression" + str(window)
        result[field] = group.log_close.transform(lambda x: x.rolling(window).apply(signed_r_squared, raw=True))
        result[field + "_rank"] = result.groupby("date")[field].rank(pct=True)
    return result.drop(columns="log_close")


def rank_regression(frame, mode):
    result = frame.copy()
    if mode == "baseline":
        return result
    if mode == "regression20":
        quality = result.regression20_rank
    elif mode == "regression60":
        quality = result.regression60_rank
    elif mode == "dual_regression":
        quality = result[["regression20_rank", "regression60_rank"]].min(axis=1, skipna=False)
    elif mode == "blended_quality":
        quality = .5 * result.efficiency_rank + .5 * result.regression60_rank
    else:
        raise ValueError(mode)
    result["score"] = (.7 * result.mom60_rank + .3 * quality).fillna(result.score)
    return result
