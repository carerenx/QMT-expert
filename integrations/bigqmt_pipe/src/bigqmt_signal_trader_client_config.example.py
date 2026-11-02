# coding: utf-8

BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
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
