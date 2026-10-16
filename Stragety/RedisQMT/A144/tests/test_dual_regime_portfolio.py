# -*- coding: utf-8 -*-
"""All order tests use a fake broker; this suite cannot submit real orders."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from Stragety.RedisQMT.A144 import A144_v12_DualRegimePortfolio as policy
from Stragety.RedisQMT.A144.core.dual_regime_execution import Journal, RedisBroker, atomic_save, exclusive_state, new_state
from Stragety.RedisQMT.A144.core.dual_regime_runtime import Market, TIMES, build_intents, load_universe, normalize


def intent(side='BUY', signal='20261016093100', quantity=200):
    return dict(key=f'A144V12-{signal}-159981-{side[0]}', code='159981.SZ',
                sleeve='secondary', side=side, quantity=quantity, limit=2., date=signal[:8],
                signal=signal, execution_stamp=signal[:8] + '093200')


def reported(order, quantity=100, price=2., status=55):
    return SimpleNamespace(order_remark=order['key'], stock_code=order['code'],
        order_type=23 if order['side'] == 'BUY' else 24, order_volume=order['quantity'],
        traded_volume=quantity, traded_price=price, order_status=status, order_id='broker-1')


class FakeBroker:
    def __init__(self):
        self.rows = []
        self.submitted = []
        self.canceled = []
        self.timeout = False

    def submit(self, order):
        self.submitted.append(dict(order))
        if self.timeout:
            raise TimeoutError('Accepted by fake broker, reply lost')
        return 'broker-1'

    def orders(self):
        return self.rows

    def cancel(self, order_id):
        self.canceled.append(order_id)


def restored(path):
    return Journal(path, json.loads(path.read_text(encoding='utf-8')))


def test_timeout_recovery_and_partial_fills_are_idempotent(tmp_path):
    path = tmp_path / 'state.json'
    journal = Journal(path, new_state('test', 10000.))
    broker = FakeBroker()
    broker.timeout = True
    order = intent()
    with pytest.raises(TimeoutError):
        journal.submit(broker, order, '2026-10-16T09:32:00')
    journal = restored(path)
    assert journal.reconcile(broker, '20261016') == [order['key']]
    journal.submit(broker, order, 'ignored')
    assert len(broker.submitted) == 1
    broker.rows = [reported(order)]
    journal.reconcile(broker, '20261016')
    assert journal.state['positions']['159981.SZ']['quantity'] == 100
    assert journal.state['cash'] == 9795.
    journal = restored(path)
    journal.reconcile(broker, '20261016')
    assert journal.state['cash'] == 9795.
    broker.rows = [reported(order, 200, 2.1, 56)]
    journal.reconcile(broker, '20261016')
    assert journal.state['cash'] == 9575.
    assert journal.state['positions']['159981.SZ']['buy_fee'] == 5.
    assert len(journal.state['fills']) == 2


def test_write_ahead_failure_never_reaches_broker(tmp_path, monkeypatch):
    journal = Journal(tmp_path / 'state.json', new_state('test', 1000.))
    broker = FakeBroker()
    def disk_error():
        raise OSError('disk full')
    monkeypatch.setattr(journal, 'save', disk_error)
    with pytest.raises(OSError):
        journal.submit(broker, intent(), 'now')
    assert not broker.submitted


def test_cancel_waits_for_ack_and_late_fill_is_not_lost(tmp_path):
    journal = Journal(tmp_path / 'state.json', new_state('test', 10000.))
    broker = FakeBroker()
    order = intent()
    journal.submit(broker, order, 'now')
    journal.cancel_stale(broker, '20261016093300')
    journal.cancel_stale(broker, '20261016093300')
    assert broker.canceled == ['broker-1']
    assert journal.active('159981.SZ')
    broker.rows = [reported(order, 100, 2., 53)]
    journal.reconcile(broker, '20261016')
    assert not journal.active()
    assert journal.state['positions']['159981.SZ']['quantity'] == 100


def test_closed_trade_costs_and_ownership_survive_reload(tmp_path):
    path = tmp_path / 'state.json'
    journal = Journal(path, new_state('test', 10000.))
    broker = FakeBroker()
    buy = intent()
    journal.submit(broker, buy, 'now')
    broker.rows = [reported(buy, 200, 2., 56)]
    journal.reconcile(broker, '20261016')
    sell = intent('SELL', '20261016094000')
    journal.submit(broker, sell, 'now')
    broker.rows.append(reported(sell, 100, 2.2, 55))
    journal.reconcile(broker, '20261016')
    journal = restored(path)
    broker.rows[-1] = reported(sell, 200, 2.2, 56)
    journal.reconcile(broker, '20261016')
    assert journal.state['trades'][0]['pnl'] == pytest.approx(30.)
    assert journal.state['cash'] == pytest.approx(10030.)
    assert not journal.state['positions']
    restored(path).reconcile(broker, '20261016')
    assert len(restored(path).state['trades']) == 1


def test_orphan_orders_and_position_mismatch_block(tmp_path):
    journal = Journal(tmp_path / 'state.json', new_state('test', 10000.))
    broker = FakeBroker()
    broker.rows = [reported(intent())]
    with pytest.raises(RuntimeError, match='absent'):
        journal.reconcile(broker, '20261016')
    broker.rows = []
    journal.submit(broker, intent(), 'now')
    broker.rows = [reported(intent())]
    journal.reconcile(broker, '20261016')
    with pytest.raises(RuntimeError, match='mismatch'):
        journal.check_positions({})


def test_actual_process_killed_after_acceptance_can_recover(tmp_path):
    path = tmp_path / 'state.json'
    acceptance = tmp_path / 'accepted.json'
    script = '''
import json, os, sys
from pathlib import Path
from Stragety.RedisQMT.A144.core.dual_regime_execution import Journal, atomic_save, new_state
order = json.loads(sys.argv[3])
class Broker:
    def submit(self, value):
        atomic_save(Path(sys.argv[2]), value)
        os._exit(91)
Journal(Path(sys.argv[1]), new_state('test', 10000.)).submit(Broker(), order, 'now')
'''
    process = subprocess.run([sys.executable, '-c', script, str(path), str(acceptance), json.dumps(intent())], timeout=20)
    assert process.returncode == 91
    accepted = json.loads(acceptance.read_text(encoding='utf-8'))
    journal = restored(path)
    broker = FakeBroker()
    broker.rows = [reported(accepted, 200, 2., 56)]
    assert journal.reconcile(broker, '20261016') == []
    journal.submit(broker, intent(), 'now')
    assert not broker.submitted
    assert journal.state['positions']['159981.SZ']['quantity'] == 200


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel lock')
def test_process_lock_excludes_parallel_start_and_releases_after_kill(tmp_path):
    path = tmp_path / 'account'
    script = '''
import sys
from Stragety.RedisQMT.A144.core.dual_regime_execution import exclusive_state
with exclusive_state(sys.argv[1]):
    print('LOCKED', flush=True)
    sys.stdin.read()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'LOCKED'
        with pytest.raises(OSError):
            with exclusive_state(path):
                pytest.fail('duplicate process acquired lock')
    finally:
        process.kill()
        process.wait(timeout=10)
    with exclusive_state(path):
        pass


def test_secondary_gates_and_exits_remain_active_after_macro_recovery():
    signal = dict(leader=True, is_reversion=False, index_down=True, ma20=1.9)
    bar = dict(close=2., low=1.99)
    assert policy.secondary_entry(signal, bar, 1.99, '093100', True)
    assert not policy.secondary_entry(signal, bar, 1.99, '093100', False)
    signal['index_down'] = False
    assert not policy.secondary_entry(signal, bar, 1.99, '093100', True)
    position = dict(buy_notional=220., bought=100, quantity=100, buy_fee=5., sell_notional=0., sell_fee=0.)
    assert policy.secondary_exit(position, signal, bar, '100000', 2) == 'stop'


def test_primary_does_not_exit_on_breadth_alone_and_new_entry_is_not_evaluated_early():
    position = dict(entry_date='20261015', buy_notional=1000., bought=100)
    row = dict(date='20261015', adj_close=10., close=10., market_strict=True,
               atr=.2, ma50=9., mom60_rank=.99, breadth=.2)
    assert policy.primary_exit(position, row, pd.DataFrame([row])) == ''
    row['market_strict'] = False
    assert policy.primary_exit(position, row, pd.DataFrame([row])) == 'index-stress'
    position['entry_date'] = '20261016'
    assert policy.primary_exit(position, row, pd.DataFrame([row])) == ''


def test_completed_bar_validation_rejects_placeholders_and_duplicates():
    frame = pd.DataFrame([dict(open=2., high=2., low=2., close=2., volume=100., amount=20000.)], index=['20261016093100'])
    assert len(normalize(frame, '20261016093100', daily=False)) == 1
    with pytest.raises(ValueError, match='Duplicate'):
        normalize(pd.concat([frame, frame]), '20261016093100', daily=False)
    frame['close'] = 0
    with pytest.raises(ValueError, match='Zero'):
        normalize(frame, '20261016093100', daily=False)


def test_expired_membership_is_never_used(tmp_path):
    path = tmp_path / 'universe.json'
    atomic_save(path, dict(source='official-test', valid_through='20261015', snapshots={}))
    with pytest.raises(ValueError, match='expired'):
        load_universe(path, ['20261015'], '20261016')


def test_external_positions_are_not_adopted_and_shared_cash_is_bounded(tmp_path):
    state = new_state('test', 1000.)
    journal = Journal(tmp_path / 'state.json', state)
    signal = '20261016093100'
    frame = pd.DataFrame([dict(open=2., high=2., low=2., close=2., volume=100000., amount=2e7)], index=[signal])
    bars = {code: frame for code in policy.CODES}
    secondary = {code: dict(leader=True, is_reversion=False, index_down=True, ma20=1.9) for code in policy.CODES}
    market = SimpleNamespace(weak=True, secondary=secondary, dates=['20261015', '20261016'],
        daily={code: pd.DataFrame(dict(close=[2.], amount=[1e9])) for code in policy.CODES})
    actual = {'159980.SZ': dict(quantity=100, sellable=100)}
    orders = build_intents(policy, market, journal, actual, 1000., '20261016093200', signal, bars)
    assert all(order['code'] != '159980.SZ' for order in orders)
    assert sum(order['quantity'] * order['limit'] + 5 for order in orders) <= 1000.
    assert not state['positions']


def test_downtime_stop_is_latched_even_if_price_recovered():
    code = '159981.SZ'
    index = [date + value for date in ('20261014', '20261015') for value in TIMES]
    history = pd.DataFrame(dict(open=2., high=2., low=2., close=2., volume=1000., amount=200000.), index=index)
    history.loc['20261014100000', ['low', 'close']] = 1.9
    current = history.iloc[:1].copy()
    current.index = ['20261016093100']
    position = dict(entry_date='20261014', entry_stamp='20261014093200', bought=100,
        quantity=100, buy_notional=200., buy_fee=5., sell_notional=0., sell_fee=0.)
    market = Market(None, None, policy)
    market.day = '20261016'
    market.asof = '20261015'
    market.dates = list(pd.bdate_range('20261001', '20261016').strftime('%Y%m%d'))
    market.daily = {code: pd.DataFrame(dict(close=2.), index=market.dates)}
    market.secondary = {code: dict(is_reversion=False)}
    market.request = lambda *args, **kwargs: {code: history}
    assert market.recover_secondary_exits(code, position, current) == 'stop'
    assert position['last_exit_signal'] == '20261014100000'


def test_primary_exit_recovery_uses_historical_peak_without_future_rows():
    position = dict(entry_date='20261013', buy_notional=1000., bought=100)
    history = pd.DataFrame([
        dict(date='20261013', adj_close=10., close=10., market_strict=True, atr=.2, ma50=9., mom60_rank=.99),
        dict(date='20261014', adj_close=10., close=10., market_strict=False, atr=.2, ma50=9., mom60_rank=.99),
        dict(date='20261015', adj_close=15., close=15., market_strict=True, atr=.2, ma50=9., mom60_rank=.99)])
    assert policy.primary_exit(position, history.iloc[0], history) == ''
    assert policy.primary_exit(position, history.iloc[1], history) == 'index-stress'


def test_redis_adapter_routes_limit_orders_and_normalizes_real_snapshot_shape():
    calls = []
    class Client:
        def call(self, method, params):
            if method == 'query_stock_asset':
                return dict(cash=2000.)
            return {'159981.SZ': dict(stock_code='159981.SZ', volume=100, available=100)}
    class Trader:
        client = Client()
        def order_stock_batch(self, *args, **kwargs):
            calls.append(args)
            assert kwargs == dict(batch_id=intent()['key'])
            return [dict(accepted=True, order_sys_id='id')]
    account = SimpleNamespace(account_id='test')
    broker = RedisBroker(Trader(), account)
    order = dict(intent(), remark=intent()['key'])
    assert broker.submit(order) == 'id'
    assert calls[0][0] == account
    assert calls[0][1] == [dict(stock_code='159981.SZ', order_type=23, order_volume=200,
        price_type=11, price=2., strategy_name='A144V12', order_remark=order['key'], require_idempotency_check=True)]
    assert broker.snapshot() == (2000., {'159981.SZ': dict(quantity=100, sellable=100)})


def test_missing_downtime_minute_blocks_exit_reconstruction():
    code = '159981.SZ'
    market = Market(None, None, policy)
    market.day = '20261016'
    market.asof = '20261015'
    market.dates = ['20261015', '20261016']
    frame = pd.DataFrame(dict(open=2., high=2., low=2., close=2., volume=1., amount=200.), index=['20261015093200'])
    market.request = lambda *args, **kwargs: {code: frame}
    position = dict(entry_stamp='20261015093200')
    with pytest.raises(ValueError, match='Missing downtime'):
        market.recover_secondary_exits(code, position, frame)


def test_partial_sell_respects_actual_sellable_stock_quantity(tmp_path):
    state = new_state('test', 1000.)
    state['positions']['600001.SH'] = dict(sleeve='primary', quantity=200, bought=200,
        buy_notional=2000., buy_fee=5., sell_notional=0., sell_fee=0., entry_date='20261015',
        entry_stamp='20261015093200', exit_reason='index-stress')
    journal = Journal(tmp_path / 'state.json', state)
    signal = '20261016093100'
    frame = pd.DataFrame(dict(open=10., high=10., low=10., close=10., volume=100000.), index=[signal])
    market = SimpleNamespace(weak=False, candidates=pd.DataFrame(columns=['code']),
        dates=['20261015', '20261016'], daily={'600001.SH': pd.DataFrame(dict(close=[10.]))})
    orders = build_intents(policy, market, journal, {'600001.SH': dict(quantity=200, sellable=0)},
        1000., '20261016093200', signal, {'600001.SH': frame})
    assert not orders
    orders = build_intents(policy, market, journal, {'600001.SH': dict(quantity=200, sellable=100)},
        1000., '20261016093200', signal, {'600001.SH': frame})
    assert len(orders) == 1 and orders[0]['side'] == 'SELL' and orders[0]['quantity'] == 100


def test_recovery_before_rpc_uses_same_identity_and_never_resends_expired_intent(tmp_path):
    path = tmp_path / 'state.json'
    journal = Journal(path, new_state('test', 10000.))
    broker = FakeBroker()
    order = intent()
    def interrupted_before_rpc(value):
        raise SystemExit('process interrupted before send')
    original = broker.submit
    broker.submit = interrupted_before_rpc
    with pytest.raises(SystemExit):
        journal.submit(broker, order, 'now')
    journal = restored(path)
    broker.submit = original
    assert not journal.recover_submission(broker, order['key'], '20261016093300')
    assert not broker.submitted
    assert journal.recover_submission(broker, order['key'], '20261016093200')
    assert len(broker.submitted) == 1 and broker.submitted[0]['remark'] == order['key']


def test_market_prepares_full_dated_membership_and_previous_day_indicators(tmp_path):
    today = '20261016'
    dates = list(pd.bdate_range(end=today, periods=261).strftime('%Y%m%d'))
    codes = [f'{600000 + index:06d}.SH' for index in range(500)]
    path = tmp_path / 'universe.json'
    atomic_save(path, dict(source='synthetic-test-only', valid_through=today, snapshots={dates[0]: codes}))
    close = pd.Series([10 + index * .05 for index in range(260)], index=dates[:-1])
    frame = pd.DataFrame(dict(open=close, high=close * 1.02, low=close * .98,
        close=close, volume=100000., amount=1e8))
    class XtData:
        def get_trading_dates(self, *args, **kwargs):
            return dates
        def get_market_data_ex(self, **kwargs):
            assert kwargs['end_time'] == dates[-2]
            return {code: frame for code in kwargs['stock_list']}
    market = Market(XtData(), path, policy)
    state = new_state('test', 100000.)
    market.prepare(today, state)
    assert market.day == today and market.asof == dates[-2]
    assert market.weak is False
    rows = market.primary.loc[market.primary.date.eq(dates[-2]) & market.primary.member]
    assert len(rows) == 500 and rows.breadth.eq(1.).all()
    assert len(state['universe_audit'][today]) == 64
