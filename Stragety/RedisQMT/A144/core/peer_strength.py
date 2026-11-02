# -*- coding: utf-8 -*-
"""Monthly past-return neighbors; no present-day industry classification dependency."""
from __future__ import annotations

import numpy as np
import pandas as pd


def monthly_peer_strength(frame, first_month, last_month):
    codes = sorted(frame.code.unique())
    returns = frame.pivot(index="date", columns="code", values="ret").reindex(columns=codes).sort_index()
    momentum = frame.pivot(index="date", columns="code", values="mom20_rank").reindex(columns=codes)
    above = frame.assign(above=(frame.adj_close > frame.ma50).astype(float)).pivot(
        index="date", columns="code", values="above").reindex(columns=codes)
    strength = pd.Series(np.nan, index=frame.index, name="peer_strength")
    breadth = pd.Series(np.nan, index=frame.index, name="peer_breadth")
    models = {}
    for month in sorted(set(frame.date.str[:6])):
        if month < first_month or month > last_month:
            continue
        cutoff = (pd.Timestamp(month + "01") - pd.Timedelta(days=1)).strftime("%Y%m%d")
        historical = returns.loc[returns.index <= cutoff].tail(60)
        if len(historical) < 60:
            raise ValueError(f"insufficient correlation history: {month}")
        eligible = (historical.notna().sum() >= 50) & (historical.std() > 1e-6)
        matrix = historical.fillna(0).clip(-.2, .2).to_numpy()
        centered = matrix - matrix.mean(axis=0)
        norms = np.sqrt((centered ** 2).sum(axis=0))
        correlations = centered.T @ centered / np.maximum(norms[:, None] * norms[None, :], 1e-12)
        valid = eligible.to_numpy()
        correlations[~valid, :] = -2
        correlations[:, ~valid] = -2
        np.fill_diagonal(correlations, -2)
        current = frame.loc[frame.date.str[:6] == month]
        dates = sorted(current.date.unique())
        mom = momentum.loc[dates].to_numpy()
        trend = above.loc[dates].to_numpy()
        month_models = {}
        for column, code in enumerate(codes):
            neighbors = np.argsort(correlations[column])[::-1][:10]
            neighbors = neighbors[correlations[column, neighbors] >= .4]
            if len(neighbors) < 3:
                continue
            month_models[code] = [codes[index] for index in neighbors]
            mean_mom = pd.Series(pd.DataFrame(mom[:, neighbors]).mean(axis=1).to_numpy(), index=dates)
            mean_trend = pd.Series(pd.DataFrame(trend[:, neighbors]).mean(axis=1).to_numpy(), index=dates)
            selected = current.code == code
            positions = current.index[selected]
            strength.loc[positions] = current.loc[positions, "date"].map(mean_mom)
            breadth.loc[positions] = current.loc[positions, "date"].map(mean_trend)
        models[month] = {"cutoff": cutoff, "last_return_date": str(historical.index.max()),
                         "lookback": 60, "neighbors": month_models}
    return strength, breadth, models
