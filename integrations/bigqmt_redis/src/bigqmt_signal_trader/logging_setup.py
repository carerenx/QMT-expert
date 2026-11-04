"""File-based logging for the Big QMT bridge.

Diagnostics used to be print()-only, which is lost when the QMT output panel
scrolls or the terminal restarts. This module wires Python's stdlib ``logging``
to a rotating file so errors survive restarts and can be reviewed after a
crash.

Usage (any module, both server and client):

    from bigqmt_signal_trader.logging_setup import get_logger
    log = get_logger("rpc")
    log.info("started")
    log.error("download failed: %s", exc)

Behavior:
- Log directory resolves to ``<qmt_python_dir>/logs`` when running inside QMT
  (found via a sys.path entry ending in ``\\python``), else ``~/.cache/bigqmt/logs``.
- Rotates at midnight into ``bigqmt.log.YYYY-MM-DD`` backups, keeping the last
  7 days by default (override with env BIGQMT_LOG_RETENTION_DAYS).
- Each record is also printed to stdout so the QMT output panel still shows it.
- Thread-safe (logging is; the print side is best-effort wrapped).
- Never raises: a logging failure must not bring down the strategy.
- Opt out via env BIGQMT_LOG_ENABLED=0 / BIGQMT_LOG_TO_STDOUT=0.
"""

import datetime as _dt
import logging
import logging.handlers
import os
import sys
import time

_LOGGER_NAME = "bigqmt"
_initialized = False


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve_log_dir():
    """Pick a writable log dir. Prefers the QMT python dir (the sys.path entry
    ending in ``\\python``) so logs sit beside the deployed strategy; falls back
    to a user cache dir otherwise. Deliberately does NOT use this package's own
    src/ directory (a repo checkout is not a writable runtime location)."""
    candidates = []
    for entry in sys.path:
        try:
            if entry and entry.endswith(r"\python") and os.path.isdir(entry):
                candidates.append(entry)
        except Exception:
            continue
    candidates.append(os.path.join(os.path.expanduser("~"), ".cache", "bigqmt"))
    for base in candidates:
        try:
            path = os.path.join(base, "logs")
            os.makedirs(path, exist_ok=True)
            # probe writability
            probe = os.path.join(path, ".write_test")
            with open(probe, "w"):
                pass
            try:
                os.remove(probe)
            except Exception:
                pass
            return path
        except Exception:
            continue
    return None


class _SafeStreamHandler(logging.Handler):
    """print() the record so the QMT output panel shows it; never raises."""

    def emit(self, record):
        try:
            print(self.format(record))
        except Exception:
            pass


def _cleanup_old_logs(log_dir, retention_days):
    """Delete rotated log files older than retention_days.

    TimedRotatingFileHandler only prunes backups at rotation time; this sweeps
    stale files on startup too (e.g. after a weekend gap or a config change).
    """
    try:
        cutoff = time.time() - retention_days * 86400
        for name in os.listdir(log_dir):
            if not (name.startswith("bigqmt") and name.endswith(".log") or ".log." in name):
                continue
            path = os.path.join(log_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                continue
    except Exception:
        pass


def _setup():
    global _initialized
    if _initialized:
        return
    _initialized = True
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not _env_bool("BIGQMT_LOG_ENABLED", True):
        logger.addHandler(logging.NullHandler())
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler: rotate at midnight, keep the last 7 days only.
    log_dir = _resolve_log_dir()
    if log_dir is not None:
        try:
            fname = os.path.join(log_dir, "bigqmt.log")
            file_handler = logging.handlers.TimedRotatingFileHandler(
                fname,
                when="midnight",
                interval=1,
                backupCount=int(os.environ.get("BIGQMT_LOG_RETENTION_DAYS", 7)),
                encoding="utf-8",
                utc=False,
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
            _cleanup_old_logs(log_dir, int(os.environ.get("BIGQMT_LOG_RETENTION_DAYS", 7)))
        except Exception:
            pass

    # Stdout handler so the QMT panel still shows logs.
    if _env_bool("BIGQMT_LOG_TO_STDOUT", True):
        stream = _SafeStreamHandler()
        stream.setLevel(logging.INFO)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())


def get_logger(name=""):
    """Return a module logger under the shared ``bigqmt`` root.

    ``get_logger("rpc")`` -> logger named ``bigqmt.rpc``; the tag is shown in
    each log line so the old ``[bigqmt_rpc]`` prefixes remain visible.
    """
    _setup()
    suffix = str(name or "").strip(".")
    full = _LOGGER_NAME if not suffix else "%s.%s" % (_LOGGER_NAME, suffix)
    return logging.getLogger(full)


def log_file_path():
    """Return the current log file path (or None if file logging is off)."""
    _setup()
    log_dir = _resolve_log_dir()
    if log_dir is None:
        return None
    return os.path.join(log_dir, "bigqmt.log")
