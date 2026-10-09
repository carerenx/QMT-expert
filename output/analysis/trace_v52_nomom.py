"""Read-only instrumentation of the frozen minute replay; no strategy changes."""
import functools
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from backtest import dayt_strict as engine


def main():
    dest = ROOT / 'analysis/v52_nomom_trace_20260915'
    if dest.exists():
        raise FileExistsError(dest)
    reference = json.loads((ROOT / 'analysis/nomom_20260914/verified/v52_nomom_5bp.json').read_text(encoding='utf-8'))
    current_hashes = {}
    for name, digest in reference['hashes'].items():
        current_hashes[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if name not in ('analysis/compare_v51_v39_minute.py',
                        'Stragety/MiniQMT_Stragety/DayT/DayT_v52_nomom.py'):
            assert current_hashes[name] == digest, name
    records, messages = [], []
    original_load = engine.load_strategy

    def load(version):
        mod = original_load(version)
        cls = mod.StrategyRunner
        original_log = cls._log

        def log(self, message):
            messages.append({'time': engine.Clock.current.isoformat(), 'message': str(message)})
            return original_log(self, message)

        cls._log = log
        for name in ('_handle_idle', '_handle_spiking', '_handle_sold', '_handle_dipping'):
            original = getattr(cls, name)

            def make_wrapper(method, label):
                @functools.wraps(method)
                def wrapped(self, price):
                    stamp = engine.Clock.current.isoformat()
                    before = self.st.get('fstate')
                    result = method(self, price)
                    if stamp[:10] >= '2026-08-06':
                        signal = self.st.get('daily_signal') or {}
                        records.append(dict(time=stamp, method=label, price=price, before=before,
                            after=self.st.get('fstate'), sell=self.st.get('sell_fill_price'),
                            target=self.st.get('buyback_target'), elapsed=self.st.get('sell_elapsed_bars'),
                            atr=signal.get('atr_pct'), trigger=self._rev_sell_trigger(),
                            avg=self.st.get('intraday_avg_price'), signal=signal if label=='_handle_spiking' else None,
                            rebound_armed=self.st.get('rebound_99_armed'), paused=self.paused_reason))
                    return result
                return wrapped

            setattr(cls, name, make_wrapper(original, name))
        return mod

    daily = pd.read_csv(engine.OUT / '1d.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = pd.read_csv(engine.OUT / '1m.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = minute.loc[minute.index.str[:8] >= '20260421']
    with patch.object(engine, 'load_strategy', load):
        result = engine.replay('v52_nomom', daily, minute, .0005, reference['settings'])
    for key in ('trades', 'orders', 'cycles', 'unclosed', 'equity', 'failure'):
        assert result[key] == reference[key], key
    dest.mkdir()
    (dest / 'trace.json').write_text(json.dumps(dict(records=records, messages=messages,
        original_hashes=reference['hashes'], current_hashes=current_hashes,
        identical_replay=True), ensure_ascii=False, indent=2), encoding='utf-8')
    print('TRACE PASS: identical trades/orders/equity; records', len(records), 'messages', len(messages))


if __name__ == '__main__':
    main()
