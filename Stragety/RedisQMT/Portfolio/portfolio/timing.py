"""大盘仓位择时：指数 regime → 总仓位（0~1）。

## 复用仓库既有模块，但不复用它的决策函数

`Stragety/MiniQMT_Stragety/core/long_hold_allocation.py` 提供：

* `calculate_indicators(highs, lows, closes)` —— **直接用**，输入需要
  ≥140 根日线，返回 ma20/ma60/ma120/ma60_slope/signed_efficiency20/
  prior_high20/drawdown20_atr 等
* `decide_allocation(...)` —— **不用**。它是给**单只股票**写的：
  有 `BREAKOUT_20D`、`PULLBACK_x_ATR`、`sessions_since_buy`、`BUY_COOLDOWN`，
  语义是「对一只股票加仓」。套到指数上完全错位。

只用 `calculate_indicators` + `classify_regime`，并且**只缩放总仓位，
绝不改变选股结果** —— 否则选股层和择时层就无法归因。

## 为什么必须做 `rescaled` 变体

`classify_regime` 的地板是 **0.30**（BEAR 也给 30% 仓位），
**永远不可能真正空仓**。而本次的要求是 0~100% 的仓位区间，
所以 `as_is` 只是「忠实复用」，真正能空仓的是 `rescaled`。

## 先验态度要诚实

仓库自己的证据（`analysis/CaptureT_v4_择时策略结论.md`、
`analysis/趋势筛选器研究结论.md`）是：单标的 MA20/MA50/ATR 择时
**全线跑输持有** 3.5 万~12.6 万元。指数级择时的文献支持（Faber 2007；
Moskowitz-Ooi-Pedersen 2012）要强一些，机制也不同（指数趋势由宏观与
资金面驱动，比个股趋势噪声小），值得测 —— **但方案不能依赖它成立**。

Stage 2 的判定点就是：择时层若不通过，基准退回 `gross=1.0`，
策略变成纯选股，择时记录为 `无效`。**这是很可能发生的结果。**
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]

VARIANTS = ("as_is", "rescaled", "binary")

# classify_regime 原样返回的目标权重
AS_IS = {"STRONG_BULL": 0.75, "BULL": 0.60, "SIDEWAYS": 0.45, "BEAR": 0.30}
# 重标定到 0~1 —— 真正允许空仓
RESCALED = {"STRONG_BULL": 1.00, "BULL": 0.60, "SIDEWAYS": 0.30, "BEAR": 0.00}

MIN_BARS = 140           # calculate_indicators 的硬性要求
BINARY_MA = 200          # binary 变体的均线周期（Faber 2007 的 10 月均线）


def _load_regime_functions():
    """只 import 叶子模块。

    **绝不能 import `core.config`** —— `core/config.py:6` 有一句
    `from matplotlib.pylab import cond`，会直接炸。
    `core/__init__.py` 是纯 docstring 无副作用，所以只 import 叶子模块是安全的。
    """
    core_root = str(ROOT / "Stragety" / "MiniQMT_Stragety")
    if core_root not in sys.path:
        sys.path.insert(0, core_root)
    from core.long_hold_allocation import (  # noqa: E402
        calculate_indicators,
        classify_regime,
    )
    return calculate_indicators, classify_regime


def regime_series(index: pd.DataFrame, variant: str = "as_is") -> pd.Series:
    """逐日算总仓位。`index` 需含 `date/high/low/close`，按日期升序。

    **因果性**：第 t 日的仓位只用 `bar[0..t]`（含当日收盘）计算。
    引擎在 T 日收盘后调用、T+1 开盘执行，所以这个时序是正确的。
    """
    if variant not in VARIANTS:
        raise ValueError(f"未知变体 {variant!r}，可选 {VARIANTS}")

    calculate_indicators, classify_regime = _load_regime_functions()

    frame = index.sort_values("date").reset_index(drop=True)
    highs = frame["high"].to_numpy(float)
    lows = frame["low"].to_numpy(float)
    closes = frame["close"].to_numpy(float)
    dates = frame["date"].tolist()

    table = AS_IS if variant == "as_is" else RESCALED
    out = np.full(len(frame), np.nan)

    for i in range(len(frame)):
        if variant == "binary":
            if i + 1 >= BINARY_MA:
                ma = closes[i - BINARY_MA + 1:i + 1].mean()
                out[i] = 1.0 if closes[i] > ma else 0.0
            continue
        if i + 1 < MIN_BARS:
            continue
        try:
            indicators = calculate_indicators(
                highs[:i + 1].tolist(), lows[:i + 1].tolist(),
                closes[:i + 1].tolist())
        except ValueError:
            continue
        regime, _ = classify_regime(indicators)
        out[i] = table.get(regime, 0.0)

    series = pd.Series(out, index=dates).dropna()
    return series


def with_hysteresis(series: pd.Series, confirm_days: int = 2) -> pd.Series:
    """分层变化需连续 `confirm_days` 日确认才生效。

    实盘用。一次假突破就换仓会白付成本 —— 指数在均线附近反复穿越是常态。
    **回测里默认不开启**（`confirm_days=1` 等价于不开），
    因为回测要回答的是「这个信号本身有没有用」，而不是「迟滞规则能不能救它」。
    """
    if confirm_days <= 1:
        return series
    values = series.to_numpy()
    out = np.empty(len(values))
    out[0] = values[0]
    pending_value = None
    pending_count = 0
    for i in range(1, len(values)):
        current = values[i]
        if current == out[i - 1]:
            pending_value, pending_count = None, 0
            out[i] = current
            continue
        if pending_value is not None and current == pending_value:
            pending_count += 1
        else:
            pending_value, pending_count = current, 1
        if pending_count >= confirm_days:
            out[i] = current
            pending_value, pending_count = None, 0
        else:
            out[i] = out[i - 1]
    return pd.Series(out, index=series.index)


def load_index_from_bridge(codes: list[str], start: str, end: str,
                           count: int = 4200) -> dict[str, pd.DataFrame]:
    """从 redisQMT 桥接取指数日线。

    指数代码形如 `000300.SH`。桥接的 `get_market_data_ex` 对指数同样有效，
    不需要特殊处理。
    """
    sys.path.insert(0, str(ROOT / "integrations/bigqmt/src"))
    from bigqmt_signal_trader.xtquant_compat import configure

    _, data = configure(account_id="8890145315", timeout_seconds=90)
    data.download_history_data2(codes, "1d", start_time=start, end_time=end,
                                download_timeout_seconds=300)
    raw = data.get_market_data_ex(["open", "high", "low", "close", "volume",
                                   "amount"], codes, period="1d", count=count,
                                  dividend_type="none", fill_data=False,
                                  timeout_seconds=120)
    out = {}
    for code, block in (raw or {}).items():
        if block is None or len(block) == 0:
            continue
        frame = block.reset_index()
        frame.columns = ["date"] + list(frame.columns[1:])
        frame["date"] = frame["date"].astype(str).str.replace("-", "")
        out[code] = frame.sort_values("date").reset_index(drop=True)
    return out
