# -*- coding: utf-8 -*-
"""Completed-session residual momentum and traded-value confirmation."""
def independent_strength(frame, benchmark):
    result = frame.sort_values(["code", "date"]).copy()
    index = benchmark.copy()
    index.index = index.index.astype(str).str[:8]
    market = index.sort_index().close.pct_change(fill_method=None)
    result["market_ret"] = result.date.map(market)
    result["cross_return"] = result.ret * result.market_ret
    result["market_square"] = result.market_ret ** 2
    group = result.groupby("code", sort=False)
    stock_mean = group.ret.transform(lambda x: x.rolling(60).mean())
    market_mean = group.market_ret.transform(lambda x: x.rolling(60).mean())
    covariance = group.cross_return.transform(lambda x: x.rolling(60).mean()) - stock_mean * market_mean
    variance = group.market_square.transform(lambda x: x.rolling(60).mean()) - market_mean ** 2
    result["beta"] = (covariance / variance.where(variance > 1e-12)).groupby(result.code).shift()
    result["residual"] = result.ret - result.beta * result.market_ret
    residual_mean = group.residual.transform(lambda x: x.rolling(60).mean())
    residual_std = group.residual.transform(lambda x: x.rolling(60).std())
    result["residual_strength"] = residual_mean / residual_std.where(residual_std > 1e-12)
    falling = result.market_ret < 0
    result["down_stock"] = result.ret.where(falling, 0)
    result["down_market"] = result.market_ret.where(falling, 0)
    result["falling"] = falling.astype(float)
    down_stock = group.down_stock.transform(lambda x: x.rolling(60).sum())
    down_market = group.down_market.transform(lambda x: x.rolling(60).sum())
    down_count = group.falling.transform(lambda x: x.rolling(60).sum())
    result["downside_capture"] = (down_stock / down_market.where(down_market < 0)).where(down_count >= 10)
    result["up_amount"] = result.amount.where(result.ret > 0, 0)
    up_amount = group.up_amount.transform(lambda x: x.rolling(20).sum())
    total_amount = group.amount.transform(lambda x: x.rolling(20).sum())
    result["up_amount_share"] = up_amount / total_amount.where(total_amount > 0)
    daily = result.groupby("date")
    result["residual_rank"] = daily.residual_strength.rank(pct=True)
    result["defense_rank"] = daily.downside_capture.rank(pct=True, ascending=False)
    result["up_amount_rank"] = daily.up_amount_share.rank(pct=True)
    return result.drop(columns=["cross_return", "market_square", "down_stock", "down_market", "falling", "up_amount"])


def rank_independent_strength(prepared, mode):
    result = prepared.copy()
    original = result.score.copy()
    if mode == "residual_rank":
        result["score"] = .4 * original + .6 * result.residual_rank.fillna(original)
    elif mode == "residual_gate":
        result["entry"] &= (result.residual_strength > 0) & (result.residual_rank >= .7)
    elif mode == "defense_rank":
        result["score"] = .5 * original + .5 * result.defense_rank.fillna(original)
    elif mode == "up_amount_rank":
        result["score"] = .5 * original + .5 * result.up_amount_rank.fillna(original)
    elif mode == "residual_amount":
        result["score"] = (.4 * original + .3 * result.residual_rank.fillna(original) +
                           .3 * result.up_amount_rank.fillna(original))
    elif mode != "baseline":
        raise ValueError(mode)
    return result
