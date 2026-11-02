# -*- coding: utf-8 -*-
"""Monthly causal logistic quality estimates using already resolved daily labels."""
from __future__ import annotations

import numpy as np
import pandas as pd


FEATURES = ("mom20_rank", "mom60_rank", "mom120_rank", "efficiency", "atr_pct",
            "ret", "extension", "ma_spread", "turnover_ratio", "range", "maxrange5",
            "breadth", "market_strict", "impact20_rank", "mom20", "mom60")


def feature_matrix(frame):
    values = frame.copy()
    values["extension"] = ((values.adj_close - values.ma20) / values.atr).clip(-5, 10)
    values["ma_spread"] = values.ma20 / values.ma50 - 1
    values["turnover_ratio"] = (values.amount / values.amount20).clip(0, 5)
    return values[list(FEATURES)].astype(float).to_numpy()


def fit_logistic(x, y):
    mean = x.mean(axis=0)
    scale = np.maximum(x.std(axis=0), 1e-8)
    z = np.column_stack([np.ones(len(x)), (x - mean) / scale])
    coefficients = np.zeros(z.shape[1])
    prior = float(y.mean())
    if not 0 < prior < 1:
        raise ValueError("training labels require both classes")
    coefficients[0] = np.log(prior / (1 - prior))
    penalty = np.eye(z.shape[1]) * .1
    penalty[0, 0] = 0
    for _ in range(12):
        probability = 1 / (1 + np.exp(-np.clip(z @ coefficients, -30, 30)))
        weights = probability * (1 - probability)
        gradient = z.T @ (probability - y) / len(y) + penalty @ coefficients
        hessian = z.T @ (weights[:, None] * z) / len(y) + penalty
        delta = np.linalg.solve(hessian, gradient)
        coefficients -= delta
        if np.max(np.abs(delta)) < 1e-7:
            break
    return mean, scale, coefficients


def predict_monthly(frame, first_month, last_month):
    group = frame.groupby("code", sort=False)
    label_end = group.date.shift(-20)
    target = group.adj_close.shift(-20) / frame.adj_close - 1
    x = feature_matrix(frame)
    finite = np.isfinite(x).all(axis=1)
    eligible = ((frame.age >= 130) & (frame.adj_close > frame.ma50) &
                (frame.mom60 > 0) & (frame.mom60_rank >= .7) &
                (frame.amount20 >= 3e7) & frame.atr_pct.between(.01, .08) &
                (frame.volume > 0) & (frame.amount > 0) & finite)
    predictions = pd.Series(np.nan, index=frame.index, name="predicted_win")
    models = {}
    months = sorted(set(frame.date.str[:6]))
    for month in months:
        if month < first_month or month > last_month:
            continue
        cutoff = (pd.Timestamp(month + "01") - pd.Timedelta(days=1)).strftime("%Y%m%d")
        available = eligible & target.notna() & (label_end <= cutoff) & (frame.date <= cutoff)
        dates = sorted(frame.loc[available, "date"].unique())[-180:]
        training = available & frame.date.isin(dates)
        if int(training.sum()) < 1000:
            raise ValueError(f"insufficient resolved training data: {month}")
        mean, scale, coefficients = fit_logistic(x[training], (target.loc[training] > 0).to_numpy(float))
        selected = (frame.date.str[:6] == month) & finite
        z = np.column_stack([np.ones(int(selected.sum())), (x[selected] - mean) / scale])
        predictions.loc[selected] = 1 / (1 + np.exp(-np.clip(z @ coefficients, -30, 30)))
        models[month] = {"cutoff": cutoff, "rows": int(training.sum()),
                         "first_feature_date": str(frame.loc[training, "date"].min()),
                         "last_feature_date": str(frame.loc[training, "date"].max()),
                         "last_label_date": str(label_end.loc[training].max()),
                         "features": list(FEATURES), "mean": mean.tolist(), "scale": scale.tolist(),
                         "coefficients": coefficients.tolist()}
        assert models[month]["last_label_date"] <= cutoff
    return predictions, models
