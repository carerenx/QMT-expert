"""Small, append-only order attribution journal for Redis-free deployments.

Each instance owns a different segment. A segment lock only coordinates an
append with expired-segment cleanup; no file handle survives either operation.
Callbacks continue to use the handlers' in-memory journal, never disk I/O.
"""

import collections
import hashlib
import json
import math
import os
import sys
import time
import uuid


DEFAULT_TTL_SECONDS = 7 * 86400.0
DEFAULT_LIMIT = 5000
_WARNING_INTERVAL_SECONDS = 60.0
_APPEND_LOCK_TIMEOUT_SECONDS = 0.1


def _directory_candidates(directory):
    override = directory or os.environ.get("BIGQMT_ORDER_IDENTITY_DIR")
    if override:
        return [os.path.abspath(os.path.expanduser(str(override)))]
    candidates = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            base = os.path.abspath(entry)
            if (os.path.basename(os.path.normpath(base)).lower() == "python"
                    and os.path.isdir(base)):
                candidate = os.path.join(base, "state", "order_identity")
                if candidate not in candidates:
                    candidates.append(candidate)
        except (OSError, TypeError, ValueError):
            continue
    candidates.append(os.path.join(os.path.expanduser("~"), ".cache",
                                   "bigqmt", "order_identity"))
    return candidates


class OrderIdentityJournal(object):
    """Durable input for an OrderedDict[(account, remark)] = (time, name).

    ``directory`` and BIGQMT_ORDER_IDENTITY_DIR name a base directory. The
    account/type namespace beneath it is hashed, including for explicit paths.
    I/O failures are recorded in ``error`` and never escape into order submit.
    The last error stays visible for the lifetime of this instance: a later
    successful append cannot prove that an earlier startup read recovered all
    identities.
    """

    def __init__(self, account_id, account_type="", directory=None,
                 ttl_seconds=DEFAULT_TTL_SECONDS, limit=DEFAULT_LIMIT, clock=None):
        self.account_id = str(account_id or "")
        self.account_type = str(account_type or "").strip().upper()
        self.ttl_seconds = float(ttl_seconds)
        self.limit = max(1, int(limit))
        self._clock = clock or time.time
        self.error = ""
        self._last_warning_at = None
        namespace = json.dumps([self.account_id, self.account_type],
                               ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        candidates = _directory_candidates(directory)
        self.directory = os.path.join(candidates[-1], digest)
        for candidate in candidates:
            self.directory = os.path.join(candidate, digest)
            try:
                os.makedirs(self.directory, exist_ok=True)
                break
            except OSError as exc:
                if candidate == candidates[-1]:
                    self._failed("create directory", exc)
        self._segment = os.path.join(self.directory, uuid.uuid4().hex + ".jsonl")

    def _failed(self, operation, exc):
        self.error = "%s: %s: %s" % (operation, type(exc).__name__, exc)
        now = time.monotonic()
        if (self._last_warning_at is None
                or now - self._last_warning_at >= _WARNING_INTERVAL_SECONDS):
            self._last_warning_at = now
            try:
                print("[bigqmt_rpc] order identity journal %s; restart recovery "
                      "may be incomplete" % self.error)
            except Exception:
                pass

    def _acquire(self, path, wait=False):
        lock = path + ".lock"
        deadline = time.monotonic() + _APPEND_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(handle)
                return lock
            except FileExistsError:
                if not wait:
                    return None
                if time.monotonic() >= deadline:
                    raise OSError("order identity segment is busy")
                time.sleep(0.005)

    def _release(self, lock):
        try:
            os.remove(lock)
        except OSError as exc:
            self._failed("release segment lock", exc)

    def _record(self, raw):
        try:
            record = json.loads(raw.decode("utf-8"))
            if (not isinstance(record, dict)
                    or record.get("account_id") != self.account_id
                    or record.get("account_type") != self.account_type):
                return None
            remark = record.get("remark")
            name = record.get("strategy_name")
            stamp = float(record["ts"])
            if (not isinstance(remark, str) or not remark.strip()
                    or not isinstance(name, str)
                    or not math.isfinite(stamp) or stamp <= 0):
                return None
            return stamp, remark.strip(), name
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None

    def _read(self, path, cutoff):
        records = []
        fully_expired = True
        saw_record = False
        try:
            with open(path, "rb") as handle:
                for number, raw in enumerate(handle):
                    # A crash can leave a partial last write. Do not interpret
                    # even valid JSON as committed without its final newline.
                    record = self._record(raw) if raw.endswith(b"\n") else None
                    if record is None:
                        fully_expired = False
                        continue
                    saw_record = True
                    stamp, remark, name = record
                    if stamp >= cutoff:
                        fully_expired = False
                        records.append((stamp, os.path.basename(path), number,
                                        remark, name))
        except FileNotFoundError:
            return [], False
        except OSError as exc:
            self._failed("read segment", exc)
            fully_expired = False
        return records, saw_record and fully_expired

    def _remove_if_expired(self, path, cutoff):
        lock = None
        try:
            lock = self._acquire(path)
            if lock is None:
                return
            # Recheck while append is excluded: the initial read alone is not
            # evidence that another process did not append a fresh row since.
            _, fully_expired = self._read(path, cutoff)
            if fully_expired:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            self._failed("remove expired segment", exc)
        finally:
            if lock is not None:
                self._release(lock)

    def load(self):
        """Read segments once at startup; replay first named ownership wins."""
        journal = collections.OrderedDict()
        cutoff = float(self._clock()) - self.ttl_seconds
        records = []
        try:
            os.makedirs(self.directory, exist_ok=True)
            paths = [os.path.join(self.directory, name)
                     for name in os.listdir(self.directory)
                     if name.endswith(".jsonl")]
            for path in sorted(paths):
                found, fully_expired = self._read(path, cutoff)
                records.extend(found)
                if fully_expired:
                    self._remove_if_expired(path, cutoff)
            for stamp, _, _, remark, name in sorted(records):
                key = (self.account_id, remark)
                existing = journal.get(key)
                if existing is not None and existing[1] and existing[1] != name:
                    continue
                journal[key] = (stamp, name)
                journal.move_to_end(key)
            while len(journal) > self.limit:
                journal.popitem(last=False)
        except OSError as exc:
            self._failed("load", exc)
        return journal

    def remember(self, account_id, remark, ts, name):
        """Append and fsync one fact. Return False on failure, never raise."""
        lock = None
        try:
            if str(account_id or "") != self.account_id:
                raise ValueError("journal account does not match")
            remark = str(remark or "").strip()
            stamp = float(ts)
            if not remark or not math.isfinite(stamp) or stamp <= 0:
                raise ValueError("journal requires a remark and finite positive time")
            payload = json.dumps({
                "account_id": self.account_id,
                "account_type": self.account_type,
                "remark": remark,
                "ts": stamp,
                "strategy_name": str(name or ""),
            }, ensure_ascii=True, separators=(",", ":")) + "\n"
            os.makedirs(self.directory, exist_ok=True)
            lock = self._acquire(self._segment, wait=True)
            with open(self._segment, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except Exception as exc:
            self._failed("append", exc)
            # A failed write may have left half a line. A subsequent successful
            # append must not concatenate onto it and become unreadable too.
            self._segment = os.path.join(self.directory, uuid.uuid4().hex + ".jsonl")
            return False
        finally:
            if lock is not None:
                self._release(lock)
