# -*- coding: utf-8 -*-
"""
core/signals.py — 动态乘数模型 + 当日信号计算
=============================================
calc_dynamic_sell_mult() : 多因子动态乘数
compute_signal()         : 每日信号综合计算
"""
from . import config as cfg
from .indicators import sma, up_streak, daily_range_ma
from .indicators_np import atr, rsi
from .instrument_rules import round_price


def calc_dynamic_sell_mult(trend, atr_pct, atr_ratio, vol_ratio, rsi_val, up_streak_val):
    """
    多因子动态乘数模型。

    因子: 趋势、波动率(ATR绝对值+相对值)、成交量、RSI
    返回: (final_mult, factor_details, base_mult)
    """
    # 基础乘数
    if trend == 'bear':
        base = cfg.SELL_TRIGGER_BASE_BEAR
    elif trend == 'weak_bull':
        base = cfg.SELL_TRIGGER_BASE_WEAK_BULL
    else:
        base = cfg.SELL_TRIGGER_BASE_SIDEWAYS

    deviations = {}
    total_deviation = 0.0

    # 因子1: 趋势
    if trend == 'bear':
        d = -0.25 if up_streak_val == 0 else -0.15
    elif trend == 'strong_bull':
        d = +999
    elif trend == 'weak_bull':
        if up_streak_val >= 3:     d = +0.20
        elif up_streak_val >= 1:   d = +0.12
        else:                       d = +0.05
    else:
        d = 0.00
    deviations['trend'] = d
    total_deviation += d

    # 因子2: 波动率 (ATR绝对值 + 相对值)
    if atr_pct > 0.08:        atr_d = -0.30
    elif atr_pct > 0.07:      atr_d = -0.22
    elif atr_pct > 0.06:      atr_d = -0.15
    elif atr_pct > 0.05:      atr_d = -0.08
    elif atr_pct > 0.03:      atr_d = +0.05
    elif atr_pct > 0.02:      atr_d = +0.15
    else:                     atr_d = +0.25

    if atr_ratio > 1.50:         atrd_d = -0.25
    elif atr_ratio > 1.25:       atrd_d = -0.18
    elif atr_ratio > 1.10:       atrd_d = -0.10
    elif atr_ratio > 0.90:       atrd_d = 0.00
    elif atr_ratio > 0.70:       atrd_d = +0.12
    elif atr_ratio > 0.50:       atrd_d = +0.20
    else:                        atrd_d = +0.25

    vol_d = atr_d * 0.55 + atrd_d * 0.45
    vol_d = max(-0.35, min(0.30, vol_d))
    deviations['volatility'] = round(vol_d, 2)
    total_deviation += vol_d

    # 因子3: 成交量。数据无效时保持中性，不用伪造的 1.0 影响乘数。
    if vol_ratio is None:        d = 0.00
    elif vol_ratio > 2.00:       d = -0.25
    elif vol_ratio > 1.50:       d = -0.18
    elif vol_ratio > 1.20:       d = -0.08
    elif vol_ratio > 0.80:       d = 0.00
    elif vol_ratio > 0.60:       d = +0.12
    elif vol_ratio > 0.40:       d = +0.20
    else:                        d = +0.25
    deviations['volume'] = d
    total_deviation += d

    # 因子4: RSI
    if rsi_val > 80:           d = -0.25
    elif rsi_val > 70:         d = -0.18
    elif rsi_val > 60:         d = -0.08
    elif rsi_val > 55:         d = -0.03
    elif rsi_val > 45:         d = 0.00
    elif rsi_val > 40:         d = +0.03
    elif rsi_val > 30:         d = +0.10
    elif rsi_val > 20:         d = +0.20
    else:                      d = +0.25
    deviations['RSI'] = d
    total_deviation += d

    final = base + total_deviation
    final = max(cfg.DYNAMIC_MULT_MIN, min(cfg.DYNAMIC_MULT_MAX, final))
    return round(final, 2), deviations, base


