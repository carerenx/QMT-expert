# -*- coding: utf-8 -*-
"""Research-only one-time profit reductions based on completed closes."""


def partial_quantity(units, factor, fraction, code):
    shares = units * factor
    quantity = int((shares * fraction + 1e-8) // 100) * 100
    minimum = 200 if code.startswith(("688", "689")) else 100
    return quantity if quantity >= minimum and shares - quantity >= minimum - 1e-8 else 0


def partial_fraction(position, row, mode):
    gain = round(row["adj_close"] / position["entry_price"] - 1, 12)
    peak = round(position["peak"] / position["entry_price"] - 1, 12)
    if mode == "half15" and gain >= .15:
        return .5
    if mode == "half30" and gain >= .30:
        return .5
    if mode == "third15" and gain >= .15:
        return 1 / 3
    if mode == "retrace15" and peak >= .15 and gain <= round(peak - .05, 12):
        return .5
    return 0.
