"""Exercise DayT's real connection path without QMT, threads or orders."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


def test_callback_is_registered_without_becoming_account_id(monkeypatch):
    account_id = '1234567890'
    events = []

    class Trader:
        # Pipe compatibility layer interprets the third argument as account_id.
        def __init__(self, path, session_id, account_id=None):
            self.account_id = account_id or '1234567890'
            self.callback = None

        def register_callback(self, callback):
            self.callback = callback
            events.append('registered')

        def start(self):
            assert self.callback is not None
            events.append('started')

        def connect(self):
            assert self.account_id == account_id
            events.append('connected')
            return 0

        def query_account_infos(self):
            return [SimpleNamespace(account_id=account_id)]

        def subscribe(self, account):
            assert account.account_id == account_id
            events.append('subscribed')

    core = ModuleType('core')
    core.config = SimpleNamespace(ACCOUNT=account_id, MINIQMT_PATH='test', SESSION_ID=1)
    monkeypatch.setitem(sys.modules, 'core', core)
    xtquant = ModuleType('xtquant')
    xtquant.xttrader = SimpleNamespace(XtQuantTrader=Trader, XtQuantTraderCallback=object)
    xtquant.xtconstant = SimpleNamespace()
    monkeypatch.setitem(sys.modules, 'xtquant', xtquant)
    package = ModuleType('_dayt_connection_test')
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    logger = ModuleType(package.__name__ + '.logger')
    logger._log = lambda message: None
    monkeypatch.setitem(sys.modules, logger.__name__, logger)
    path = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT/infra/connector.py'
    spec = importlib.util.spec_from_file_location(package.__name__ + '.connector', path)
    connector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(connector)
    monkeypatch.setattr(connector._time, 'sleep', lambda seconds: None)
    connection = connector.MiniQMTConnector()

    assert connection.connect_trade(account_id=account_id, path='test', session=1)
    assert connection.trader.callback is connection.callback
    assert connection.trader.account_id == account_id
    assert events == ['registered', 'started', 'connected', 'subscribed']
