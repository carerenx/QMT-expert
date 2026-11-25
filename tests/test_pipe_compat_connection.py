"""Cold-import and account-schema checks against the real Pipe bridge."""

from pathlib import Path
import subprocess
import sys
import textwrap


SOURCE = Path(__file__).resolve().parents[1] / 'integrations/bigqmt_pipe/src'


def run_check(code):
    result = subprocess.run(
        [sys.executable, '-c', 'import sys; sys.path.insert(0, %r)\n' % str(SOURCE)
         + textwrap.dedent(code)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_account_infos_provides_miniqmt_names_and_preserves_native_fields():
    run_check('''
        from types import SimpleNamespace
        from xtquant.xttrader import XtQuantTrader
        trader = XtQuantTrader.__new__(XtQuantTrader)
        trader._server_account_type = 'STOCK'
        trader._declared_account_type = ''
        rows = [{'m_strAccountID': '12345', 'm_dBalance': 100.0},
                {'account_id': '67890', 'account_type': 3}]
        trader.client = SimpleNamespace(account_id='12345', call=lambda *a, **k: rows)
        infos = trader.query_account_infos()
        assert infos[0].account_id == '12345'
        assert infos[0].account_type == 2
        assert infos[0].m_dBalance == 100.0
        assert infos[1].account_id == '67890'
        assert infos[1].account_type == 3
        trader.client.call = lambda *a, **k: []
        assert trader.query_account_infos() == []
        trader.client.call = lambda *a, **k: [{'m_dBalance': 100.0}]
        try:
            trader.query_account_infos()
        except ValueError:
            pass
        else:
            raise AssertionError('must not invent an account for a malformed row')
    ''')


def test_transport_package_does_not_eagerly_import_factory():
    run_check('''
        import importlib
        import types
        # Isolate the transport imports from the root package's eager exports.
        root = types.ModuleType('bigqmt_signal_trader')
        root.__path__ = [sys.path[0] + '/bigqmt_signal_trader']
        sys.modules[root.__name__] = root
        package = importlib.import_module('bigqmt_signal_trader.transports')
        assert callable(package.build_transport)
        assert 'bigqmt_signal_trader.transports.factory' not in sys.modules
        importlib.import_module('bigqmt_signal_trader.transports.factory')
        assert 'bigqmt_signal_trader.transports.redis_transport' not in sys.modules
    ''')


def test_cold_factory_and_pipe_imports_can_run_concurrently():
    run_check('''
        import importlib
        import threading
        import types
        root = types.ModuleType('bigqmt_signal_trader')
        root.__path__ = [sys.path[0] + '/bigqmt_signal_trader']
        sys.modules[root.__name__] = root
        errors = []
        barrier = threading.Barrier(2)
        def load(name):
            try:
                barrier.wait(timeout=5)
                importlib.import_module(name)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=load, args=(name,), daemon=True) for name in (
            'bigqmt_signal_trader.transports.factory',
            'bigqmt_signal_trader.transports.pipe_push_channel')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        assert not any(thread.is_alive() for thread in threads), 'import deadlock'
        assert not errors, errors
    ''')
