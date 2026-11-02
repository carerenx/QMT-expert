# -*- coding: utf-8 -*-
"""Greedy close-time diversification using only observed trailing returns."""
import numpy as np


class CorrelationSelector:
    def __init__(self, frame, benchmark, cap, same_batch=False, residual=False):
        self.returns = frame.pivot(index="date", columns="code", values="ret").sort_index()
        index = benchmark.copy()
        index.index = index.index.astype(str).str[:8]
        self.market = index.sort_index().close.pct_change(fill_method=None)
        self.cap = cap
        self.same_batch = same_batch
        self.residual = residual
        self.decisions = {}

    def __call__(self, date, candidates, held, limit):
        history = self.returns.loc[self.returns.index <= date].tail(60)
        market = self.market.reindex(history.index).to_numpy()
        peers = [] if self.same_batch else list(held)
        selected = []
        for row in candidates.to_dict("records"):
            if len(selected) >= limit:
                break
            code = row["code"]
            comparisons = {}
            for peer in peers:
                pair = history.reindex(columns=[code, peer]).to_numpy()
                valid = np.isfinite(pair).all(axis=1)
                if self.residual:
                    valid &= np.isfinite(market)
                value = None
                if valid.sum() >= 50:
                    centered = pair[valid] - pair[valid].mean(axis=0)
                    if self.residual:
                        factor = market[valid] - market[valid].mean()
                        denominator = factor @ factor
                        if denominator > 1e-12:
                            centered -= factor[:, None] * (factor @ centered / denominator)
                    norms = np.sqrt((centered ** 2).sum(axis=0))
                    if (norms > 1e-12).all():
                        value = float(np.clip(centered[:, 0] @ centered[:, 1] / norms.prod(), -1, 1))
                comparisons[peer] = value
            accepted = all(value is not None and value <= self.cap for value in comparisons.values())
            self.decisions[date + ":" + code] = {"accepted": accepted, "peers": comparisons,
                                                 "last_history_date": str(history.index.max())}
            if accepted:
                selected.append(code)
                peers.append(code)
        return candidates.loc[candidates.code.isin(selected)]
