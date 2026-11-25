"""Atomic, single-writer execution checkpoints; no broker credentials stored."""
import io
import json
import math
import os
from pathlib import Path

import pandas as pd


def validate_runner_state(state):
    st = state['st']
    valid_states = {'IDLE', 'SPIKING', 'SOLD', 'DIPPING', 'DONE',
                    'BT_DIPPING', 'BT_BOUGHT', 'BT_SPIKING'}
    if st['fstate'] not in valid_states:
        raise ValueError('invalid checkpoint state')
    for key in ('total_pnl', 'feedback_cash', 'feedback_shares', 'total_t_days'):
        if not math.isfinite(float(state[key])):
            raise ValueError('non-finite checkpoint counter')
    for group, legs in state['legs'].items():
        for price, shares in legs:
            if (not math.isfinite(float(price)) or price <= 0 or
                    not isinstance(shares, int) or shares <= 0):
                raise ValueError('invalid checkpoint execution leg')
    for group, key in (('SHORT', 'short_legs'), ('LONG', 'long_legs')):
        recorded = [list(leg) for leg in state['legs'].get(group, [])]
        tracked = [list(leg) for leg in st.get(key, [])]
        if recorded != tracked:
            raise ValueError('checkpoint state/ledger leg mismatch')
    if st['fstate'] in ('SOLD', 'DIPPING') and not st.get('short_legs'):
        raise ValueError('checkpoint short state has no execution leg')
    if st['fstate'] in ('BT_BOUGHT', 'BT_SPIKING') and not st.get('long_legs'):
        raise ValueError('checkpoint long state has no execution leg')
    pending = state['pending']
    if pending:
        if (not isinstance(pending['signed'], int) or pending['signed'] == 0 or
                not math.isfinite(pending['price']) or pending['price'] <= 0 or
                not math.isfinite(pending['submitted_at'])):
            raise ValueError('invalid checkpoint pending order')
        label = pending['label']
        if label not in ('REV-T sell', 'FWD-T buy', 'FWD-T sell') and not label.startswith('REV-T buyback'):
            raise ValueError('invalid pending order purpose')


def encode(value):
    if isinstance(value, pd.DataFrame):
        return {'__dataframe__': value.to_json(orient='split', double_precision=15)}
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode(item) for item in value]
    if hasattr(value, 'item'):
        return encode(value.item())
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'__dataframe__'}:
            return pd.read_json(io.StringIO(value['__dataframe__']), orient='split')
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


class ExecutionJournal:
    def __init__(self, path, account):
        self.path = Path(path)
        self.account = str(account)
        self.lock = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path) + '.lock', 'a+b')
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            handle.close()
            raise RuntimeError('another process owns the execution journal')
        self.lock = handle

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def read(self):
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or str(data.get('account')) != self.account:
            raise ValueError('execution journal schema/account mismatch')
        return decode(data)

    def write(self, payload):
        if self.lock is None:
            raise RuntimeError('execution journal lock not held')
        data = dict(payload, schema=1, account=self.account)
        content = json.dumps(encode(data), ensure_ascii=False, allow_nan=False)
        temporary = Path(str(self.path) + '.tmp-' + str(os.getpid()))
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
