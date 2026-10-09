"""面板访问层。

## 为什么以长表为主

滚动因子（MA / 波动率 / 协方差）要在**按 code 分块**的序列上算，
长表是内存连续的最优形态。只有调仓日的因子快照才 pivot 成
`date × code` 的截面矩阵。

全量 2-D 化是没必要的：即使 4000 个交易日 × 5500 只 = 2200 万单元，
6 个字段的 float32 也就 530MB，而实际只在约 200 个调仓日做截面运算。

## `truncate` 是防未来函数的关键

`PanelData.truncate(asof)` 返回只含 `date <= asof` 的新面板。
引擎的无未来函数测试就是靠它：**把面板截断到 asof 跑出的目标权重，
必须与全量面板在同一 asof 跑出的目标权重逐元素相等**。
任何泄漏都会在这个测试里暴露。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# 面板的列。`raw_*` 指不复权原始价（用于涨跌停与成交），
# `adj_*` 指自合成后复权序列（用于一切指标）。
RAW_PRICE = ["open", "high", "low", "close"]
ADJ_PRICE = ["adj_open", "adj_high", "adj_low", "adj_close"]

REQUIRED = (["code", "date"] + RAW_PRICE + ADJ_PRICE
            + ["preclose", "volume", "amount", "limit_up", "limit_down",
               "tradable", "listed_days", "liquid", "isST",
               "cannot_buy", "cannot_sell"])


@dataclass
class PanelData:
    """一格 (code, date) 一行。切片返回新对象。

    **不是 frozen** —— 保留一个惰性构建的「日期 → 行号」索引。
    没有它，`cross_section` 每次都要对整表做一次 `long["date"] == d` 扫描；
    引擎在 1900 个交易日 × 每个日数次调用它，1400 万行乘以 1900 次
    就是 260 亿次行比较，回测根本跑不完。
    """

    long: pd.DataFrame
    dates: np.ndarray          # 升序的交易日字符串
    codes: np.ndarray          # 升序的代码
    _positions: dict | None = None      # 惰性缓存：date → 行号数组

    @classmethod
    def load(cls, path) -> "PanelData":
        frame = pd.read_parquet(path)
        return cls.from_frame(frame)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> "PanelData":
        # 面板文件里日期列叫 `time`（沿袭 QMT/桥接的口径），内部统一叫 `date`。
        # 不统一的话，下游每一处引用都要记住用的是哪个名字 —— 那种不一致
        # 迟早会以「KeyError」或更糟的「静默取错列」的形式爆出来。
        if "date" not in frame.columns and "time" in frame.columns:
            frame = frame.rename(columns={"time": "date"})

        missing = [c for c in REQUIRED if c not in frame.columns]
        if missing:
            raise ValueError(f"面板缺列: {missing}")
        frame = frame.sort_values(["code", "date"], kind="stable")
        frame = frame.reset_index(drop=True)
        return cls(
            long=frame,
            dates=np.sort(frame["date"].unique()),
            codes=np.sort(frame["code"].unique()),
        )

    # ─────────────────────── 切片 ───────────────────────

    def truncate(self, asof: str) -> "PanelData":
        """只保留 `date <= asof` 的行。无未来函数测试的基础。"""
        mask = self.long["date"] <= asof
        frame = self.long.loc[mask]
        return PanelData(
            long=frame.reset_index(drop=True),
            dates=self.dates[self.dates <= asof],
            codes=np.sort(frame["code"].unique()),
        )

    def between(self, start: str | None, end: str | None) -> "PanelData":
        mask = pd.Series(True, index=self.long.index)
        if start is not None:
            mask &= self.long["date"] >= start
        if end is not None:
            mask &= self.long["date"] <= end
        frame = self.long.loc[mask]
        return PanelData(
            long=frame.reset_index(drop=True),
            dates=self.dates[(self.dates >= (start or self.dates[0]))
                             & (self.dates <= (end or self.dates[-1]))],
            codes=np.sort(frame["code"].unique()),
        )

    # ─────────────────────── 截面访问 ───────────────────────

    def _date_positions(self) -> dict:
        """`date -> 行号数组`，只建一次。

        `groupby(...).indices` 给的就是行号数组，取值是 O(1)。
        """
        if self._positions is None:
            self._positions = self.long.groupby("date", sort=False).indices
        return self._positions

    def cross_section(self, date: str,
                      columns: list[str] | None = None) -> pd.DataFrame:
        """取某一交易日的横截面，索引为 code。O(该日行数)，不是 O(全表)。"""
        positions = self._date_positions().get(date)
        if positions is None:
            return pd.DataFrame(columns=(columns or self.long.columns),
                                index=pd.Index([], name="code"))
        rows = self.long.iloc[positions]
        if columns is not None:
            rows = rows[["code"] + [c for c in columns if c != "code"]]
        return rows.set_index("code")

    def field_matrix(self, field: str, dates=None) -> pd.DataFrame:
        """`date × code` 矩阵。只在调仓日调用。"""
        dates = self.dates if dates is None else np.asarray(dates)
        wanted = set(dates.tolist() if hasattr(dates, "tolist") else dates)
        rows = self.long[self.long["date"].isin(wanted)]
        return rows.pivot(index="date", columns="code", values=field)

    # ─────────────────────── 可交易域 ───────────────────────

    def eligible(self, date: str, min_listed_days: int = 250,
                 require_liquid: bool = True,
                 max_price: float | None = None) -> pd.Index:
        """某一交易日**可买**的股票集合。

        过滤链：上市够久 → 有流动性 → 当日可交易 → 非 ST →
        开盘未封涨停 → （可选）股价不超过整手可买上限。

        `max_price` 是 100 万 ÷ 持仓数 ÷ 100 的整手约束 ——
        组合买不起的股票就该从可买域里剔掉，而不是让它在信号里
        占个位置然后下单失败。
        """
        rows = self.cross_section(date)
        mask = (rows["listed_days"] >= min_listed_days) & rows["tradable"]
        if require_liquid:
            mask &= rows["liquid"]
        mask &= rows["isST"] != "1"
        mask &= ~rows["cannot_buy"]
        if max_price is not None:
            mask &= rows["close"] <= max_price
        return rows.index[mask]

    def sellable(self, date: str) -> pd.Index:
        """某一交易日**可卖**的股票集合（停牌 / 封跌停都卖不掉）。"""
        rows = self.cross_section(date)
        return rows.index[rows["tradable"] & ~rows["cannot_sell"]]

    # ─────────────────────── 工具 ───────────────────────

    def next_date(self, date: str) -> str | None:
        """下一个交易日。T 日算信号、T+1 执行，就靠这个函数。"""
        later = self.dates[self.dates > date]
        return later[0] if len(later) else None

    def rebalance_dates(self, rule: str, start: str | None = None,
                        end: str | None = None) -> list[str]:
        """调仓**信号日**列表。

        * `"M"` —— 每个自然月最后一个交易日（实盘脚本用的规则）
        * 形如 `"20"` —— 每 20 个交易日一个

        回测与实盘的调仓日必须来自同一个函数，否则两边会慢慢错开。
        """
        dates = self.dates
        if start is not None:
            dates = dates[dates >= start]
        if end is not None:
            dates = dates[dates <= end]
        if len(dates) == 0:
            return []

        if rule == "M":
            periods = pd.Series(dates).str[:6]
            last = pd.Series(dates).groupby(periods).max()
            return last.tolist()

        step = int(rule)
        return list(dates[::step])
