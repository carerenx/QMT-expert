# -*- coding: utf-8 -*-
"""Durable write-ahead orders; broker cumulative fills are applied exactly once.

Uses project BigQMT shim order/query/cancel methods, not injected QMT APIs.
No broker connection or order is made on import.
"""
from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from pathlib import Path


TERMINAL = {53, 54, 56, 57}  # xtquant/xtconstant.py: partial-cancel/cancel/fill/reject
PRIMARY_COOLDOWN_SESSIONS = 10


def atomic_save(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def exclusive_state(path):
    # Windows kernel releases this byte lock on abrupt process termination.
    import msvcrt
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def new_state(account, capital):
    return dict(schema=1, strategy='A144V12', account=str(account), initial_cash=capital,
                cash=capital, positions={}, orders={}, parents={}, counts={}, cooldowns={},
                fills=[], trades=[], last_signal='', day='')


def fee(notional, side, date, sleeve):
    if notional <= 0:
        return 0.
    if sleeve == 'secondary':
        return max(5., notional * .0001)
    rate = .00076 if side == 'BUY' else (.00176 if date < '20230828' else .00126)
    return max(5., notional * rate)


def price_limit(price, side, sleeve):
    ticks = 1000 if sleeve == 'secondary' else 100
    value = price * (1.01 if side == 'BUY' else .99) * ticks
    return (math.ceil(value - 1e-8) if side == 'BUY' else math.floor(value + 1e-8)) / ticks


class Journal:
    def __init__(self, path, state):
        self.path = Path(path)
        self.state = state

    def save(self):
        atomic_save(self.path, self.state)

    def active(self, code=None):
        return [order for order in self.state['orders'].values()
                if not order['terminal'] and (code is None or order['code'] == code)]

    def submit(self, broker, intent, now):
        # Signal cursor is persisted by the caller only after all its intents.
        # The deterministic key closes the crash window between these writes.
        key = intent['key']
        if key in self.state['orders']:
            return
        if self.active(intent['code']):
            raise RuntimeError('Previous order unresolved: ' + intent['code'])
        order = dict(intent, remark=key, sent_at=now, terminal=False, order_id='',
                     applied_quantity=0, applied_notional=0., applied_fee=0., status='unknown')
        self.state['orders'][key] = order
        self.save()  # Must be durable BEFORE the first RPC side effect.
        result = broker.submit(order)
        order['order_id'] = str(result or '')
        # Empty/-1 is ambiguous with this bridge; never infer zero execution.
        order['status'] = 'submitted' if result and str(result) != '-1' else 'unknown'
        self.save()

    def reconcile(self, broker, today):
        orders = broker.orders()
        matches = {}
        for item in orders:
            remark = item.order_remark
            if remark.startswith('A144V12-') and remark not in self.state['orders']:
                raise RuntimeError('Broker has an order absent from this checkpoint: ' + remark)
            if remark in self.state['orders']:
                if remark in matches:
                    raise RuntimeError('Duplicate broker remark: ' + remark)
                matches[remark] = item
        unresolved = []
        for key, order in self.state['orders'].items():
            if order['terminal']:
                continue
            item = matches.get(key)
            if item is None:
                unresolved.append(key)
                continue
            if (item.stock_code != order['code'] or item.order_type != (23 if order['side'] == 'BUY' else 24)
                    or item.order_volume != order['quantity']):
                raise RuntimeError('Broker order identity mismatch: ' + key)
            quantity = int(item.traded_volume)
            notional = quantity * float(item.traded_price)
            if (quantity < order['applied_quantity'] or quantity > order['quantity']
                    or not math.isfinite(notional) or (quantity and notional <= 0)):
                raise RuntimeError('Invalid cumulative broker fill: ' + key)
            charge = fee(notional, order['side'], order['date'], order['sleeve'])
            delta = quantity - order['applied_quantity']
            amount = notional - order['applied_notional']
            costs = charge - order['applied_fee']
            if delta or abs(amount) > 1e-7 or abs(costs) > 1e-7:
                self.apply_fill(order, delta, amount, costs, today)
            order['applied_quantity'] = quantity
            order['applied_notional'] = notional
            order['applied_fee'] = charge
            order['order_id'] = str(item.order_id)
            order['terminal'] = item.order_status in TERMINAL
            order['status'] = str(item.order_status)
            self.save()  # Position delta and applied cursor in the same atomic file.
        return unresolved

    def recover_submission(self, broker, key, stamp):
        order = self.state['orders'][key]
        if stamp != order['execution_stamp']:
            return False  # Never replay an expired intent at a new price/time.
        # RedisBroker uses server-side strict identity checks and the same tag.
        # The bridge handles this RPC serially with the original submission.
        result = broker.submit(order)
        order['order_id'] = str(result or '')
        self.save()
        return True

    def apply_fill(self, order, quantity, notional, charge, today):
        code = order['code']
        positions = self.state['positions']
        if order['side'] == 'BUY':
            if code not in positions:
                if quantity <= 0:
                    raise RuntimeError('Fill adjustment without position')
                positions[code] = dict(sleeve=order['sleeve'], quantity=0, bought=0,
                    buy_notional=0., buy_fee=0., sell_notional=0., sell_fee=0.,
                    entry_date=order['date'], entry_stamp=order['execution_stamp'], exit_reason='')
            position = positions[code]
            position['quantity'] += quantity
            position['bought'] += quantity
            position['buy_notional'] += notional
            position['buy_fee'] += charge
            self.state['cash'] -= notional + charge
            parent = self.state['parents'].get(code)
            if parent:
                parent['spent'] += notional + charge
        else:
            position = positions[code]
            if quantity > position['quantity']:
                raise RuntimeError('Sell exceeds strategy-owned quantity')
            position['quantity'] -= quantity
            position['sell_notional'] += notional
            position['sell_fee'] += charge
            self.state['cash'] += notional - charge
            if position['quantity'] == 0:
                pnl = position['sell_notional'] - position['sell_fee'] - position['buy_notional'] - position['buy_fee']
                self.state['trades'].append(dict(position, code=code, exit_date=today, pnl=pnl,
                    fee_basis='estimated-per-order; reconcile against broker statements'))
                if position['sleeve'] == 'primary':
                    self.state['cooldowns'][code] = dict(date=today, sessions=PRIMARY_COOLDOWN_SESSIONS)
                positions.pop(code)
        self.state['fills'].append(dict(key=order['key'], code=code, side=order['side'],
            date=today, quantity=quantity, notional=notional, estimated_fee=charge))

    def cancel_stale(self, broker, stamp):
        # A cancel request is not a cancellation acknowledgement. Continue to
        # reserve cash and prohibit replacement until reconciliation is terminal.
        for order in self.active():
            if (stamp > order['execution_stamp'] and order['order_id']
                    and order.get('cancel_requested_for') != stamp):
                order['cancel_requested_for'] = stamp
                self.save()
                broker.cancel(order['order_id'])

    def check_positions(self, actual):
        for code, position in self.state['positions'].items():
            if actual.get(code, {}).get('quantity', 0) != position['quantity']:
                raise RuntimeError('Broker/checkpoint position mismatch: ' + code)


class RedisBroker:
    def __init__(self, trader, account):
        self.trader = trader
        self.account = account

    def orders(self):
        return self.trader.query_stock_orders(self.account, strategy_name='')

    def submit(self, order):
        rows = self.trader.order_stock_batch(self.account, [dict(stock_code=order['code'],
            order_type=23 if order['side'] == 'BUY' else 24, order_volume=order['quantity'],
            price_type=11, price=order['limit'], strategy_name='A144V12',
            order_remark=order['remark'], require_idempotency_check=True)], batch_id=order['key'])
        if not isinstance(rows, list) or len(rows) != 1 or not rows[0].get('accepted'):
            raise RuntimeError('Bridge did not confirm idempotent submission: ' + str(rows))
        return rows[0].get('order_sys_id') or ''

    def cancel(self, order_id):
        return self.trader.cancel_order_stock(self.account, order_id)

    def snapshot(self):
        # Direct RPC avoids the compat layer's stale cached-asset fallback.
        client = self.trader.client
        asset = client.call('query_stock_asset', {'account_id': self.account.account_id})
        rows = client.call('query_stock_positions', {'account_id': self.account.account_id})
        if isinstance(rows, dict):
            rows = list(rows.values())
        if not isinstance(asset, dict) or asset.get('cash') is None or not isinstance(rows, list):
            raise RuntimeError('Incomplete live account snapshot')
        cash = float(asset['cash'])
        if not math.isfinite(cash) or cash < 0:
            raise RuntimeError('Invalid live available cash')
        positions = {row['stock_code']: dict(quantity=int(row['volume']),
            sellable=int(row.get('available', row.get('can_use_volume', 0))))
            for row in rows if int(row.get('volume', 0)) > 0}
        return cash, positions
