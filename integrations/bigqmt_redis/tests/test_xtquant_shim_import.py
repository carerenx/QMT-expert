"""The xtquant shim must not form an import cycle with xtquant_compat.

xtquant_compat does ``from xtquant.xtconstant import *``, so anything
xtquant/__init__ imports eagerly runs while xtquant_compat is still half
initialised. xtdata and xttrader both reach back into it, which closes the loop.

The failure is direction-dependent -- ``import xtquant`` first works, ``import
bigqmt_signal_trader`` first raises -- so it survives casual testing and shows
up only in whichever process happens to import the other package first. These
tests run each direction in a clean interpreter.
"""

import os
import subprocess
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def _run(code):
    """Execute in a fresh interpreter: import order is the whole point here."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=SRC),
    )


class ImportCycleTest(unittest.TestCase):
    def test_importing_the_package_first_works(self):
        """The direction that broke: bigqmt_signal_trader before xtquant."""
        result = _run("import bigqmt_signal_trader; print('ok')")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_importing_xtquant_first_works(self):
        result = _run("import xtquant; import bigqmt_signal_trader; print('ok')")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compat_import_alone_works(self):
        result = _run(
            "from bigqmt_signal_trader.xtquant_compat import BigQmtXtTrader; print('ok')")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_init_does_not_eagerly_import_the_shims(self):
        """Eager import of either shim re-creates the cycle. Assert on
        sys.modules rather than on the source, so the guarantee holds however
        __init__ is written."""
        result = _run(
            "import xtquant, sys;"
            "print('xtdata' if 'xtquant.xtdata' in sys.modules else 'lazy');"
            "print('xttrader' if 'xtquant.xttrader' in sys.modules else 'lazy')")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["lazy", "lazy"])


class ShimSurfaceTest(unittest.TestCase):
    """Laziness must not change how callers reach the shims -- the whole point
    of this package is that unmodified ``from xtquant import xtdata`` code
    keeps working."""

    def test_attribute_access(self):
        result = _run("import xtquant; print(xtquant.xtdata.__name__)")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("xtquant.xtdata", result.stdout)

    def test_from_import(self):
        result = _run(
            "from xtquant import xtdata, xttrader, xtconstant, xttype;"
            "print(callable(xtdata.get_full_tick), hasattr(xttype, 'XtAsset'),"
            "      xtconstant.STOCK_BUY)")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("True True 23", result.stdout)

    def test_submodule_import(self):
        result = _run("import xtquant.xttrader; print('ok')")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dir_lists_the_lazy_submodules(self):
        result = _run(
            "import xtquant;"
            "print(all(n in dir(xtquant) for n in "
            "('xtconstant', 'xtdata', 'xttrader', 'xttype')))")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("True", result.stdout)

    def test_unknown_attribute_still_raises(self):
        result = _run(
            "import xtquant\n"
            "try:\n"
            "    xtquant.nope\n"
            "except AttributeError:\n"
            "    print('raised')\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raised", result.stdout)


class ConstantValueTest(unittest.TestCase):
    """The port added 443 constants; the 90 that already existed must keep
    their values. A silently changed STOCK_BUY would misroute live orders."""

    KNOWN = {
        "STOCK_BUY": 23, "STOCK_SELL": 24,
        "ORDER_UNREPORTED": 48, "ORDER_SUCCEEDED": 56, "ORDER_JUNK": 57,
        "FIX_PRICE": 11,
        "SECURITY_ACCOUNT": 2, "CREDIT_ACCOUNT": 3, "FUTURE_ACCOUNT": 1,
    }

    def test_known_values_are_unchanged(self):
        from xtquant import xtconstant

        for name, expected in self.KNOWN.items():
            self.assertEqual(getattr(xtconstant, name), expected, name)

    def test_compat_reexports_the_same_values(self):
        from bigqmt_signal_trader import xtquant_compat
        from xtquant import xtconstant

        for name, expected in self.KNOWN.items():
            self.assertEqual(getattr(xtquant_compat, name), expected, name)
            self.assertEqual(getattr(xtquant_compat, name),
                             getattr(xtconstant, name), name)


if __name__ == "__main__":
    unittest.main()
