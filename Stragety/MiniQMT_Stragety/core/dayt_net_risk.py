"""Pure, explicit cost estimates and intraday risk/attribution for DayT.

Fees are estimates until reconciled to broker statements. Stock sell stamp tax
uses the 2023-08-28 reduction; ETF transfer trades are stamp-tax exempt.
https://shanghai.chinatax.gov.cn/zcfw/zcfgk/yhs/202308/t468451.html
No broker calls, order placement, cycle recovery or persistence in this module.
"""
from dataclasses import dataclass
from datetime import datetime, time
import math


@dataclass(frozen=True)
class FeeEstimate:
    commission_rate: float = 0.00025
    minimum_commission: float = 5.0
    stock_sell_tax: float = 0.0005
    stock_transfer_rate: float = 0.00001
    impact_per_side: float = 0.001

    def fee(self, price, shares, side, fund=False):
        if side not in ('BUY', 'SELL'):
            raise ValueError('side must be BUY or SELL')
        if not math.isfinite(price) or price <= 0 or shares < 0:
            raise ValueError('invalid execution price/quantity')
        if not shares:
            return 0.0
        value = price * shares
        commission = max(self.minimum_commission, value * self.commission_rate)
        transfer = 0.0 if fund else value * self.stock_transfer_rate
        tax = value * self.stock_sell_tax if side == 'SELL' and not fund else 0.0
        return commission + transfer + tax

    def round_trip(self, entry, exit_price, shares, side, fund=False):
        other = 'BUY' if side == 'SELL' else 'SELL'
        fees = self.fee(entry, shares, side, fund) + self.fee(exit_price, shares, other, fund)
        return fees + (entry + exit_price) * shares * self.impact_per_side


def trading_seconds_between(start, end):
    """Same-day Shanghai continuous-session time, excluding lunch/out of hours.

    Cross-day intervals deliberately return zero: this module does not add the
    cycle carry/recovery mechanism excluded by the user.
    """
    if end <= start or start.date() != end.date():
        return 0.0
    seconds = 0.0
    for opening, closing in ((time(9, 30), time(11, 30)), (time(13), time(15))):
        left = max(start, datetime.combine(start.date(), opening))
        right = min(end, datetime.combine(end.date(), closing))
        seconds += max(0.0, (right - left).total_seconds())
    return seconds


def risk_capped_shares(planned, lot, price, atr_pct, equity, risk_fraction,
                       adverse_atr, fees, side, fund=False):
    """Largest whole lot inside the incremental T risk budget, never force a lot.

    This is a modeled budget, not a guaranteed loss bound (gaps/nonfills exist).
    """
    values = (price, atr_pct, equity, risk_fraction, adverse_atr)
    if lot <= 0 or not all(math.isfinite(v) and v > 0 for v in values):
        return 0
    if risk_fraction > 1 or atr_pct * adverse_atr >= 1:
        return 0
    budget = equity * risk_fraction
    distance = price * atr_pct * adverse_atr
    exit_price = price + distance if side == 'SELL' else price - distance
    lower = 0
    upper = max(0, int(planned)) // lot
    while lower < upper:
        middle = (lower + upper + 1) // 2
        shares = middle * lot
        loss = shares * distance + fees.round_trip(price, exit_price, shares, side, fund)
        if loss <= budget:
            lower = middle
        else:
            upper = middle - 1
    return lower * lot


def net_space_check(entry, exit_price, shares, side, fees, fund=False,
                    cost_multiple=1.5, minimum_net_fraction=0.002):
    if not all(math.isfinite(v) and v > 0 for v in (entry, exit_price, shares)):
        return {'allowed': False, 'reason': 'invalid price/quantity'}
    gross = shares * ((entry - exit_price) if side == 'SELL' else (exit_price - entry))
    cost = fees.round_trip(entry, exit_price, shares, side, fund)
    required = cost * cost_multiple + entry * shares * minimum_net_fraction
    return {'allowed': gross >= required, 'gross_space': gross,
            'stress_cost': cost, 'required_space': required,
            'reason': 'space clears stress costs' if gross >= required else 'space below stress cost hurdle'}


class NetAttribution:
    """Own T fills vs keeping those shares; reporting only, never manages orders.

    Session-only; excludes dividends, outside trades and funding adjustments.
    Actual fill prices already include realized slippage: do not subtract modeled
    impact again here. Impact is used only for pre-trade stress admission.
    """
    def __init__(self, fund=False, fees=None):
        self.fund = fund
        self.fees = fees or FeeEstimate()
        self.orders = set()
        self.cash_flow = 0.0
        self.delta_shares = 0
        self.estimated_fees = 0.0
        self.realized_gross = 0.0

    def record(self, order_id, signed_shares, price, realized_gross=0.0):
        if str(order_id) in self.orders:
            raise ValueError('duplicate attribution order')
        if not signed_shares or not math.isfinite(price) or price <= 0:
            raise ValueError('invalid attribution fill')
        side = 'BUY' if signed_shares > 0 else 'SELL'
        self.estimated_fees += self.fees.fee(price, abs(signed_shares), side, self.fund)
        self.cash_flow -= signed_shares * price
        self.delta_shares += signed_shares
        self.realized_gross += realized_gross
        self.orders.add(str(order_id))

    def mark(self, price):
        if not math.isfinite(price) or price <= 0:
            raise ValueError('invalid mark')
        gross = self.cash_flow + self.delta_shares * price
        return {'realized_gross': self.realized_gross,
                'inventory_component': gross - self.realized_gross,
                'incremental_gross_vs_hold': gross,
                'estimated_fees': self.estimated_fees,
                'incremental_net_estimate': gross - self.estimated_fees,
                'delta_shares': self.delta_shares}
