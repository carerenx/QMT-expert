"""Offline adversarial review of actual v0628 methods. Never connects/trades.

Run: python analysis/dayt_adversarial_review_20261124/probe.py
Defect assertions are intentional reproductions, not fixed-behavior tests.
"""
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Stragety/MiniQMT_Stragety'))
# Historical defect probe targets the preserved pre-fix source, not current v0628.
SOURCE = Path(__file__).with_name('source_before_fix.py')
spec = importlib.util.spec_from_file_location('adversarial_v0628', SOURCE)
strategy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strategy)


def runner(symbol='601869.SH'):
    portfolio = strategy.PortfolioRunner(dry_run=False)
    portfolio.seed = SimpleNamespace(pending=None)
    subject = strategy.StrategyRunner(portfolio, symbol)
    subject._init_state()
    subject.st.update(base_shares=200, base_can_use=200,
        daily_signal={'atr_pct':0.1,'trend':'bear','sell_trigger':100}, do_short=True)
    subject.messages = []
    subject._log = subject.messages.append
    return subject


def fake_order(subject, volume=100, price=200, status=56):
    subject._submitted_order_id = 123
    row = SimpleNamespace(order_id=123,order_sysid='',stock_code=subject.stock_qmt,
        traded_volume=volume,traded_price=price,order_status=status)
    subject.conn = SimpleNamespace(trader=SimpleNamespace(query_stock_order=lambda *args:row),
                                    _account_obj=object(),cancel_order=lambda *args:True)
    return row


def stale_tick():
    subject = runner()
    now = datetime(2026,11,24,14).timestamp()
    old = {'timetag':'20261124 10:00:00','lastPrice':100.5}
    future = {'timetag':'20261124 14:30:00','lastPrice':100.5}
    assert subject._tick_is_current(old,'20261124',now) is True
    assert subject._tick_is_current(future,'20261124',now) is True
    assert subject._tick_is_current({'timetag':'20261123 14:00:00'},'20261124',now) is False
    subject.st.update(fstate='SPIKING',peak_price=101,short_arm_trigger=100,
                      short_arm_bars=2,trade_count_short=1)
    submissions = []
    subject._submit_order = lambda *args: submissions.append(args) or ('SKIP',0)
    subject._run_inner_state_machine(old['lastPrice'],now)
    assert len(submissions) == 1
    return {'old_tick_age_seconds':14400,'old_tick_accepted':True,'future_tick_accepted':True,
            'submission_path_entered':True,'control_previous_date_blocked':True}


def post_fill_failure():
    subject = runner()
    fake_order(subject)
    calls = []
    subject.conn.cancel_order = lambda order_id: calls.append(order_id)
    # Keep the real refresh/MockPosition path: only the broker payload is bad.
    # A plain query transport exception is swallowed by MiniQMTConnector, so
    # this case specifically models malformed sellable fields AFTER execution.
    malformed = SimpleNamespace(stock_code='601869.SH',volume=100,
                                open_price=200,available='malformed')
    fake_positions = SimpleNamespace(query_positions=lambda:[malformed])
    clock = [0.0]

    def monotonic():
        clock[0] += 1
        return clock[0]

    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(strategy.get_trade_detail_data.__globals__,'_global_conn',fake_positions)
        patch.setattr(strategy._time,'monotonic',monotonic)
        patch.setattr(strategy._time,'sleep',lambda seconds:None)
        with pytest.raises(RuntimeError,match='unresolved broker order'):
            subject._wait_for_fill(-100,'REV-T sell',200,timeout_sec=0.1)
    assert subject.execution_book.legs['SHORT'] == [(200,100)]
    assert any('order already accounted' in message for message in subject.messages)
    return {'broker_reports_filled':True,'book_already_recorded':True,'failed_to_return_fill':True,
            'duplicate_accounting_retries':True,'cancelled_already_filled_order':calls,
            'account_uncertain':subject.portfolio.order_uncertain,'log':subject.messages}


def inconsistent_success_status():
    subject = runner()
    fake_order(subject,volume=0,price=0,status=56)
    subject._refresh_position = lambda:None
    submissions = []

    def submit(*args):
        submissions.append(args)
        return subject._wait_for_fill(-100,'REV-T sell',100.5)

    subject._submit_order = submit
    for attempt in range(2):
        subject._handle_idle(101)
        subject._handle_spiking(100.5)
        subject._handle_spiking(100.5)
        assert subject.st['fstate'] == 'IDLE'
    assert len(submissions) == 2
    assert subject.portfolio.order_uncertain is False
    return {'order_status':56,'traded_volume':0,'attempts':len(submissions),
            'account_uncertain':False,'state':'IDLE','count_after_failures':subject.st['trade_count_short']}


def fund_odd_remainder():
    subject = runner('159981.SZ')
    subject.execution_book.record('sold','REV-T sell',-150,1.75)
    subject.net_attribution.record('sold',-150,1.75)
    subject.st.update(fstate='DIPPING',short_legs=[(1.75,150)],sell_fill_price=1.75,
        buyback_target=1.73,buyback_target_pct=1.0,buyback_trigger_mult=0.2)
    subject._clamp_buy_shares = lambda planned,price:planned
    subject.ctx = SimpleNamespace(get_full_tick=lambda codes:{'159981.SZ':{'askPrice':[1.73]}})
    subject._refresh_position = lambda:None
    calls = []
    row = fake_order(subject,volume=100,price=1.73)
    subject._last_executed_order = None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strategy,'order_shares',lambda *args:calls.append(args) or 123)
        subject._do_buyback(1.73)
        assert subject.st['short_legs'] == [(1.75,50)]
        assert subject._do_buyback(1.73) == 0
    assert len(calls) == 1
    return {'original_partial_sell':150,'buyback_submitted':calls[0][1],
            'remaining':50,'next_buyback_blocked':True,'log':subject.messages}


def cash_fee_boundary():
    subject = runner('159981.SZ')
    subject._available_cash = lambda:175.2
    allowed = subject._clamp_buy_shares(100,1.75)
    modeled_cost = 1.75*allowed+subject.fee_estimate.fee(1.75,allowed,'BUY',fund=True)
    assert allowed == 100 and modeled_cost > 175.2
    calls = []
    subject._wait_for_fill = lambda *args:('TIMEOUT',0)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strategy,'order_shares',lambda *args:calls.append(args) or 123)
        assert subject._submit_order(100,1.75,'REV-T buyback()',style='FIX') == ('TIMEOUT',0)
    assert len(calls) == 1
    return {'cash':175.2,'price':1.75,'allowed':allowed,'own_fee_model_cost':modeled_cost,
            'insufficient_under_configured_fee_model':True,'submission_path_entered':True,
            'scope':'closing buyback; no new-leg cost check'}


def healthy_fill_control():
    subject = runner()
    fake_order(subject)
    subject._refresh_position = lambda:None
    assert subject._wait_for_fill(-100,'REV-T sell',200) == ('FILLED',-100)
    return {'healthy_confirmed_fill_returns':True}


def main():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strategy.cfg,'now_hms',lambda:'10:00:00')
        evidence = {'stale_tick':stale_tick(),'post_fill_failure':post_fill_failure(),
            'inconsistent_success_status':inconsistent_success_status(),
            'fund_odd_remainder':fund_odd_remainder(),'cash_fee_boundary':cash_fee_boundary(),
            'control':healthy_fill_control()}
    result = {'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              'defects_reproduced':5,'evidence':evidence,
              'scope':'Offline actual methods with injected broker snapshots; no live connection/order.'}
    Path(__file__).with_name('evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    main()
