"""No-MOM variants must change only the existing enable switch."""
import ast
import inspect
from pathlib import Path
import unittest

from backtest.dayt_registry import STRATEGIES


class NoMomVariantsTests(unittest.TestCase):
    def test_v39_nomom_loop_yields_to_offline_clock(self):
        from output.analysis.compare_v51_v39_minute import load_strategy
        self.assertTrue(inspect.isgeneratorfunction(load_strategy('v39_nomom').StrategyRunner.run))

    def test_only_mom_switch_changes(self):
        root = Path(__file__).resolve().parents[1] / 'Stragety/MiniQMT_Stragety/DayT'
        for version in ('v39', 'v51', 'v52'):
            with self.subTest(version=version):
                trees = []
                for key, enabled in ((version, True), (version + '_nomom', False)):
                    tree = ast.parse((root / STRATEGIES[key]).read_text(encoding='utf-8'))
                    switches = [node for node in tree.body if isinstance(node, ast.Assign)
                                and any(isinstance(t, ast.Name) and t.id == 'MOM_ENABLED'
                                        for t in node.targets)]
                    self.assertEqual(len(switches), 1)
                    self.assertIs(switches[0].value.value, enabled)
                    switches[0].value = ast.Constant(True)
                    trees.append(ast.dump(tree))
                self.assertEqual(*trees)
