"""Optional xtquant import shim backed by Big QMT Redis RPC.

Put this package before the real xtquant package on PYTHONPATH only when the
caller intentionally wants Big QMT RPC compatibility.
"""

# xtconstant / xttype are pure definitions and safe to import eagerly.
#
# xtdata and xttrader are NOT: both reach back into
# bigqmt_signal_trader.xtquant_compat, which itself does
# ``from xtquant.xtconstant import *``. Importing them here closes the loop --
# ``import bigqmt_signal_trader`` then fails with "partially initialized
# module", and only in that direction, so whether it breaks depends on which
# package the caller happens to import first.
#
# Resolved lazily through __getattr__ (PEP 562): ``xtquant.xtdata`` and
# ``from xtquant import xttrader`` both still work, but the import runs after
# xtquant_compat has finished initialising rather than in the middle of it.
from . import xtconstant, xttype

__all__ = ["xtconstant", "xtdata", "xttrader", "xttype"]

_LAZY_SUBMODULES = ("xtdata", "xttrader")


def __getattr__(name):
    if name in _LAZY_SUBMODULES:
        import importlib

        module = importlib.import_module("." + name, __name__)
        globals()[name] = module      # resolve once, then it is a plain attribute
        return module
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(set(list(globals()) + list(_LAZY_SUBMODULES)))
