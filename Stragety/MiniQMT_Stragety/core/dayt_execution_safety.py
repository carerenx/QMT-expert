"""Pure execution constraints shared by v0566 admission and submission."""
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math


BUY_COST_BUFFER_RATE = .001
BUY_MIN_FEE_RESERVE = 5.


def buy_cost(price, shares):
    value = price * shares
    return value * (1 + BUY_COST_BUFFER_RATE) + BUY_MIN_FEE_RESERVE


def affordable_shares(cash, price):
    if not all(math.isfinite(v) for v in (cash, price)) or price <= 0:
        return 0
    budget = Decimal(str(cash)) - Decimal(str(BUY_MIN_FEE_RESERVE))
    unit_cost = Decimal(str(price)) * (1 + Decimal(str(BUY_COST_BUFFER_RATE)))
    return max(0, int((budget / unit_cost).to_integral_value(rounding=ROUND_FLOOR)))


def tick_timestamp(tick):
    value = tick.get('time') or tick.get('timetag')
    if isinstance(value, (int, float)):
        return value / 1000 if value > 100000000000 else float(value)
    if not isinstance(value, str):
        return None
    for form in ('%Y%m%d %H:%M:%S', '%Y%m%d%H%M%S',
                 '%Y-%m-%d %H:%M:%S', '%Y%m%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(value, form).timestamp()
        except ValueError:
            continue
    return None


def bounded_buy_limit(base, premium, rules):
    try:
        step = Decimal(str(rules['PriceTick']))
        upper = Decimal(str(rules['UpStopPrice']))
        lower = Decimal(str(rules['DownStopPrice']))
        value = Decimal(str(base))
        if not all(v.is_finite() and v > 0 for v in (step, upper, lower, value)):
            return 0.
        if lower > upper or value < lower or value > upper:
            return 0.
        wanted = value * (1 + Decimal(str(premium)))
        rounded = (wanted / step).to_integral_value(rounding=ROUND_CEILING) * step
        cap = (upper / step).to_integral_value(rounding=ROUND_FLOOR) * step
        return float(min(rounded, cap))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return 0.
