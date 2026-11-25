"""Causal price-space policies, explicit lot-aware round-trip cost admission.

Only past completed-minute VWAP/extrema supply anchors. This is a hypothesis
about attainable movement, never a claim that a past extreme will be revisited.
"""
from dataclasses import dataclass
import math

from .dayt_regime_minute_lab import POLICIES, fee


CANDIDATES = ('HOLD', 'SYMMETRIC', 'RAW_SPACE_REVERT', 'COST_REVERT',
              'COST_RANGE', 'COST_TREND', 'COST_SHOCK', 'COST_ROUTER')
LEARNABLE = ('COST_REVERT', 'COST_RANGE', 'COST_TREND', 'COST_SHOCK')


def enrich(frame):
    result = frame.copy()
    group = result.groupby('session', sort=False)
    result['ret1'] = group['close'].pct_change(fill_method=None)
    result['past_high30'] = group['high'].transform(lambda values: values.shift(1).rolling(30, min_periods=20).max())
    result['past_low30'] = group['low'].transform(lambda values: values.shift(1).rolling(30, min_periods=20).min())
    return result


def round_trip_rate(quantity, entry, exit_price, worst_slip_bps=20):
    if quantity < 100 or entry <= 0 or exit_price <= 0:
        return math.inf
    value = quantity * entry
    # Exactly one buy and one sell. Use the dearer quote for both fees to avoid
    # underestimating sell stamp when the profitable side is a forward T.
    dearer = max(entry, exit_price)
    broker = fee(quantity, dearer) + fee(-quantity, dearer)
    impact = quantity * (entry + exit_price) * worst_slip_bps / 10_000
    return (broker + impact) / value


@dataclass(frozen=True)
class SpacePolicy:
    name: str
    family: str = 'revert'
    cost_gate: bool = True
    distance: float = 0.30
    max_minutes: int = 120

    def setup(self, row):
        mode = row['regime']
        if mode in ('UNKNOWN', 'ILLIQUID'):
            return None
        family = self.family
        if family == 'router':
            family = ('range' if mode.startswith('RANGE_') else
                      ('trend' if mode.endswith('_TREND') else 'shock'))
        band = min(0.03, max(0.002, row['atr_pct'] * 0.30))
        gap = row['vwap_gap']
        if family in ('revert', 'range'):
            if family == 'range' and not mode.startswith('RANGE_'):
                return None
            if gap >= band and row['ret3'] < 0:
                return -1, row['vwap']
            if gap <= -band and row['ret3'] > 0:
                return 1, row['vwap']
        elif family == 'trend':
            if mode == 'UP_TREND' and row['ret3'] > 0 and row['ret5'] < 0:
                return 1, row['past_high30']
            if mode == 'DOWN_TREND' and row['ret3'] < 0 and row['ret5'] > 0:
                return -1, row['past_low30']
        elif family == 'shock':
            if mode == 'SHOCK_UP' and row['ret1'] < 0 and row['index_ret3'] <= 0.002:
                return -1, row['vwap']
            if mode == 'SHOCK_DOWN' and row['ret1'] > 0 and row['index_ret3'] >= -0.002:
                return 1, row['vwap']
        return None

    def entry(self, row):
        setup = self.setup(row)
        if setup is None or not math.isfinite(setup[1]):
            return 0
        side = setup[0]
        anchor = setup[1]
        # A minimum space is required even in the no-cost-gate ablation.
        room = side * (anchor / row['close'] - 1)
        return side if room * 0.60 >= 0.003 else 0

    def execution_terms(self, signal, quantity, price, direction):
        setup = self.setup(signal)
        if setup is None or setup[0] != direction:
            return None
        anchor = float(setup[1])
        target = direction * (anchor / price - 1) * 0.60
        if not math.isfinite(target) or not 0.003 <= target <= 0.05:
            return None
        exit_price = price * (1 + direction * target)
        cost_rate = round_trip_rate(quantity, price, exit_price)
        if self.cost_gate and target < cost_rate * 1.50 + 0.002:
            return None
        return dict(target_pct=target, stop_pct=min(0.03, max(0.0075, target * 0.75)),
                    anchor_price=anchor, expected_round_trip_rate=cost_rate,
                    admission_margin=target - cost_rate * 1.50 - 0.002,
                    cost_gate=self.cost_gate, anchor_fraction=0.60)


CATALOG = dict(POLICIES)
CATALOG.update({
    'RAW_SPACE_REVERT': SpacePolicy('RAW_SPACE_REVERT', cost_gate=False),
    'COST_REVERT': SpacePolicy('COST_REVERT'),
    'COST_RANGE': SpacePolicy('COST_RANGE', family='range'),
    'COST_TREND': SpacePolicy('COST_TREND', family='trend'),
    'COST_SHOCK': SpacePolicy('COST_SHOCK', family='shock'),
    'COST_ROUTER': SpacePolicy('COST_ROUTER', family='router'),
})
