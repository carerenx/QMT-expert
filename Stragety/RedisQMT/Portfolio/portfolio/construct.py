"""从 alpha 分数构造目标权重。

## 缓冲带（buffer band）是这里最重要的一件事

朴素做法「每期重排、取前 N」会产生极高的换手：排名在边界附近的
股票每期进进出出，alpha 几乎没变，成本却照付。

Novy-Marx & Velikov (2016) 的做法是**不对称阈值**：

* **买入**：进入前 `buffer_entry` 分位（默认 20%）
* **卖出**：跌出前 `buffer_exit` 分位（默认 40%）才卖
* **中间**：已持有的继续持有

这样能在几乎不损失 alpha 的前提下把换手砍掉一半以上。
`buffer_exit == buffer_entry` 就退化成朴素做法，所以消融实验里
「关缓冲带」只要把两个参数设成相等即可。

## 阈值取 `max(n_holdings, 分位数)` 的原因

分位数阈值在小股票池里会小于持仓数 —— 例如只有 50 只可买、
要持 30 只、`buffer_entry=0.20` → 阈值 10 < 30，就选不满。
所以用 `max` 兜底，保证任何可交易域下都能选到 `n_holdings` 只。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


WEIGHT_SCHEMES = ("equal", "score", "risk_parity")


def select_holdings(ranked: pd.Series, currently_held: list[str],
                    n_holdings: int, buffer_entry: float,
                    buffer_exit: float) -> list[str]:
    """按缓冲带规则选出持仓名单。

    `ranked` 是按分数**降序**排好的 `code -> score`，已限定在可买域内。
    返回按分数降序排列的名单（长度 ≤ `n_holdings`）。
    """
    if len(ranked) == 0:
        return []
    # 防御性排序：本函数全部逻辑都建立在「rank 越小越好」之上。
    # 调用方（引擎）恰好已经排过序，但把这个隐性契约留在注释里
    # 迟早会出事 —— 传入未排序的 Series 会静默产出错误的名单。
    ranked = ranked.sort_values(ascending=False)
    n = len(ranked)
    entry_cut = max(n_holdings, int(np.ceil(n * buffer_entry)))
    exit_cut = max(n_holdings, int(np.ceil(n * buffer_exit)))

    position = {code: i for i, code in enumerate(ranked.index)}

    # 通过保留阈值的持仓**优先占位**，再用新候选填剩余名额。
    #
    # 顺序不能反。「先取前 entry_cut 名、再按排名截断到 n_holdings」
    # 会让排名稍好的新候选把缓冲带保下来的老持仓挤掉 ——
    # 那样缓冲带就完全失效了（测试 `test_buffer_keeps_holding_inside_exit_band`
    # 就是钉这一条）。
    keep = [c for c in currently_held if position.get(c, n) < exit_cut]
    keep.sort(key=lambda c: position[c])
    keep = keep[:n_holdings]

    held_set = set(keep)
    fresh = [c for c in ranked.index[:entry_cut] if c not in held_set]
    slots = n_holdings - len(keep)
    return keep + fresh[:slots]


def target_weights(ranked: pd.Series, selected: list[str],
                   scheme: str = "equal",
                   volatility: pd.Series | None = None,
                   score_tilt: float = 1.0) -> pd.Series:
    """把名单变成权重（合计为 1）。

    * `equal`       —— 等权。唯一没有自由参数的方案
    * `score`       —— 按 `score - 入选者最低分 + 1` 加权
    * `risk_parity` —— 按 1/波动率 加权（需要 `volatility`）

    `score_tilt` 控制 `score` 方案的凸度：0 就退化成等权。
    """
    if not selected:
        return pd.Series(dtype=float)
    if scheme not in WEIGHT_SCHEMES:
        raise ValueError(f"未知权重方案 {scheme!r}，可选 {WEIGHT_SCHEMES}")

    if scheme == "equal":
        raw = pd.Series(1.0, index=selected)

    elif scheme == "score":
        scores = ranked.loc[selected]
        floor = scores.min()
        raw = (scores - floor + 1e-9) ** score_tilt
        if raw.sum() <= 0:
            raw = pd.Series(1.0, index=selected)

    else:  # risk_parity
        if volatility is None:
            raise ValueError("risk_parity 需要 volatility")
        vol = volatility.reindex(selected).astype(float)
        vol = vol.where(np.isfinite(vol) & (vol > 1e-8))
        inv = 1.0 / vol
        if not np.isfinite(inv).any() or inv.sum() <= 0:
            inv = pd.Series(1.0, index=selected)
        raw = inv.fillna(inv.mean())

    return raw / raw.sum()
