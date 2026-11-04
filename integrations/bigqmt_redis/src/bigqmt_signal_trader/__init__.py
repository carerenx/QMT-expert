"""可替换的大 QMT 信号下单包核心模块。"""

__version__ = "0.2.0"

from .app import SignalTradingApp
from .models import (
    AccountSnapshot,
    AssetSnapshot,
    OrderRequest,
    OrderSubmitResult,
    PositionSnapshot,
    SignalAction,
    SignalStatus,
    TradeSignal,
)
from .xtquant_compat import BigQmtRpcClient, BigQmtXtData, BigQmtXtTrader

__all__ = [
    "AccountSnapshot",
    "AssetSnapshot",
    "BigQmtRpcClient",
    "BigQmtXtData",
    "BigQmtXtTrader",
    "OrderRequest",
    "OrderSubmitResult",
    "PositionSnapshot",
    "SignalAction",
    "SignalStatus",
    "SignalTradingApp",
    "TradeSignal",
    "__version__",
]
