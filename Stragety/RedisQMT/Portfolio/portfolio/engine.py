"""组合回测引擎：选股 → 目标权重 → 大盘择时 → T+1 执行 → 组合净值。

## 时序契约（无未来函数的来源）

    T 日收盘后    算可交易域、因子、alpha、目标权重、总仓位
                  —— 所有输入都只来自 date <= T 的数据
    T+1 开盘      按 T+1 的**开盘价**成交

**「T 日收盘成交」不是可实现假设**：T 日收盘价在 15:00 前不可知。
实盘脚本在 T 日收盘后运行、T+1 开盘下单，回测用同一套时序，
所以不需要两个模型。

代价是隔夜跳空被计入 —— 这正是策略的真实成本，对反转类因子尤其致命
（A 股短期反转有很大一部分在开盘那一笔完成）。

引擎提供 `execution="close"` 作为**诊断开关**，用它跑出「T 日收盘成交」
的版本，两个版本之差就是隔夜跳空的代价。若某因子的超额主要来自这个差，
它不可交易。

## 这个设计为什么能防未来函数

`compute_targets(panel, signal_date)` 是纯函数：给它一个只含
`date <= signal_date` 的面板，和给它一个含全部历史的面板，
在同一 `signal_date` 上必须返回**逐元素相同**的目标权重。
`tests/test_portfolio_no_lookahead.py` 就是断言这一条。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from Stragety.RedisQMT.Portfolio.portfolio.broker import Broker
from Stragety.RedisQMT.Portfolio.portfolio.construct import select_holdings, target_weights
from Stragety.RedisQMT.Portfolio.portfolio.costs import CostModel


@dataclass
class BacktestResult:
    equity: pd.Series
    weights: pd.DataFrame                     # 每日实际权重
    targets: pd.DataFrame                     # 调仓信号日目标权重
    trades: pd.DataFrame
    rebalances: pd.DataFrame
    diagnostics: dict = field(default_factory=dict)

    @property
    def returns(self) -> pd.Series:
        return self.equity.pct_change().dropna()


@dataclass
class Target:
    """某一个信号日算出来的目标（尚未执行）。"""
    signal_date: str
    exec_date: str
    weights: pd.Series       # code -> 权重（已含总仓位缩放）
    gross_exposure: float
    n_selected: int
    n_eligible: int


class PortfolioEngine:
    def __init__(self, panel, initial_capital: float = 1_000_000.0,
                 costs: CostModel | None = None,
                 n_holdings: int = 30,
                 rebalance_rule: str = "20",
                 buffer_entry: float = 0.20,
                 buffer_exit: float = 0.40,
                 weight_scheme: str = "equal",
                 min_listed_days: int = 250,
                 require_liquid: bool = True,
                 enforce_lot_price: bool = True,
                 execution: str = "open",
                 delist_haircut: float = 0.0):
        if execution not in ("open", "close"):
            raise ValueError("execution 只能是 open（T+1 开盘）或 close（诊断用）")
        self.panel = panel
        self.initial_capital = initial_capital
        self.costs = costs or CostModel()
        self.n_holdings = n_holdings
        self.rebalance_rule = rebalance_rule
        self.buffer_entry = buffer_entry
        self.buffer_exit = buffer_exit
        self.weight_scheme = weight_scheme
        self.min_listed_days = min_listed_days
        self.require_liquid = require_liquid
        self.enforce_lot_price = enforce_lot_price
        self.execution = execution
        self.delist_haircut = delist_haircut

    # ─────────────────────── 信号侧（纯函数）───────────────────────

    def max_affordable_price(self, equity: float) -> float | None:
        """整手约束下的股价上限。

        100 万 ÷ 30 只 = 3.3 万/只，100 股整手 → 股价 > 330 元就买不了。
        超过这个价的股票应该从可买域里剔掉，而不是让它在信号里占位、
        下单时才失败。
        """
        if not self.enforce_lot_price:
            return None
        return equity / self.n_holdings / 100.0

    def compute_targets(self, panel, signal_date: str, alpha: pd.DataFrame,
                        current_weights: dict[str, float],
                        equity: float,
                        gross_exposure: float = 1.0) -> Target:
        """信号日的目标权重。**只用 `panel` 里 date <= signal_date 的数据。**"""
        eligible = panel.eligible(
            signal_date,
            min_listed_days=self.min_listed_days,
            require_liquid=self.require_liquid,
            max_price=self.max_affordable_price(equity),
        )

        day_alpha = alpha[alpha["date"] == signal_date]
        day_alpha = day_alpha.set_index("code")["alpha"]
        day_alpha = day_alpha.reindex(eligible).dropna()
        ranked = day_alpha.sort_values(ascending=False)

        held = [c for c in current_weights if current_weights[c] > 0]
        selected = select_holdings(ranked, held, self.n_holdings,
                                   self.buffer_entry, self.buffer_exit)
        weights = target_weights(ranked, selected, self.weight_scheme)
        weights = weights * gross_exposure

        exec_date = panel.next_date(signal_date)
        return Target(signal_date=signal_date, exec_date=exec_date,
                      weights=weights, gross_exposure=gross_exposure,
                      n_selected=len(selected), n_eligible=len(eligible))

    # ─────────────────────── 回测主循环 ───────────────────────

    def run(self, alpha_fn, timing_fn=None, start: str | None = None,
            end: str | None = None) -> BacktestResult:
        panel = self.panel
        dates = [d for d in panel.dates
                 if (start is None or d >= start) and (end is None or d <= end)]
        if not dates:
            raise ValueError("日期区间为空")

        alpha = alpha_fn(panel)
        if not isinstance(alpha, pd.DataFrame) or "alpha" not in alpha.columns:
            raise ValueError("alpha_fn 必须返回含 'alpha' 列的长表")

        gross = timing_fn(panel) if timing_fn is not None else None

        signal_dates = panel.rebalance_dates(self.rebalance_rule,
                                             start=dates[0], end=dates[-1])
        signal_set = set(signal_dates)
        date_set = set(dates)

        # 开盘价**不填充** —— 停牌日没有开盘价，就不该能成交。
        open_mx = panel.field_matrix("open", dates).astype("float32")
        # 收盘价**必须前向填充** —— 面板里停牌日缺行（桥接用 fill_data=False），
        # 不填充的话持仓股停牌当天市值会变成 0，净值凭空跳水。
        # 代价是停牌期按冻结价盯市，低估了真实的波动与回撤 —— 已知口径限制。
        close_mx = (panel.field_matrix("close", dates)
                    .astype("float32").ffill())

        broker = Broker(self.initial_capital, self.costs)
        equity_curve: dict[str, float] = {}
        weight_rows: dict[str, pd.Series] = {}
        trade_rows: list[dict] = []
        rebal_rows: list[dict] = []
        target_rows: list[dict] = []

        # 只取一次横截面。写成 `cross_section(d).index[cross_section(d)[...]]`
        # 会对每个交易日算两遍 —— 1900 个交易日就是双倍开销。
        tradable_by_date = {}
        for d in dates:
            section = panel.cross_section(d)
            tradable_by_date[d] = set(section.index[section["tradable"]])

        # 每只股票的最后一个交易日，用于识别「持有期内退市」。
        # 逐个持仓在 `panel.long` 上做布尔扫描是 O(全表) ——
        # 30 只持仓 × 96 次调仓 × 1400 万行 = 400 亿次行比较。
        last_date_by_code = panel.long.groupby("code")["date"].max().to_dict()

        prev_date: str | None = None

        for date in dates:
            broker.account.new_day()

            # 昨天是信号日 → 今天是执行日
            if prev_date is not None and prev_date in signal_set:
                held = {c: s for c, s in broker.account.positions.items() if s}
                equity_now = broker.equity(self._price_dict(close_mx, prev_date,
                                                            held))
                day_alpha = alpha[alpha["date"] == prev_date]
                # 注意：用**信号日**的数据算目标，但用**执行日**的价格成交
                target = self.compute_targets(
                    panel, prev_date, alpha,
                    current_weights=self._weights(broker, close_mx, prev_date),
                    equity=equity_now,
                    gross_exposure=self._gross(gross, prev_date),
                )
                target_rows.append({
                    "date": prev_date, "exec_date": date,
                    "gross_exposure": target.gross_exposure,
                    "n_selected": target.n_selected,
                    "n_eligible": target.n_eligible,
                })

                if self.execution == "open":
                    report = self._execute(broker, target, date, open_mx,
                                           close_mx, panel, tradable_by_date,
                                           last_date_by_code)
                else:
                    report = self._execute(broker, target, date, close_mx,
                                           close_mx, panel, tradable_by_date,
                                           last_date_by_code)
                rebal_rows.append(report)
                for fill in report["fills"]:
                    trade_rows.append(fill)

            # 盯市
            held = {c: s for c, s in broker.account.positions.items() if s}
            prices = self._price_dict(close_mx, date, held)
            equity = broker.equity(prices)
            equity_curve[date] = equity

            total = equity if equity > 0 else 1.0
            weight_rows[date] = pd.Series(
                {c: s * prices.get(c, 0.0) / total for c, s in held.items()})
            prev_date = date

        equity = pd.Series(equity_curve).sort_index()
        # 持仓名单每期不同 → 转置后是 object 列，先显式转 float 再填 0，
        # 否则 pandas 会对 fillna 触发 downcasting 的 FutureWarning
        weights = pd.DataFrame(weight_rows).T
        weights = weights.astype("float64").fillna(0.0).sort_index()
        targets = pd.DataFrame(target_rows)
        trades = pd.DataFrame(trade_rows)
        rebalances = pd.DataFrame(rebal_rows)

        return BacktestResult(
            equity=equity,
            weights=weights,
            targets=targets,
            trades=trades,
            rebalances=rebalances,
            diagnostics=self._diagnostics(equity, targets, rebalances, panel),
        )

    # ─────────────────────── 内部工具 ───────────────────────

    @staticmethod
    def _price_dict(close_mx: pd.DataFrame, date: str,
                    codes) -> dict[str, float]:
        if date not in close_mx.index:
            return {}
        row = close_mx.loc[date]
        out = {}
        for code in codes:
            value = row.get(code, np.nan)
            if np.isfinite(value):
                out[code] = float(value)
        return out

    @staticmethod
    def _gross(gross: pd.Series | None, date: str) -> float:
        if gross is None:
            return 1.0
        value = gross.get(date, np.nan)
        if not np.isfinite(value):
            return 0.0
        return float(np.clip(value, 0.0, 1.0))

    @staticmethod
    def _weights(broker: Broker, close_mx: pd.DataFrame,
                 date: str) -> dict[str, float]:
        held = {c: s for c, s in broker.account.positions.items() if s}
        prices = PortfolioEngine._price_dict(close_mx, date, held)
        equity = broker.equity(prices)
        if equity <= 0:
            return {c: 0.0 for c in held}
        return {c: s * prices.get(c, 0.0) / equity for c, s in held.items()}

    def _execute(self, broker: Broker, target: Target, exec_date: str,
                 price_mx: pd.DataFrame, close_mx: pd.DataFrame,
                 panel, tradable_by_date: dict, last_date_by_code: dict) -> dict:
        """在 `exec_date` 按目标权重调仓。"""
        codes = list(target.weights.index) + list(broker.account.positions)
        # 退市处理：持仓里在 panel 中再无后续数据的股票，强制清仓
        delisted = set()
        for code in broker.account.positions:
            last = last_date_by_code.get(code)
            if last is not None and last < exec_date:
                delisted.add(code)

        price_row = price_mx.loc[exec_date] if exec_date in price_mx.index \
            else pd.Series(dtype=float)
        open_px: dict[str, float] = {}
        close_px: dict[str, float] = {}
        for code in set(codes) | delisted:
            value = price_row.get(code, np.nan)
            if np.isfinite(value) and value > 0:
                px = float(value)
                if code in delisted:
                    px *= (1.0 - self.delist_haircut)
                open_px[code] = px
            cv = close_mx.loc[exec_date].get(code, np.nan) \
                if exec_date in close_mx.index else np.nan
            if np.isfinite(cv) and cv > 0:
                close_px[code] = float(cv)

        tradable = tradable_by_date.get(exec_date, set())
        sec = panel.cross_section(exec_date)
        sellable = set(sec.index[sec["tradable"] & ~sec["cannot_sell"]])
        buyable = set(sec.index[sec["tradable"] & ~sec["cannot_buy"]
                                & (sec["isST"] != "1")])
        amounts = sec["amount"].to_dict() if "amount" in sec.columns else {}
        for code in delisted:
            sellable.add(code)          # 退市强制卖出，不受涨跌停约束

        equity = broker.equity(close_px) or broker.equity(open_px)
        target_value = {c: w * equity for c, w in target.weights.items()}

        report = broker.rebalance(
            date=exec_date, target_value=target_value,
            open_px=open_px, close_px=close_px,
            tradable=tradable, sellable=sellable, buyable=buyable,
            day_amount=amounts, liquidate=delisted)

        return {
            "date": exec_date,
            "signal_date": target.signal_date,
            "turnover": report.turnover,
            "fees": report.fees,
            "n_fills": len(report.fills),
            "n_blocked_sells": len(report.blocked_sells),
            "n_blocked_buys": len(report.blocked_buys),
            "cash_weight": report.cash_weight,
            "gross_weight": report.gross_weight,
            "gross_exposure": target.gross_exposure,
            "fills": [
                {"date": f.date, "code": f.code, "side": f.side,
                 "shares": f.shares, "price": f.price, "fee": f.fee}
                for f in report.fills
            ],
        }

    @staticmethod
    def _diagnostics(equity: pd.Series, targets: pd.DataFrame,
                     rebalances: pd.DataFrame, panel) -> dict:
        out = {
            "n_days": len(equity),
            "n_rebalances": len(rebalances),
        }
        if len(rebalances):
            out["avg_turnover"] = float(rebalances["turnover"].mean())
            out["avg_fees"] = float(rebalances["fees"].mean())
            out["total_fees"] = float(rebalances["fees"].sum())
            out["avg_gross_exposure"] = float(
                rebalances["gross_exposure"].mean())
            out["blocked_sells"] = int(rebalances["n_blocked_sells"].sum())
            out["blocked_buys"] = int(rebalances["n_blocked_buys"].sum())
        if len(equity) > 1:
            years = (pd.Timestamp(equity.index[-1])
                     - pd.Timestamp(equity.index[0])).days / 365.25
            if years > 0:
                out["total_return"] = float(equity.iloc[-1] / equity.iloc[0] - 1)
                out["years"] = years
        return out
