# coding: utf-8

from pathlib import Path

_account_lines = (Path(__file__).resolve().parents[3] / "trading_account.txt").read_text(encoding="utf-8-sig").splitlines()
_accounts = [line.split("#", 1)[0].strip() for line in _account_lines]
_accounts = [account for account in _accounts if account]
if len(_accounts) != 1 or not _accounts[0].isascii() or not _accounts[0].isdigit():
    raise ValueError("trading_account.txt must contain exactly one uncommented numeric account ID")
BIGQMT_ACCOUNT_ID = _accounts[0]
BIGQMT_RPC_TIMEOUT_SECONDS = 30.0

BIGQMT_REDIS_CONFIG = {
    "transport": "pipe",
    "redis_enabled": False,
    "pipe": {
        "pipe_name": "bigqmt_rpc",
        "connect_timeout_seconds": 5.0,
    },
}

# 关闭 FormulaServer 直连，让查询走管道桥接。
BIGQMT_FORMULA_SERVER_CONFIG = {
    "enabled": False,
}

BIGQMT_FULL_TICK_CACHE_CONFIG = {
    "enabled": False,
}
BIGQMT_LOCAL_CACHE_CONFIG = {"enabled": True, "fallback_rpc": True}
