"""账户与撮合：T+1、涨跌停不可成交、整手、现金约束。

## 时序契约

调用方（`engine.py`）必须遵守：**T 日收盘算目标，`rebalance` 在 T+1 用
T+1 的开盘价调用**。本模块不做时序判断，它只按传入的价格和可成交集合执行 ——
时序是调用方的责任，并且由 `test_no_lookahead` 兜底。

## 为什么先卖后买

A 股资金 T+0、证券 T+1：卖出所得当日即可用于买入，所以**先卖后买**
能最大化资金利用率。反过来做会因为现金不足而少买。

## 卖不掉 / 买不到怎么办

不假装成交。卖不掉的仓位继续留在账上（占着权重，下期再试）；
买不到的就不买，形成现金拖累。这两件事都要**如实记录**，
因为它们正是真实策略与「假设完美成交」的回测之间的差。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from Stragety.RedisQMT.Portfolio.portfolio.costs import CostModel, round_lot


@dataclass
class Fill:
    date: str
    code: str
    side: str            # buy / sell
    shares: int
    price: float         # 含滑点的成交价
    fee: float
    reason: str = ""


@dataclass
class Account:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    locked: dict[str, int] = field(default_factory=dict)

    def equity(self, prices: dict[str, float]) -> float:
        """总权益 = 现金 + 持仓市值。

        停牌股用最后可得价（调用方传入的 `prices` 应已做 ffill），
        这会低估停牌期的真实波动与回撤 —— 是已知的口径限制。
        """
        value = self.cash
        for code, shares in self.positions.items():
            if shares:
                value += shares * prices.get(code, 0.0)
        return value

    def market_value(self, code: str, prices: dict[str, float]) -> float:
        return self.positions.get(code, 0) * prices.get(code, 0.0)

    def new_day(self) -> None:
        """新交易日：昨天买的今天可以卖了。"""
        for code in list(self.locked):
            shares = self.locked[code] - self.positions.get(code, 0)
            if shares <= 0:
                self.locked.pop(code, None)
            else:
                self.locked[code] = shares


@dataclass
class RebalanceReport:
    date: str
    fills: list[Fill]
    turnover: float          # 成交金额 / 期初权益
    fees: float
    blocked_sells: dict[str, str]   # code -> 原因
    blocked_buys: dict[str, str]
    cash_weight: float
    gross_weight: float


class Broker:
    def __init__(self, cash: float, costs: CostModel | None = None):
        self.account = Account(cash=cash)
        self.costs = costs or CostModel()

    # ─────────────────────── 查询 ───────────────────────

    def equity(self, prices: dict[str, float]) -> float:
        return self.account.equity(prices)

    def shares_of(self, code: str) -> int:
        return self.account.positions.get(code, 0)

    def sellable_shares(self, code: str) -> int:
        """可卖股数 = 持仓 − 当日买入的锁定部分。"""
        return self.account.positions.get(code, 0) - self.account.locked.get(code, 0)

    # ─────────────────────── 单笔成交 ───────────────────────

    def _sell(self, date: str, code: str, shares: int, price: float,
              reason: str = "") -> Fill | None:
        shares = round_lot(shares)
        if shares <= 0:
            return None
        fill_price = self.costs.fill_price(price, "sell")
        fee = self.costs.total_fee(code, date, "sell", fill_price, shares)
        proceeds = fill_price * shares - fee
        self.account.cash += proceeds
        remaining = self.account.positions[code] - shares
        if remaining > 0:
            self.account.positions[code] = remaining
        else:
            self.account.positions.pop(code, None)
        return Fill(date, code, "sell", shares, fill_price, fee, reason)

    def _buy(self, date: str, code: str, shares: int, price: float,
             day_amount: float, reason: str = "") -> Fill | None:
        shares = round_lot(shares)
        shares = self.costs.capacity(shares, price, day_amount)
        if shares <= 0:
            return None

        fill_price = self.costs.fill_price(price, "buy")
        # 现金约束：费用也要留出来。用二分不必要 —— 先按名义价估，
        # 不够就按可用现金等比缩量，再砍整手。
        need = fill_price * shares
        fee = self.costs.total_fee(code, date, "buy", fill_price, shares)
        if need + fee > self.account.cash:
            affordable = self.account.cash / (fill_price * 1.001)
            shares = round_lot(affordable)
            shares = self.costs.capacity(shares, price, day_amount)
            if shares <= 0:
                return None
            need = fill_price * shares
            fee = self.costs.total_fee(code, date, "buy", fill_price, shares)
            if need + fee > self.account.cash:
                shares = round_lot(shares - 100)
                if shares <= 0:
                    return None
                need = fill_price * shares
                fee = self.costs.total_fee(code, date, "buy", fill_price, shares)

        self.account.cash -= need + fee
        self.account.positions[code] = self.account.positions.get(code, 0) + shares
        # 当日买入的部分锁定，T+1 解锁
        self.account.locked[code] = self.account.locked.get(code, 0) + shares
        return Fill(date, code, "buy", shares, fill_price, fee, reason)

    # ─────────────────────── 调仓 ───────────────────────

    def rebalance(self, date: str, target_value: dict[str, float],
                  open_px: dict[str, float], close_px: dict[str, float],
                  tradable: set[str], sellable: set[str], buyable: set[str],
                  day_amount: dict[str, float],
                  liquidate: set[str] | None = None) -> RebalanceReport:
        """按目标市值调仓。`liquidate` 里的股票无条件清仓（如退市）。"""
        start_equity = self.account.equity(close_px)
        fills: list[Fill] = []
        blocked_sells: dict[str, str] = {}
        blocked_buys: dict[str, str] = {}
        liquidate = liquidate or set()

        # ── 卖出（含无条件清仓）──
        targets = dict(target_value)
        for code in liquidate:
            targets[code] = 0.0

        sells: list[tuple[str, int]] = []
        for code, shares in list(self.account.positions.items()):
            if not shares:
                continue
            price = open_px.get(code)
            if price is None or price <= 0:
                blocked_sells[code] = "无价格"
                continue
            if code not in sellable:
                blocked_sells[code] = "停牌或封跌停"
                continue
            free = self.sellable_shares(code)
            if free <= 0:
                blocked_sells[code] = "T+1 锁定"
                continue
            want = round_lot((targets.get(code, 0.0)) / price)
            delta = shares - want
            if delta <= 0:
                continue
            sells.append((code, min(delta, free)))

        for code, shares in sells:
            fill = self._sell(date, code, shares, open_px[code])
            if fill:
                fills.append(fill)

        # ── 买入 ──
        buys: list[tuple[str, int]] = []
        for code, value in targets.items():
            if value <= 0:
                continue
            price = open_px.get(code)
            if price is None or price <= 0:
                blocked_buys[code] = "无价格"
                continue
            if code not in buyable:
                blocked_buys[code] = "停牌 / 封涨停 / ST"
                continue
            held = self.account.positions.get(code, 0)
            if held:
                can_buy = self.sellable_shares(code)      # 锁定部分看不到，不下单
                want = round_lot(value / price)
                delta = want - can_buy
            else:
                delta = round_lot(value / price)
            if delta <= 0:
                continue
            buys.append((code, delta))

        # 按目标金额从大到小买，现金不够时优先保住大仓位
        buys.sort(key=lambda item: -target_value.get(item[0], 0.0))
        for code, shares in buys:
            fill = self._buy(date, code, shares, open_px[code],
                             day_amount.get(code, 0.0))
            if fill:
                fills.append(fill)
            else:
                blocked_buys.setdefault(code, "资金不足或容量限制")

        # ── 统计 ──
        traded = sum(f.price * f.shares for f in fills)
        fees = sum(f.fee for f in fills)
        gross = sum(self.account.market_value(c, close_px)
                    for c in self.account.positions)
        equity = self.account.equity(close_px)
        return RebalanceReport(
            date=date,
            fills=fills,
            turnover=traded / start_equity if start_equity > 0 else 0.0,
            fees=fees,
            blocked_sells=blocked_sells,
            blocked_buys=blocked_buys,
            cash_weight=self.account.cash / equity if equity > 0 else 1.0,
            gross_weight=gross / equity if equity > 0 else 0.0,
        )
