# -*- coding: utf-8 -*-
"""peak 语义：风控开关的跟踪止损锚怎么取。单一事实来源。

策略文件、`analysis/timing_lab_*.py`、`analysis/screener_lab_*.py` 三处都要
用同一套语义，所以解析放在这里而不是各写一份。三种读法回答的是不同的问题：

  * ``MARKET``  —— 重放窗口内收盘价的**运行最高值**，永不重置。
                   读作「这个市场离它自己的高点有多远」，是 regime 锚。
                   必须不可衰减：一旦改成滚动窗口，下跌时锚会跟着价格下移，
                   止损线永远追不上，止损就再也不会触发。
  * ``REENTRY`` —— 每次「空仓 → 回场」时重置为回场当日的收盘价。
                   读作「当前这手持仓的高水位」。
  * ``ROLLING`` —— 近 N 个交易日收盘价的最高值（默认 20 个交易日 ≈ 一个月）。
                   读作「最近有没有快速下跌」，是**短期**锚。窗口内的最高值
                   会随价格一起下移，所以**只有比窗口更快的下跌才会触发**；
                   阴跌（比窗口慢）它一路跟着走，永不触发。

``ROLLING<N>`` 可以显式指定窗口，供研究脚本扫参；单独的 ``ROLLING`` 用调用方
传入的默认窗口。**新增一种语义时只改这里，三处调用点自动一致。**
"""

MARKET = 'MARKET'
REENTRY = 'REENTRY'
ROLLING = 'ROLLING'
KINDS = (MARKET, REENTRY, ROLLING)

#: 默认滚动窗口：20 个交易日 ≈ 一个自然月。
DEFAULT_ROLLING_WINDOW = 20


def parse_peak_mode(mode, default_window=DEFAULT_ROLLING_WINDOW):
    """把模式字符串解析成 ``(kind, window)``。

    ``window`` 只在 ``kind == ROLLING`` 时非 None。大小写不敏感。
    无法识别时抛 ``ValueError`` —— 宁可启动就炸，也不要静默退回某个语义。
    """
    if not isinstance(mode, str):
        raise ValueError('unknown peak mode: ' + repr(mode))
    token = mode.strip().upper()
    if token in (MARKET, REENTRY):
        return token, None
    if token == ROLLING:
        token = ROLLING + str(int(default_window))
    if token.startswith(ROLLING) and token[len(ROLLING):].isdigit():
        window = int(token[len(ROLLING):])
        if window >= 2:
            return ROLLING, window
    raise ValueError('unknown peak mode: ' + str(mode))


def rolling_peak_series(closes, default_window=DEFAULT_ROLLING_WINDOW):
    """按 ``kind`` 需要预先算好的滚动最高收盘价（numpy 数组）。

    只给 ``ROLLING`` 用：把窗口最大值提到循环外算一次，内层才只剩标量比较。
    用 ``min_periods=1``，所以序列开头也有定义（等价于扩张窗口）。
    """
    import numpy as np
    import pandas as pd

    return (pd.Series(np.asarray(closes, dtype=float))
            .rolling(int(default_window), min_periods=1).max().to_numpy())
