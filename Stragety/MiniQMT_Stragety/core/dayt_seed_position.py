"""Initial-position admission and persistent, at-most-once order intents.

No market connections here. The caller supplies verified daily data and broker
snapshots. An unresolved intent is never automatically submitted again.
"""

from dataclasses import dataclass
import math

from .dayt_execution_safety import affordable_shares, buy_cost


@dataclass(frozen=True)
class SeedPolicy:
    total_fraction: float = 0.50
    symbol_fraction: float = 0.20
    start: str = '09:45:00'
    end: str = '14:00:00'
    maximum_gain: float = 0.02
    order_timeout: float = 30.0

    def __post_init__(self):
        if not 0 < self.symbol_fraction <= self.total_fraction <= 1:
            raise ValueError('require 0 < symbol_fraction <= total_fraction <= 1')
        if not ('09:30:00' <= self.start < self.end <= '14:50:00'):
            raise ValueError('invalid seed order window')
        if not 0 < self.maximum_gain <= 0.1 or self.order_timeout <= 0:
            raise ValueError('invalid seed price/order limits')


def seed_shares(budget, limit_price):
    if not math.isfinite(budget) or budget <= 0:
        return 0
    return affordable_shares(budget, limit_price) // 100 * 100


class SeedPositionManager:
    def __init__(self, journal, policy, dry_run, log):
        self.journal = journal
        self.policy = policy
        self.dry_run = dry_run
        self.log = log
        self._last_plan_key = None
        self.state = {'baseline_cash': None, 'spent': 0.0, 'attempts': {}, 'pending': None}
        if not dry_run:
            journal.acquire()
            try:
                saved = journal.read()
                if saved is not None:
                    if saved.get('purpose') != 'DayT-v0624-seed':
                        raise ValueError('wrong seed checkpoint purpose')
                    self.state = saved['seed']
                    if (not isinstance(self.state['attempts'], dict)
                            or not math.isfinite(float(self.state['spent']))
                            or self.state['spent'] < 0):
                        raise ValueError('invalid seed checkpoint')
                    baseline = self.state['baseline_cash']
                    if baseline is not None and (not math.isfinite(float(baseline)) or baseline < 0):
                        raise ValueError('invalid baseline cash')
            except Exception:
                journal.close()
                raise

    @property
    def pending(self):
        return self.state['pending']

    def _save(self):
        if not self.dry_run:
            self.journal.write({'purpose': 'DayT-v0624-seed', 'seed': self.state})

    def close(self):
        if not self.dry_run:
            self.journal.close()

    def filled_today(self, code, today):
        record = self.state['attempts'].get(code) or {}
        return record.get('fill_date') == today and record.get('filled', 0) > 0

    def reconcile(self, orders, now, today, cancel):
        pending = self.pending
        if not pending:
            return
        matches = [row for row in orders
                   if str(row.get('order_remark') or '') == pending['remark']
                   and row.get('stock_code') == pending['code']]
        if len(matches) != 1:
            self.log('[SEED-PENDING] no unique broker match; no resubmission')
            return
        row = matches[0]
        if (int(row['order_volume']) != pending['shares']
                or int(row['order_type']) != 23):
            raise ValueError('seed broker order identity mismatch')
        filled = int(row['traded_volume'])
        if not 0 <= filled <= pending['shares']:
            raise ValueError('invalid seed filled quantity')
        status = int(row['order_status'])
        if status in (53, 54, 56, 57):
            price = float(row.get('traded_price') or 0)
            if filled and (not math.isfinite(price) or price <= 0):
                raise ValueError('missing seed average execution price')
            # Reserve the same conservative cost buffer used at admission.
            self.state['spent'] += buy_cost(price, filled) if filled else 0
            self.state['attempts'][pending['code']] = {
                'status': status, 'filled': filled, 'price': price,
                'fill_date': today if filled else '', 'remark': pending['remark'],
            }
            self.state['pending'] = None
            self._save()
            self.log('[SEED-SETTLED] {} filled={} avg={} status={}; T+1'.format(
                pending['code'], filled, price, status))
            return
        if now - pending['submitted_at'] >= self.policy.order_timeout and not pending.get('cancel_requested'):
            pending['cancel_requested'] = True
            self._save()
            # Persist BEFORE the request: exceptions must not cause repeated cancels.
            cancel(row['order_id'])
            self.log('[SEED-CANCEL] timeout; awaiting broker terminal state')

    def plan(self, code, price, last_close, cash, reserved, held, active_orders,
             selection_ok, trading_day, hms):
        """Read-only preview using the exact sizing and guards used to submit."""
        result = dict(code=code, price=price, shares=0, budget=0.0,
                      cash=cash, reserved=reserved, baseline=0.0,
                      total_cap=0.0, remaining=0.0, estimated_cost=0.0,
                      chase_cap=last_close * (1 + self.policy.maximum_gain),
                      reason='', mode='SIGNAL' if self.dry_run else 'LIVE')
        numbers = (price, last_close, cash, reserved)
        valid = (all(math.isfinite(value) for value in numbers)
                 and min(price, last_close) > 0 and cash >= 0 and reserved >= 0)
        if valid:
            free = max(0.0, cash - reserved)
            baseline = self.state['baseline_cash']
            if baseline is None:
                baseline = free
            total_cap = baseline * self.policy.total_fraction
            remaining = max(0.0, total_cap - self.state['spent'])
            budget = min(baseline * self.policy.symbol_fraction, remaining, free)
            quantity = seed_shares(budget, price)
            result.update(baseline=baseline, total_cap=total_cap, remaining=remaining,
                          budget=budget, shares=quantity,
                          estimated_cost=buy_cost(price, quantity) if quantity else 0.0)
        if (not trading_day or not self.policy.start <= hms <= self.policy.end
                or '11:30:00' <= hms < '13:00:00'):
            result['reason'] = 'outside entry session'
        elif self.pending or active_orders:
            result['reason'] = 'unresolved or active account order'
        elif code in held or code in self.state['attempts']:
            result['reason'] = 'already held or attempted'
        elif not valid:
            result['reason'] = 'invalid price/cash'
        elif not selection_ok or price > result['chase_cap']:
            result['reason'] = 'selection or price condition not met'
        elif result['shares'] < 100:
            result['reason'] = 'budget insufficient for 100 shares including costs'
        return result

    def log_plan(self, plan):
        key = tuple(plan.items())
        if key == self._last_plan_key:
            return
        self._last_plan_key = key
        self.log('[SEED-PLAN] {code} mode={mode} | BUY limit=Y{price:.2f} '
                 'planned={shares} sh cost-with-buffer~Y{estimated_cost:.2f} | '
                 'budget=Y{budget:.2f} campaign-cap=Y{total_cap:.2f} '
                 'remaining=Y{remaining:.2f} cash=Y{cash:.2f} '
                 'T-reserve=Y{reserved:.2f} chase-cap=Y{chase_cap:.2f} | '
                 'status={status}; recheck before submission'.format(
                     **plan, status=plan['reason'] or ('SIGNAL-ONLY' if self.dry_run else 'READY')))

    def admit(self, code, price, last_close, cash, reserved, held, active_orders,
              selection_ok, trading_day, now, today, hms, submit):
        plan = self.plan(code, price, last_close, cash, reserved, held,
                         active_orders, selection_ok, trading_day, hms)
        self.log_plan(plan)
        if plan['reason']:
            return plan['reason']
        quantity = plan['shares']
        budget = plan['budget']
        if self.dry_run:
            self.log('[SEED-SIGNAL] {} buy={} limit={} budget={:.2f}; no order'.format(
                code, quantity, price, budget))
            return 'signal'
        self.state['baseline_cash'] = plan['baseline']
        remark = 'S0624{}{}'.format(today, code.split('.')[0])
        self.state['pending'] = {
            'code': code, 'shares': quantity, 'limit_price': price,
            'remark': remark, 'submitted_at': now, 'order_id': None,
        }
        self.state['attempts'][code] = {'status': 'INTENT', 'filled': 0}
        self._save()
        self.log('[SEED-INTENT] {} buy={} limit={}; persisted before submit'.format(code, quantity, price))
        # If the broker raises or returns no id, keep the intent and reconcile.
        order_id = submit(code, quantity, price, remark)
        self.pending['order_id'] = str(order_id) if order_id is not None else None
        self._save()
        return 'submitted'
