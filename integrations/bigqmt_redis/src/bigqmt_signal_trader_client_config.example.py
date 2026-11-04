# coding: utf-8
"""Copy to bigqmt_signal_trader_client_config.py; match the QMT server account."""
BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BIGQMT_RPC_TIMEOUT_SECONDS = 6.0
BIGQMT_REDIS_CONFIG = {
    "transport": "zmq",
    "zmq": {"host": "127.0.0.1", "port": 15700,
            "connect_address": "tcp://127.0.0.1:15700"},
}
BIGQMT_FULL_TICK_CACHE_CONFIG = {"enabled": False}
BIGQMT_LOCAL_CACHE_CONFIG = {"enabled": True, "fallback_rpc": True}
BIGQMT_FORMULA_SERVER_CONFIG = {"enabled": False}