def compute_signal(opens, highs, lows, closes, volumes, yesterday_close=None,
                   today_open=None, price_tick=0.01):
    """
    计算当日信号。

    Args:
        opens, highs, lows, closes, volumes: list[float], 日线数据 (最后一条是今日/昨日)
        yesterday_close: float | None, 昨收(真实价, 来自实时tick lastClose)。
            用于触发价基准, 与今开同源; 缺省时回退到日线 closes[-1](前复权)。
        price_tick: 报价网格；默认股票0.01，基金由调用方显式传0.001。

    Returns:
        dict | None: 信号字典, 数据不足时返回 None
    """
    n = len(closes)
    if n < 60:
        return None

    # Historical arrays contain complete daily bars only.  The current session
    # open comes from the live tick and must never overwrite a historical bar.
    co = float(today_open) if today_open is not None and float(today_open) > 0 else opens[-1]
    cc = closes[-1]
    # 日线成交量必须与价格序列对齐，且至少有“当前完整日 +
    # 之前20个完整日”。不满足时显式标记无效，不回退为 1.0。
    volume_aligned = len(volumes) == n
    cv = float(volumes[-1]) if volume_aligned and volumes else 0.0

    atr_arr = atr(highs, lows, closes, cfg.ATR_PERIOD)
    curr_atr = atr_arr[-1] or cc * 0.03
    curr_atr_pct = curr_atr / cc if cc > 0 else 0.03

    atr_ma20 = sma(atr_arr, 20)[-1] if n >= 20 else curr_atr
    atr_ratio = curr_atr / atr_ma20 if atr_ma20 > 0 else 1.0

    ma5  = sma(closes, 5)[-1]
    ma20 = sma(closes, 20)[-1]
    curr_rsi = rsi(closes)[-1]
    up_streak_val = up_streak(closes)[-1]

    price_above_ma = (cc > ma20) and (ma5 > ma20)
    price_below_ma = (cc < ma20) and (ma5 < ma20)

    if price_above_ma and curr_rsi > cfg.STRONG_BULL_RSI and up_streak_val >= cfg.STRONG_BULL_STREAK:
        trend = 'strong_bull'
    elif price_above_ma:
        trend = 'weak_bull'
    elif price_below_ma:
        trend = 'bear'
    else:
        trend = 'sideways'

    previous_volumes = [float(value) for value in volumes[-21:-1]] if volume_aligned and len(volumes) >= 21 else []
    volume_baseline_count = len(previous_volumes)
    volume_avg20 = sum(previous_volumes) / 20.0 if volume_baseline_count == 20 else 0.0
    volume_valid = cv > 0 and volume_avg20 > 0
    curr_vr = cv / volume_avg20 if volume_valid else None

    sell_mult, factor_details, base_used = calc_dynamic_sell_mult(
        trend, curr_atr_pct, atr_ratio, curr_vr, curr_rsi, up_streak_val
    )

    daily_range_ma10 = daily_range_ma(highs, lows, opens, 10)[-1]
    max_trigger_by_range = co * (1.0 + daily_range_ma10 * cfg.DAILY_RANGE_CAP_MULT)
    range_capped = False

    # ★ 触发基准价: 取今开与昨收的较高者。卖出触发价必须站上"今开/昨收的高水位",
    #   保证触发价始终高于开盘价(不会出现"需涨为负"的错误)。
    #   昨收优先用实时tick的 lastClose(与今开同源, 真实价), 缺失时回退到日线 closes[-1]。
    # ref_close = yesterday_close if (yesterday_close and yesterday_close > 0) else cc
    ref = co
    sell_trigger_raw = ref * (1.0 + curr_atr_pct * sell_mult * cfg.SELL_TRIGGER_SCALE)

    if cfg.DAILY_RANGE_CAP_ENABLED and sell_trigger_raw > max_trigger_by_range:
        sell_trigger = round_price(max_trigger_by_range, price_tick)
        range_capped = True
    else:
        sell_trigger = round_price(sell_trigger_raw, price_tick)

    do_short = True
    reason = ''

    if trend == 'strong_bull':
        do_short = False
        reason = 'strong bull blocks REV-T (streak>=5 + RSI>70)'
    elif volume_valid and curr_vr < cfg.VOLUME_FILTER_RATIO:
        do_short = False
        reason = f'low volume (vol_ratio {curr_vr:.2f})'
    elif curr_rsi > cfg.RSI_OVERBOUGHT:
        do_short = False
        reason = f'RSI overbought ({curr_rsi:.0f})'

    return {
        'do_short': do_short, 
        'blocked_reason': reason, 
        'trend': trend,
        'sell_trigger': sell_trigger, 
        'sell_trigger_raw': round_price(sell_trigger_raw, price_tick),
        'range_capped': range_capped, 
        'daily_range_ma10': daily_range_ma10,
        'open_price': co, 
        'close_yday': cc,
        'atr': curr_atr, 
        'atr_pct': curr_atr_pct, 
        'rsi': curr_rsi,
        'vol_ratio': curr_vr,
        'volume_valid': volume_valid,
        'volume_current': cv,
        'volume_avg20': volume_avg20,
        'volume_baseline_count': volume_baseline_count,
        'sell_mult': sell_mult, 
        'sell_mult_base': base_used,
        'factor_details': factor_details, 
        'atr_ratio': atr_ratio,
        'up_streak': up_streak_val, 
        'buyback_mult': cfg.BUYBACK_TRIGGER_MULT,
        'bounce_pct': cfg.BOUNCE_PCT,
    }
