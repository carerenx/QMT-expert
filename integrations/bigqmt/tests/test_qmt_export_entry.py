"""QMT-export entry tests: no QMT terminal or real account access."""

from pathlib import Path
import subprocess
import sys
import shutil
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations/bigqmt_pipe/src"))

import run_bigqmt as entry
from bigqmt_signal_trader import external_strategy_launcher as launcher
from bigqmt_signal_trader import xtquant_compat as compat


def test_alternate_bridge_launches_with_its_own_shim_and_config(tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    for package in ("bigqmt_signal_trader", "xtquant"):
        shutil.copytree(ROOT / "integrations/bigqmt/src" / package,
                        bridge / package, ignore=shutil.ignore_patterns("__pycache__"))
    (bridge / "bigqmt_signal_trader/external_strategy_launcher.py").unlink()
    (bridge / "bigqmt_signal_trader_client_config.py").write_text(
        "BIGQMT_ACCOUNT_ID = 'test-account'\n"
        "BIGQMT_REDIS_CONFIG = {'transport': 'zmq'}\n", encoding="utf-8")
    strategy = tmp_path / "strategy.py"
    (tmp_path / "config.py").write_text("ACCOUNT = 'test-account'\n", encoding="utf-8")
    strategy.write_text(
        "import sys, xtquant\n"
        "from pathlib import Path\n"
        "from bigqmt_signal_trader.xtquant_compat import get_default_client\n"
        "assert Path(xtquant.__file__).resolve().parent == Path(%r) / 'xtquant'\n"
        "assert get_default_client().account_id == 'test-account'\n"
        "assert sys.argv[1:] == ['--mode', 'signal']\n"
        "print('alternate bridge strategy OK')\n" % str(bridge), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "run_bigqmt.py"), "--bridge-src", str(bridge),
         "--strategy", str(strategy), "--mode", "signal"],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "alternate bridge strategy OK" in result.stdout


