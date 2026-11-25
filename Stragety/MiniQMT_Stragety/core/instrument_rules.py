# -*- coding: utf-8 -*-
"""Listed-stock/fund quote grids; broker sellable quantities stay authoritative.

SZSE fund rules: https://investor.szse.cn/knowledge/fund/trade/t20171113_538865.html
Commodity ETF same-day auction sales:
https://investor.szse.cn/knowledge/fund/trade/t20190306_565341.html
Only 159981's commodity classification is explicitly configured here; a fund
code/price grid alone never grants same-day selling permission.
"""
from dataclasses import dataclass
import math
from typing import Optional


@dataclass(frozen=True)
class InstrumentRules:
    kind: str
    price_tick: float
    lot_size: int
    same_day_auction_sell: Optional[bool] = None

    @property
    def price_decimals(self):
        return price_decimals(self.price_tick)


def rules_for_symbol(symbol):
    parts = str(symbol).upper().split('.')
    code = parts[0]
    exchange = parts[1] if len(parts) == 2 else ''
    listed_fund = ((exchange == 'SH' and code.startswith('5')) or
                   (exchange == 'SZ' and code.startswith(('15', '16'))))
    if listed_fund:
        same_day = True if code == '159981' and exchange == 'SZ' else None
        return InstrumentRules('FUND', 0.001, 100, same_day)
    lot = 200 if code.startswith(('688', '689')) else 100
    return InstrumentRules('STOCK', 0.01, lot, False)


def price_decimals(price_tick):
    if price_tick == 0.01:
        return 2
    if price_tick == 0.001:
        return 3
    raise ValueError('supported price grids are 0.01 (stock) and 0.001 (fund)')


def round_price(value, price_tick=0.01, direction='nearest'):
    digits = price_decimals(price_tick)
    amount = float(value)
    if not math.isfinite(amount):
        raise ValueError('price must be finite')
    if direction == 'nearest':
        # Preserve legacy Python round(..., 2) stock behavior exactly.
        return round(amount, digits)
    if direction == 'ceil':
        return round(math.ceil(amount / price_tick - 1e-9) * price_tick, digits)
    if direction == 'floor':
        return round(math.floor(amount / price_tick + 1e-9) * price_tick, digits)
    raise ValueError('unsupported price rounding direction: ' + str(direction))
