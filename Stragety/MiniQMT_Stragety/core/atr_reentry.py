# -*- coding: utf-8 -*-
"""Next-cycle thresholds anchored to a confirmed closing execution."""
import math
import numpy as np
from .indicators_np import atr
from .instrument_rules import round_price, price_decimals

# 仅使用最近已完成日线；窗口越长越稳定，越短越灵敏。
REENTRY_HISTORY_DAYS = 80
# ATR窗口=样本数的此指数次方，0.5为平方根窗口。
REENTRY_LOOKBACK_EXPONENT = 0.5
# 中位分位为默认：上、下行分别由各自历史幅度计算，不固定ATR倍数。
# 调大需要更大的再次波动，触发更少；调小更容易再次入场。
REENTRY_QUANTILE = 0.5
REENTRY_MIN_HISTORY = 30
REENTRY_PRICE_TICK = 0.01


def calculate_atr_reentry(base, opens, highs, lows, closes, price_tick=REENTRY_PRICE_TICK):
    price_decimals(price_tick)  # Validate an explicitly supplied stock/fund grid.
    n = min(REENTRY_HISTORY_DAYS, len(opens), len(highs), len(lows), len(closes))
    if not math.isfinite(base) or base <= 0 or n < REENTRY_MIN_HISTORY:
        return None
    o, h, l, c = [np.asarray(x[-n:], dtype=float) for x in (opens, highs, lows, closes)]
    if (not all(np.isfinite(x).all() for x in (o, h, l, c)) or
            np.any(o <= 0) or np.any(c <= 0)):
        return None
    span = max(2, int(n ** REENTRY_LOOKBACK_EXPONENT))
    values = np.asarray(atr(h, l, c, span), dtype=float)
    ups, downs = [], []
    for i in range(span, n - 1):
        fraction = values[i] / c[i]
        if not math.isfinite(fraction) or fraction <= 0:
            continue
        ups.append(max(0.0, h[i + 1] / o[i + 1] - 1) / fraction)
        downs.append(max(0.0, 1 - l[i + 1] / o[i + 1]) / fraction)
    fraction = float(values[-1] / c[-1])
    if not ups or not math.isfinite(fraction) or fraction <= 0:
        return None
    up = float(np.quantile(ups, REENTRY_QUANTILE))
    down = float(np.quantile(downs, REENTRY_QUANTILE))
    # 至少离基准一个价位，避免历史分位为0时立即重新入场。
    sell = round_price(base + max(base * fraction * up, price_tick), price_tick, 'ceil')
    buy = round_price(base - max(base * fraction * down, price_tick), price_tick, 'floor')
    if buy <= 0:
        return None
    return dict(sell_trigger=sell, buy_trigger=buy, atr_pct=fraction,
                up_units=up, down_units=down, quantile=REENTRY_QUANTILE,
                sample_count=len(ups), span=span, base=base)