@pytest.fixture
def strategy(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core/config.py").write_text("ACCOUNT = 'test-account'\n")
    file = tmp_path / "strategy.py"
    file.write_text("raise AssertionError('strategy must not execute during checks')\n")
    return file


@pytest.fixture
def config():
    with mock.patch.object(compat, "load_client_config", return_value={
        "account_id": "test-account", "redis_config": {"transport": "zmq"},
    }):
        yield


def test_check_never_connects_or_launches(strategy, config):
    with mock.patch.object(compat, "configure") as configure, mock.patch.object(launcher, "launch_strategy") as launch:
        assert entry.main(["--strategy", str(strategy), "--check"]) == 0
        configure.assert_not_called()
        launch.assert_not_called()


def test_forwards_strategy_arguments(strategy, config):
    with mock.patch.object(launcher, "launch_strategy") as launch:
        assert entry.main(["--strategy", str(strategy), "--", "--mode", "signal"]) == 0
        launch.assert_called_once_with(strategy.resolve(), ["--mode", "signal"])


def test_account_mismatch_stops_before_strategy(strategy):
    with mock.patch.object(compat, "load_client_config", return_value={"account_id": "wrong"}), mock.patch.object(launcher, "launch_strategy") as launch:
        with pytest.raises(SystemExit) as exc:
            entry.main(["--strategy", str(strategy)])
        assert exc.value.code == 2
        launch.assert_not_called()


def test_probe_uses_only_read_methods(strategy, config):
    trader = mock.Mock()
    trader.client.call.side_effect = [
        {"pong": True, "account_id": "test-account"}, {"cash": 12000}, [],
    ]
    with mock.patch.object(compat, "configure", return_value=(trader, None)), mock.patch.object(launcher, "launch_strategy") as launch:
        assert entry.main(["--strategy", str(strategy), "--probe"]) == 0
        assert trader.client.call.call_args_list == [
            mock.call("ping"), mock.call("query_stock_asset"), mock.call("query_stock_positions"),
        ]
        launch.assert_not_called()


@pytest.mark.parametrize("reply", [TimeoutError("RPC unavailable"), {"pong": True, "account_id": "wrong"}])
def test_probe_failure_does_not_query_account_or_launch_strategy(strategy, config, reply, capsys):
    trader = mock.Mock()
    if isinstance(reply, Exception):
        trader.client.call.side_effect = reply
    else:
        trader.client.call.return_value = reply
    with mock.patch.object(compat, "configure", return_value=(trader, None)), mock.patch.object(launcher, "launch_strategy") as launch:
        with pytest.raises(SystemExit) as exc:
            entry.main(["--strategy", str(strategy), "--probe"])
        assert exc.value.code == 1
        trader.client.call.assert_called_once_with("ping")
        launch.assert_not_called()
        assert "Start the bridge inside Big QMT" in capsys.readouterr().err


def test_native_sdk_already_loaded_is_rejected(strategy):
    fake_sdk = mock.Mock(__file__=str(strategy.parent / "xtquant/__init__.py"))
    with mock.patch.dict(sys.modules, {"xtquant": fake_sdk}):
        with pytest.raises(SystemExit) as exc:
            entry.main(["--strategy", str(strategy), "--check"])
        assert exc.value.code == 2


def test_entry_can_run_from_another_working_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "run_bigqmt.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--probe" in result.stdout
    assert entry.DEFAULT_STRATEGY.is_file()


def test_template_pair_matches_and_disables_orders():
    import runpy
    src = entry.BRIDGE_SRC
    client = runpy.run_path(str(src / "bigqmt_signal_trader_client_config.example.py"))
    server = runpy.run_path(str(src / "bigqmt_signal_trader_local_config.example.py"))
    assert client["BIGQMT_ACCOUNT_ID"] == server["BIGQMT_ACCOUNT_ID"]
    assert client["BIGQMT_REDIS_CONFIG"]["transport"] == server["BIGQMT_REDIS_CONFIG"]["transport"] == "pipe"
    assert client["BIGQMT_REDIS_CONFIG"]["pipe"]["pipe_name"] == server["BIGQMT_REDIS_CONFIG"]["pipe"]["pipe_name"]
    assert client["BIGQMT_REDIS_CONFIG"]["redis_enabled"] is False
    assert server["BIGQMT_REDIS_CONFIG"]["redis_enabled"] is False
    assert server["BIGQMT_REDIS_CONFIG"]["rpc_allow_order_methods"] is False
    assert client["BIGQMT_LOCAL_CACHE_CONFIG"]["fallback_rpc"] is True


def test_existing_connector_reads_account_and_positions_through_bridge(monkeypatch):
    monkeypatch.syspath_prepend(str(entry.DEFAULT_STRATEGY.parent))
    monkeypatch.syspath_prepend(str(entry.DEFAULT_STRATEGY.parent.parent))
    from infra import connector
    account = connector.cfg.ACCOUNT
    responses = {
        "ping": {"pong": True, "account_id": account},
        "query_account_infos": [{"account_id": account, "account_type": 2}],
        "query_stock_asset": {"cash": 12000.0, "total_asset": 52000.0},
        "query_stock_positions": [{"stock_code": "601869.SH", "volume": 200,
                                   "can_use_volume": 100}],
    }

    def rpc(method, *args, **kwargs):
        return responses[method]

    with mock.patch.object(compat, "load_client_config", return_value={"account_id": account, "redis_config": {"transport": "zmq"}}), mock.patch.object(compat.BigQmtRpcClient, "call", side_effect=rpc) as calls, mock.patch.object(compat.BigQmtXtTrader, "_start_event_listener"), mock.patch.object(connector, "_log"):
        conn = connector.MiniQMTConnector()
        try:
            assert conn.connect_data()
            assert conn.connect_trade(account_id=account)
            assert conn.query_account().cash == 12000.0
            positions = conn.query_positions()
            assert positions[0].stock_code == "601869.SH"
            assert positions[0].volume == 200
            assert positions[0].can_use_volume == 100
            assert {call.args[0] for call in calls.call_args_list} == set(responses)
        finally:
            conn.disconnect()
