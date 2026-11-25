"""Frozen-data research CLI; never imports a live trader or requests market data."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backtest.redis_minute_audit import audit_sessions
from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import (
    CANDIDATES, REGIMES, features, prepare, replay,
)

DATA = ROOT / 'output/inventory_band_sizing_20261022/data'
INDEX = ROOT / 'analysis/dayt_synchronized_surge_20261104/000300.SH_history.csv'
PROTOCOL = ROOT / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0625_RegimeMinuteResearch.md'
ENGINE = ROOT / 'Stragety/MiniQMT_Stragety/core/dayt_regime_minute_lab.py'
DEFAULT_OUT = ROOT / 'analysis/dayt_regime_lab_20261117/round02_cost_separated'
FOLDS = (('F1', '20260331', '20260401', '20260531'),
         ('F2', '20260531', '20260601', '20260731'),
         ('F3', '20260731', '20260801', '20261020'))
CAPITALS = (100_000, 1_000_000)
SLIPS = (10, 20)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def audit(out, additional_fingerprints=None):
    """Always verify source bytes and session validity before replay/cache use."""
    source = json.loads((DATA / 'manifest.json').read_text(encoding='utf-8'))
    old = json.loads((INDEX.parent / 'manifest.json').read_text(encoding='utf-8'))
    assert sha(INDEX) == old['index_sha256'], 'index changed'
    market = pd.read_csv(INDEX, dtype={'stime': str})
    market.index = pd.to_datetime(market.pop('stime')).dt.strftime('%Y%m%d%H%M%S')
    index_audit = audit_sessions(market)
    common = set(index_audit['valid_dates'])
    stocks = {}
    audits = {}
    for symbol, info in source['symbols'].items():
        path = DATA / (symbol + '_1m.csv')
        assert sha(path) == info['sha256'], 'stock changed: ' + symbol
        frame = pd.read_csv(path, dtype={'time': str}).set_index('time')
        item = audit_sessions(frame)
        active = (frame['volume'] > 0) & (frame['amount'] > 0)
        ratio = float((frame.loc[active, 'amount'] / frame.loc[active, 'volume'] / frame.loc[active, 'close']).median())
        assert 95 < ratio < 105, 'volume units unknown'
        stocks[symbol] = frame.loc[frame.index.str[:8].isin(item['valid_dates'])]
        audits[symbol] = dict(sha256=info['sha256'], volume_ratio=ratio, audit=item)
        common.intersection_update(item['valid_dates'])
    dates = sorted(date for date in common if '20260107' <= date <= '20261020')
    assert dates[0] == '20260107' and dates[-1] == '20261020' and len(dates) >= 180
    # Do not silently bridge unaccounted dividend dates in a continuous account.
    for action in source['actions']:
        if dates[0] < action['date'] <= dates[-1]:
            assert action['date'] in dates, 'missing corporate-action session'
    fingerprint = dict(stock_manifest=sha(DATA / 'manifest.json'), index=sha(INDEX),
                       engine=sha(ENGINE), runner=sha(Path(__file__)), protocol=sha(PROTOCOL))
    if additional_fingerprints:
        fingerprint.update(additional_fingerprints)
    manifest_path = out / 'manifest.json'
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding='utf-8'))
        assert prior['fingerprint'] == fingerprint, 'changed implementation: use a new output directory'
    manifest = dict(fingerprint=fingerprint, source='frozen local raw QMT minute exports; no connection',
                    common_dates=dates, index_audit=index_audit, stocks=audits,
                    folds=FOLDS, capitals=CAPITALS, slips=SLIPS,
                    attribution='entry regime; includes open-leg mark and dividend entitlement difference')
    save(manifest_path, manifest)
    for symbol, frame in stocks.items():
        actions = [action for action in source['actions'] if action['symbol'] == symbol]
        enriched = features(frame, market, actions)
        enriched = enriched.loc[enriched['date'].isin(dates)]
        assert len(enriched) == len(dates) * 240
        enriched.to_pickle(out / (symbol + '_features.pkl'))
    print('AUDIT:', len(dates), 'common sessions;', dates[0], '..', dates[-1], flush=True)
    return manifest


def account_key(fold, split, symbol, candidate, capital, slip):
    return f'{fold}_{split}_{symbol}_{candidate}_{capital}_{slip}'


def run_group(out_string, symbol, fold, split, mapping=None):
    out = Path(out_string)
    label, train_end, valid_start, valid_end = fold
    frame = pd.read_pickle(out / (symbol + '_features.pkl'))
    if split == 'train':
        frame = frame.loc[frame['date'] <= train_end]
    else:
        frame = frame.loc[(frame['date'] >= valid_start) & (frame['date'] <= valid_end)]
    prepared = prepare(frame)
    names = ('LEARNED_ROUTER',) if mapping is not None else CANDIDATES
    summaries = []
    for candidate in names:
        for capital in CAPITALS:
            for slip in SLIPS:
                key = account_key(label, split, symbol, candidate, capital, slip)
                path = out / 'accounts' / (key + '.json')
                if path.exists():
                    summary = json.loads(path.read_text(encoding='utf-8'))
                else:
                    summary, fills, cycles, equity, hold = replay(frame, candidate, capital, slip, mapping, prepared)
                    summary.update(symbol=symbol, fold=label, split=split, mapping=mapping)
                    if split == 'valid':
                        evidence = out / 'ledgers'
                        evidence.mkdir(exist_ok=True)
                        pd.DataFrame(fills).to_csv(evidence / (key + '_fills.csv.gz'), index=False)
                        pd.DataFrame(cycles).to_csv(evidence / (key + '_cycles.csv.gz'), index=False)
                        pd.DataFrame(dict(time=frame.index, equity=equity, hold=hold)).to_csv(
                            evidence / (key + '_equity.csv.gz'), index=False)
                    save(path, summary)
                summaries.append(summary)
    return summaries


def learn_mapping(training, candidates=CANDIDATES):
    """Require robust support, count cycles once (1m capital/10bps), not four times."""
    mapping = dict.fromkeys(REGIMES, 'HOLD')
    diagnostics = {}
    for regime in REGIMES:
        choices = []
        for candidate in candidates:
            if candidate in ('HOLD', 'FIXED_ROUTER'):
                continue
            rows = [row for row in training if row['candidate'] == candidate]
            cells = []
            positive = []
            for capital in CAPITALS:
                for slip in SLIPS:
                    subset = [row for row in rows if row['capital'] == capital and row['slip_bps'] == slip]
                    cells.append(sum(row['scenario_net'][regime] / capital * 100 for row in subset) / len(subset))
                    positive.append(sum(row['scenario_net'][regime] > 0 for row in subset))
            canonical = [row for row in rows if row['capital'] == 1_000_000 and row['slip_bps'] == 10]
            count = sum(row['scenario_cycles'][regime] for row in canonical)
            qualified = min(cells) > 0 and min(positive) >= 3 and count >= 30
            choices.append(dict(candidate=candidate, worst_mean_net_pct=min(cells),
                                cell_mean_net_pct=cells, minimum_positive_stocks=min(positive),
                                canonical_cycles=count, qualified=qualified))
        eligible = [item for item in choices if item['qualified']]
        if eligible:
            mapping[regime] = max(eligible, key=lambda item: item['worst_mean_net_pct'])['candidate']
        diagnostics[regime] = choices
    return mapping, diagnostics


def report(out, summaries, candidates=CANDIDATES):
    validation = [row for row in summaries if row['split'] == 'valid']
    flat = [{key: value for key, value in row.items() if not isinstance(value, (dict, list))}
            for row in validation]
    pd.DataFrame(flat).to_csv(out / 'validation_accounts.csv', index=False)
    aggregated = []
    for candidate in (*candidates, 'LEARNED_ROUTER'):
        for capital in CAPITALS:
            for slip in SLIPS:
                rows = [row for row in validation if row['candidate'] == candidate
                        and row['capital'] == capital and row['slip_bps'] == slip]
                by_stock = {}
                for row in rows:
                    by_stock[row['symbol']] = by_stock.get(row['symbol'], 0.0) + row['excess_pct']
                # Folds restart accounts: sum of percentage-point excess, not compounded portfolio CAGR.
                item = dict(candidate=candidate, capital=capital, slip_bps=slip,
                            mean_fold_sum_excess_pct=sum(by_stock.values()) / len(by_stock),
                            positive_stocks=sum(value > 0 for value in by_stock.values()),
                            maximum_fold_drawdown_pct=max(row['max_drawdown_pct'] for row in rows),
                            maximum_dd_worsening_pct=max(row['max_drawdown_pct'] - row['hold_drawdown_pct'] for row in rows),
                            closed=sum(row['closed'] for row in rows),
                            open_accounts=sum(row['open_leg'] is not None for row in rows),
                            fees=sum(row['fees'] for row in rows), by_stock=by_stock)
                item['gate_pass'] = (item['mean_fold_sum_excess_pct'] > 0 and item['positive_stocks'] >= 4
                                     and item['maximum_dd_worsening_pct'] <= 2)
                aggregated.append(item)
    save(out / 'aggregate.json', aggregated)
    lines = ['# Minute regime experiment: ' + out.name, '',
             'Development rolling validation; previously inspected historical sample, not untouched holdout.',
             'Seven independent stocks, three account-reset folds. Excess is sum of fold percentage points.',
             'Cash dividends are gross receivables; minute execution is a proxy, not queue evidence.',
             'Opening-price proxy plus separately debited adverse impact cost; higher cost does not remove a bar fill.', '',
             '|Candidate|Capital|Slip bp|Mean excess pp|Positive /7|Worst DD delta pp|Closed|Open accounts|Gate|',
             '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for row in aggregated:
        lines.append(f"|{row['candidate']}|{row['capital']}|{row['slip_bps']}|{row['mean_fold_sum_excess_pct']:.4f}|"
                     f"{row['positive_stocks']}|{row['maximum_dd_worsening_pct']:.4f}|{row['closed']}|"
                     f"{row['open_accounts']}|{row['gate_pass']}|")
    lines += ['', 'Training maps and eligibility: mappings.json. Full accounts: accounts/.',
              'Validation fills, closed cycles and full equity: ledgers/. Data audit and hashes: manifest.json.',
              'An aggregate gate pass alone does not establish per-regime effectiveness.']
    (out / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return aggregated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    manifest = audit(out)
    if not args.run:
        return
    summaries = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        tasks = [pool.submit(run_group, str(out), symbol, fold, split)
                 for symbol in manifest['stocks'] for fold in FOLDS for split in ('train', 'valid')]
        for index, future in enumerate(as_completed(tasks), 1):
            summaries.extend(future.result())
            print('BASE GROUP', index, '/', len(tasks), flush=True)
        mappings = {}
        for fold in FOLDS:
            training = [row for row in summaries if row['fold'] == fold[0] and row['split'] == 'train']
            mapping, diagnostics = learn_mapping(training)
            mappings[fold[0]] = dict(mapping=mapping, diagnostics=diagnostics)
        save(out / 'mappings.json', mappings)
        tasks = [pool.submit(run_group, str(out), symbol, fold, 'valid', mappings[fold[0]]['mapping'])
                 for symbol in manifest['stocks'] for fold in FOLDS]
        for index, future in enumerate(as_completed(tasks), 1):
            summaries.extend(future.result())
            print('ROUTER GROUP', index, '/', len(tasks), flush=True)
    aggregate = report(out, summaries)
    print('DONE', len(summaries), 'accounts;', out, flush=True)
    print(json.dumps([item for item in aggregate if item['candidate'] == 'LEARNED_ROUTER'], indent=2))


if __name__ == '__main__':
    main()
