# coding: utf-8
# Reference configuration for the QMT-side model. In the deployed single-file
# model, edit its top configuration block; this separate file is not loaded.
BIGQMT_ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BIGQMT_ACCOUNT_TYPE = "STOCK"
BIGQMT_REDIS_CONFIG = {
    "transport": "pipe",
    "redis_enabled": False,
    "pipe": {"pipe_name": "bigqmt_rpc", "connect_timeout_seconds": 5.0},
    "rpc_allow_order_methods": False,
    "native_xtdata_enabled": False,
    "rpc_process_in_listener": True,
    "rpc_listener_methods": ("*",),
    "rpc_background_threads": False,
    "schedule_adjust": True,
    "schedule_adjust_interval": "100nMilliSecond",
    "quote_push": {"enabled": False},
    "download_jobs_enabled": False,
    "full_tick_cache_enabled": False,
    "exec_events_enabled": True,
}
