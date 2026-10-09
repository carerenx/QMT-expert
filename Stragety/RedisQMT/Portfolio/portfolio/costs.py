"""A 股交易成本模型 —— 全仓库**唯一真源**，且随日期变化。

## 为什么必须新建，而不是复用 okh/trade_mgr.py

`backtest/okh/trade_mgr.py` 有两处是错的，而我们的样本从 2011 年起，
大部分区间都会踩中：

1. **过户费**（`trade_mgr.py:115`）：注释写「仅沪市」、费率 `0.00001`。
   实际是 2015-08-01 起**沪深两市都收**，且费率先是 `0.00002`，
   2022-04-29 才降到 `0.00001`。用它的模型，2011–2022 年的深市交易
   会少算过户费，且沪市费率整体偏低一半。
2. **滑点**（`trade_mgr.py:83`）：ratio 模式下**除以 2**，而
   `backtest/config.py` 的 `SLIPPAGE` 是全额。两处语义不一致，
   不知道该信哪个。

所以本模块独立实现，并用 `tests/test_portfolio_costs.py` 把与 okh 的
差异**记录成测试**，而不是留成隐患。

## 费率口径

| 项目 | 规则 |
|---|---|
| 佣金 | 双向 `0.00025`，**每笔最低 5 元** |
| 印花税 | **仅卖出**。2023-08-28 起 `0.0005`，之前 `0.001` |
| 过户费 | 沪深**双向**按成交金额。2022-04-29 起 `0.00001`，之前 `0.00002`；2015-08-01 前仅沪市收 |
| 滑点 | 买 `price × (1+s)`，卖 `price × (1-s)`，基准 5bp/单边 |
| 流量费 | 默认 0（多数券商已不收），留参数做敏感度 |
| 容量 | 单笔 ≤ 当日成交额 × `max_participation` |

单边合计（5bp 滑点）：卖出约 12.6bp、买入约 7.6bp → **往返 100% 换手 ≈ 20bp**。
"""

from __future__ import annotations

from dataclasses import dataclass


LOT = 100

STAMP_TAX_CUT = "20230828"       # 印花税减半生效日
TRANSFER_FEE_CUT = "20220429"    # 过户费降至 0.001% 生效日
TRANSFER_FEE_UNIFY = "20150801"  # 深市开始收过户费的日子


def round_lot(shares: float) -> int:
    """向下取整到整手（100 股）。A 股买入必须是整手。"""
    if shares <= 0:
        return 0
    return int(shares // LOT) * LOT


def stamp_tax_rate(date: str) -> float:
    """印花税率（仅卖出）。`date` 形如 `20230828`。"""
    return 0.0005 if date >= STAMP_TAX_CUT else 0.001


def transfer_fee_rate(code: str, date: str) -> float:
    """过户费率（双向，按成交金额）。

    2015-08-01 之前只有沪市收 —— 用后缀判断。
    """
    is_shanghai = code.endswith(".SH")
    if date < TRANSFER_FEE_UNIFY and not is_shanghai:
        return 0.0
    return 0.00001 if date >= TRANSFER_FEE_CUT else 0.00002


@dataclass(frozen=True)
class CostModel:
    """一次调仓里所有摩擦成本的来源。"""

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    slippage_bps: float = 5.0
    flow_fee: float = 0.0
    max_participation: float = 0.01

    @property
    def slippage(self) -> float:
        return self.slippage_bps / 10_000.0

    def fill_price(self, price: float, side: str) -> float:
        """滑点后的成交价。买贵卖便宜。"""
        if side == "buy":
            return price * (1.0 + self.slippage)
        if side == "sell":
            return price * (1.0 - self.slippage)
        raise ValueError(f"side 只能是 buy/sell，收到 {side!r}")

    def total_fee(self, code: str, date: str, side: str, price: float,
                  shares: int) -> float:
        """一笔成交的全部费用（不含滑点 —— 滑点已体现在成交价里）。"""
        if shares <= 0:
            return 0.0
        amount = price * shares

        commission = max(amount * self.commission_rate, self.min_commission)
        transfer = amount * transfer_fee_rate(code, date)
        stamp = amount * stamp_tax_rate(date) if side == "sell" else 0.0
        return commission + transfer + stamp + self.flow_fee

    def capacity(self, shares: int, price: float, day_amount: float) -> int:
        """按当日成交额的参与率上限，砍到整手。

        `day_amount × max_participation` 是**金额**，要除以价格才能和
        `shares` 比较 —— 少了这一步就是拿元当股，上限形同虚设。
        """
        if day_amount <= 0 or price <= 0:
            return 0
        cap_shares = day_amount * self.max_participation / price
        return min(round_lot(shares), round_lot(cap_shares))

    def round_trip_bps(self, code: str = "600000.SH",
                       date: str = "20260101") -> float:
        """100% 换手走一个来回的成本（bp）。写报告时直接用这个数。"""
        notional = 1_000_000.0
        buy = (self.fill_price(1.0, "buy") - 1.0) * notional
        buy += self.total_fee(code, date, "buy", 1.0, int(notional))
        sell = (1.0 - self.fill_price(1.0, "sell")) * notional
        sell += self.total_fee(code, date, "sell", 1.0, int(notional))
        return (buy + sell) / notional * 10_000.0
