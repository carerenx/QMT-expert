# -*- coding: utf-8 -*-
"""Signal diagnostic: python Stragety/RedisQMT/CSI1000Independent/prepare_candidates.py

Frozen v1 trend/pullback rules; outputs candidates only. No backtest or live orders.
ST history, membership cross-anchor and execution data remain gates. See ResearchProtocol.md.
"""
import hashlib
import json
from pathlib import Path

import pandas as pd
import numpy as np

from core.signals import daily_features
from core.signals import ranked_candidates
from core.source_policy import require_redis_membership


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output/analysis/csi1000_independent_20261027'


def main():
    universe = json.loads((OUT / 'reconstructed_membership.json').read_text(encoding='utf-8'))
    require_redis_membership(universe)
    manifest = json.loads((OUT / 'daily/manifest.json').read_text(encoding='utf-8'))
    actions = json.loads((OUT / 'corporate_actions.json').read_text(encoding='utf-8'))
    benchmark = pd.read_parquet(OUT / 'daily/000852.SH.parquet')
    calendar = benchmark.loc['20251009':'20261009'].index
    starts = sorted(universe['snapshots'])
    pools = {date: set(universe['snapshots'][max(start for start in starts if start <= date)])
             for date in calendar}
    features = {}
    checked = 0
    invalid = []
    for code in universe['annual_mainboard_union']:
        path = OUT / f'daily/{code}.parquet'
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][code]['sha256']:
            raise ValueError(f'Changed daily source {code}')
        raw = pd.read_parquet(path)
        required_dates = [date for date in calendar if code in pools[date]]
        required = raw.reindex(required_dates)
        valid = np.isfinite(required.to_numpy()).all(axis=1)
        valid &= required[['open', 'high', 'low', 'close']].gt(0).all(axis=1)
        valid &= required.high.add(1e-8).ge(required[['open', 'low', 'close']].max(axis=1))
        valid &= required.low.sub(1e-8).le(required[['open', 'high', 'close']].min(axis=1))
        valid &= required[['volume', 'amount']].ge(0).all(axis=1)
        checked += len(required)
        invalid.extend({'code': code, 'date': date} for date in required.index[~valid])
        features[code] = daily_features(raw, actions[code])
    daily_audit = {'source': 'Redis QMT raw daily', 'member_days_checked': checked,
                   'invalid_member_days': invalid, 'calendar_days': len(calendar),
                   'scope': 'Raw daily OHLCV validity only; membership cross-anchor, ST and actions not certified'}
    (OUT / 'daily_member_audit.json').write_text(json.dumps(daily_audit, indent=2), encoding='utf-8')
    if invalid:
        raise ValueError(f'{len(invalid)} invalid member-day bars; no candidates approved')
    candidates = ranked_candidates(features, benchmark, universe['snapshots'])
    candidates = candidates[candidates.trade_date.between('20251010', '20261009')]
    candidates.to_csv(OUT / 'v1_candidates.csv', index=False)
    summary = {'status': 'Provisional signal diagnostics only; ST and source gates unresolved',
               'candidate_rows': len(candidates), 'candidate_days': candidates.trade_date.nunique(),
               'candidate_codes': candidates.code.nunique(), 'formal_backtest': False,
               'development_rows': int(candidates.trade_date.le('20260630').sum()),
               'validation_rows': int(candidates.trade_date.ge('20260701').sum()),
               'strategy_source_sha256': hashlib.sha256((Path(__file__).parent / 'core/signals.py').read_bytes()).hexdigest()}
    (OUT / 'v1_candidate_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
