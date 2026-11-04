from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bigqmt_signal_trader.external_strategy_launcher import (
    ExternalStrategyLaunchError,
    _load_strategy_account,
    launch_strategy,
)


class ExternalStrategyLauncherTest(unittest.TestCase):
    def _strategy_tree(self, root, account="acct-v40"):
        strategy_root = Path(root) / "MiniQMT_Stragety"
        (strategy_root / "core").mkdir(parents=True)
        (strategy_root / "core" / "config.py").write_text(
            "ACCOUNT = %r\n" % account, encoding="utf-8"
        )
        strategy = strategy_root / "strategy.py"
        strategy.write_text("RESULT = 'ran'\n", encoding="utf-8")
        return strategy

    def test_loads_account_from_sibling_core_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy = self._strategy_tree(tmp, "8899")
            self.assertEqual(_load_strategy_account(strategy), "8899")

    def test_loads_account_from_strategy_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy = self._strategy_tree(tmp, "parent-account")
            strategy = strategy.parent / "A144" / "strategy.py"
            strategy.parent.mkdir()
            strategy.write_text("RESULT = 'ran'\n", encoding="utf-8")
            (strategy.parent / "config.py").write_text(
                "ACCOUNT = 'a144-account'\n", encoding="utf-8"
            )
            self.assertEqual(_load_strategy_account(strategy), "a144-account")

    def test_loads_account_when_strategy_is_in_a_category_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy_root = Path(tmp) / "MiniQMT_Stragety"
            (strategy_root / "core").mkdir(parents=True)
            (strategy_root / "core" / "config.py").write_text(
                "ACCOUNT = 'nested-account'\n", encoding="utf-8"
            )
            strategy = strategy_root / "DayT" / "strategy.py"
            strategy.parent.mkdir()
            strategy.write_text("RESULT = 'ran'\n", encoding="utf-8")

            self.assertEqual(_load_strategy_account(strategy), "nested-account")

    def test_missing_strategy_fails_before_bridge_configuration(self):
        missing = Path(tempfile.gettempdir()) / "missing-qmt-strategy.py"
        with self.assertRaisesRegex(ExternalStrategyLaunchError, "does not exist"):
            launch_strategy(missing)

    def test_launch_configures_strategy_account_and_forwards_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy = self._strategy_tree(tmp)
            client = mock.Mock(account_id="acct-v40")
            trader = mock.Mock(client=client)
            with mock.patch(
                "bigqmt_signal_trader.xtquant_compat.configure",
                return_value=(trader, mock.Mock()),
            ) as configure, mock.patch(
                "bigqmt_signal_trader.external_strategy_launcher._assert_project_xtquant_shim"
            ), mock.patch(
                "bigqmt_signal_trader.external_strategy_launcher.runpy.run_path",
                return_value={"RESULT": "ran"},
            ) as run_path:
                result = launch_strategy(strategy, ["--mode", "signal"])

            configure.assert_called_once_with(account_id="acct-v40")
            run_path.assert_called_once_with(str(strategy.resolve()), run_name="__main__")
            self.assertEqual(result["RESULT"], "ran")

    def test_launch_reports_missing_strategy_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy = self._strategy_tree(tmp)
            trader = mock.Mock(client=mock.Mock(account_id="acct-v40"))
            missing = ModuleNotFoundError("No module named 'numpy'", name="numpy")
            with mock.patch(
                "bigqmt_signal_trader.xtquant_compat.configure",
                return_value=(trader, mock.Mock()),
            ), mock.patch(
                "bigqmt_signal_trader.external_strategy_launcher._assert_project_xtquant_shim"
            ), mock.patch(
                "bigqmt_signal_trader.external_strategy_launcher.runpy.run_path",
                side_effect=missing,
            ):
                with self.assertRaisesRegex(
                    ExternalStrategyLaunchError, "dependency 'numpy'"
                ):
                    launch_strategy(strategy)

    def test_entry_moves_project_src_ahead_of_an_installed_xtquant(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_site = Path(tmp)
            fake_xtquant = fake_site / "xtquant"
            fake_xtquant.mkdir()
            (fake_xtquant / "__init__.py").write_text("", encoding="utf-8")
            src = str(ROOT / "src")
            entry = str(ROOT / "examples" / "run_daytrading_v40_bigqmt.py")
            code = (
                "import runpy,sys;"
                "src=%r;fake=%r;entry=%r;"
                "sys.path.remove(src) if src in sys.path else None;"
                "sys.path[:0]=[fake,src];"
                "runpy.run_path(entry,run_name='_entry_import_test');"
                "import xtquant;print(xtquant.__file__)"
            ) % (src, str(fake_site), entry)

            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            loaded = Path(result.stdout.strip()).resolve()
            self.assertEqual(loaded.parent, (ROOT / "src" / "xtquant").resolve())

    def test_v41_entry_selects_v41_and_forwards_strategy_arguments(self):
        from examples import run_daytrading_v41_bigqmt as entry

        with mock.patch.object(entry, "_main", return_value=0) as launcher:
            result = entry.main(
                ["--mode", "backtest", "--start", "20260101", "--end", "20260131"]
            )

        self.assertEqual(result, 0)
        launcher.assert_called_once_with([
            "--strategy",
            str(entry.V41_STRATEGY),
            "--mode",
            "backtest",
            "--start",
            "20260101",
            "--end",
            "20260131",
        ])


if __name__ == "__main__":
    unittest.main()
