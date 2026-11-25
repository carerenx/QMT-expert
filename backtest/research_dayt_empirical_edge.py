"""Frozen twelve-stock conditional action/horizon experiment. No live APIs."""
import argparse
import ast
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.audit_dayt_regime_lab import check_account
from backtest.redis_minute_audit import audit_sessions
from backtest.research_dayt_regime_lab import CAPITALS, FOLDS, SLIPS, account_key, save, sha
from Stragety.MiniQMT_Stragety.core.dayt_regime_minute_lab import REGIMES, features, prepare, replay
from Stragety.MiniQMT_Stragety.core.dayt_empirical_edge_lab import (
    CANDIDATES, ROBUST, catalog, enrich, fit_model, label_examples,
)

DEFAULT_OUT = ROOT / 'analysis/dayt_empirical_edge_20261118/round01'
SOURCES = (ROOT / 'output/inventory_band_sizing_20261022/data',
           ROOT / 'output/v0621_five_stock_transfer_20261023/data')
INDEX = ROOT / 'analysis/dayt_synchronized_surge_20261104/000300.SH_history.csv'
PROTOCOL = ROOT / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0627_EmpiricalSceneHorizonT.md'
FILES = (Path(__file__), PROTOCOL,
         ROOT / 'Stragety/MiniQMT_Stragety/DayT/DayT_v0627_EmpiricalSceneHorizonT.py',
         ROOT / 'Stragety/MiniQMT_Stragety/core/dayt_empirical_edge_lab.py',
         ROOT / 'Stragety/MiniQMT_Stragety/core/dayt_regime_minute_lab.py',
         ROOT / 'backtest/audit_dayt_regime_lab.py',
         ROOT / 'backtest/redis_minute_audit.py',
         ROOT / 'backtest/research_dayt_regime_lab.py')
VARIANTS = ('EMPIRICAL_MEAN', *ROBUST)
SCENES = tuple(mode for mode in REGIMES if mode not in ('UNKNOWN', 'ILLIQUID'))


def audit(out):
    stock_frames = {}
    stocks = {}
    common = None
    fingerprint = {str(path.relative_to(ROOT)): sha(path) for path in FILES}
    index_manifest = json.loads((INDEX.parent / 'manifest.json').read_text(encoding='utf-8'))
    assert sha(INDEX) == index_manifest['index_sha256']
    fingerprint[str(INDEX.relative_to(ROOT))] = sha(INDEX)
    market = pd.read_csv(INDEX, dtype={'stime': str})
    market.index = pd.to_datetime(market.pop('stime')).dt.strftime('%Y%m%d%H%M%S')
    index_audit = audit_sessions(market)
    common = set(index_audit['valid_dates'])
    for group, directory in zip(('original7', 'additional5'), SOURCES):
        path = directory / 'manifest.json'
        fingerprint[str(path.relative_to(ROOT))] = sha(path)
        original = json.loads(path.read_text(encoding='utf-8'))
        for symbol, item in original['symbols'].items():
            path = directory / (symbol + '_1m.csv')
            digest = sha(path)
            assert digest == item['sha256'], symbol
            fingerprint[str(path.relative_to(ROOT))] = digest
            raw = pd.read_csv(path, dtype={'time': str}).set_index('time')
            audited = audit_sessions(raw)
            active = (raw['volume'] > 0) & (raw['amount'] > 0)
            ratio = float((raw.loc[active, 'amount'] / raw.loc[active, 'volume'] / raw.loc[active, 'close']).median())
            assert 95 < ratio < 105
            actions = [action for action in original['actions'] if action['symbol'] == symbol]
            assert all(not any(float(value) != 0 for value in action['raw_factor_values'][1:6]) for action in actions)
            stocks[symbol] = dict(path=str(path), sha256=digest, group=group, volume_ratio=ratio,
                                  actions=actions, audit=audited)
            stock_frames[symbol] = raw.loc[raw.index.str[:8].isin(audited['valid_dates'])]
            common.intersection_update(audited['valid_dates'])
    dates = sorted(date for date in common if '20260107' <= date <= '20261020')
    assert len(dates) >= 180 and dates[0] == '20260107' and dates[-1] == '20261020'
    assert len(stocks) == 12
    for item in stocks.values():
        assert all(action['date'] in dates for action in item['actions'] if dates[0] < action['date'] <= dates[-1])
    manifest = dict(fingerprint=fingerprint, source='local frozen raw QMT minute exports; no connection/download',
                    stocks=stocks, common_dates=dates, index_audit=index_audit,
                    folds=FOLDS, capitals=CAPITALS, slips=SLIPS, candidates=CANDIDATES)
    path = out / 'manifest.json'
    if path.exists():
        prior = json.loads(path.read_text(encoding='utf-8'))
        assert prior['fingerprint'] == fingerprint, 'changed study: use a new output directory'
    save(path, manifest)
    for symbol, raw in stock_frames.items():
        frame = features(raw, market, stocks[symbol]['actions'])
        frame = enrich(frame.loc[frame['date'].isin(dates)])
        assert len(frame) == len(dates) * 240
        frame.to_pickle(out / (symbol + '_features.pkl'))
    for path in FILES:
        target = out / 'source_snapshot' / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            assert sha(path) == sha(target)
        shutil.copy2(path, target)
    save(out / 'runtime.json', dict(python=platform.python_version(), numpy=np.__version__, pandas=pd.__version__))
    print('AUDIT', len(stocks), 'stocks;', len(dates), 'common sessions', dates[0], dates[-1], flush=True)
    return manifest


def fit_fold_models(out, manifest):
    combined = {}
    label_directory = out / 'labels'
    label_directory.mkdir(exist_ok=True)
    for capital in CAPITALS:
        parts = []
        for symbol in manifest['stocks']:
            frame = pd.read_pickle(out / (symbol + '_features.pkl'))
            labels = label_examples(frame, symbol, capital, FOLDS[-1][1])
            labels.to_csv(label_directory / f'{symbol}_{capital}.csv.gz', index=False)
            if not labels.empty:
                parts.append(labels)
        combined[capital] = pd.concat(parts, ignore_index=True)
    models = {}
    for fold in FOLDS:
        label = fold[0]
        cutoff = fold[1]
        fitted = {}
        for capital in CAPITALS:
            labels = combined[capital]
            settled = labels.loc[labels['label_end'].str[:8] <= cutoff]
            fitted[str(capital)] = {variant: fit_model(settled, variant, cutoff) for variant in VARIANTS}
        models[label] = dict(models=fitted, blocked={name: [] for name in ROBUST}, cutoff=cutoff)
        save(out / 'models' / (label + '_initial.json'), models[label])
        print('FITTED', label, {capital: {name: len(model['rules']) for name, model in books.items()}
                                for capital, books in fitted.items()}, flush=True)
    return models


def horizon_attribution(summary, fills, cycles):
    net = {}
    entries = {}
    closed = {}
    active = None
    remaining = 0
    for fill in fills:
        if not fill['closing']:
            active = str(fill.get('opening_terms', {}).get('max_trading_bars', 'ATR_EXIT'))
            entries[active] = entries.get(active, 0) + 1
            remaining = abs(fill['quantity'])
        else:
            net[active] = net.get(active, 0.0) + fill['net_contribution']
            remaining -= abs(fill['quantity'])
            if remaining == 0:
                closed[active] = closed.get(active, 0) + 1
                active = None
    if active is not None:
        net[active] = net.get(active, 0.0) + summary['open_contribution']
    summary['horizon_net'] = net
    summary['horizon_entries'] = entries
    summary['horizon_cycles'] = closed
    assert abs(sum(net.values()) / summary['capital'] * 100 - summary['excess_pct']) < 1e-8


def run_group(out_string, symbol, fold, split, model_bundle, names=CANDIDATES):
    out = Path(out_string)
    frame = pd.read_pickle(out / (symbol + '_features.pkl'))
    if split == 'valid':
        frame = frame.loc[(frame['date'] >= fold[2]) & (frame['date'] <= fold[3])]
    else:
        frame = frame.loc[frame['date'] <= fold[1]]
    prepared = prepare(frame)
    results = []
    for capital in CAPITALS:
        policies = catalog(model_bundle['models'][str(capital)], model_bundle['blocked'])
        for slip in SLIPS:
            for candidate in names:
                key = account_key(fold[0], split, symbol, candidate, capital, slip)
                path = out / 'accounts' / (key + '.json')
                if path.exists():
                    summary = json.loads(path.read_text(encoding='utf-8'))
                else:
                    result = replay(frame, candidate, capital, slip, prepared=prepared, policy_catalog=policies)
                    summary = result[0]
                    fills = result[1]
                    cycles = result[2]
                    horizon_attribution(summary, fills, cycles)
                    summary.update(symbol=symbol, fold=fold[0], split=split, blocked=model_bundle['blocked'])
                    if split == 'valid':
                        ledgers = out / 'ledgers'
                        ledgers.mkdir(exist_ok=True)
                        pd.DataFrame(fills).to_csv(ledgers / (key + '_fills.csv.gz'), index=False)
                        pd.DataFrame(cycles).to_csv(ledgers / (key + '_cycles.csv.gz'), index=False)
                        pd.DataFrame(dict(time=frame.index, equity=result[3], hold=result[4])).to_csv(
                            ledgers / (key + '_equity.csv.gz'), index=False)
                    save(path, summary)
                results.append(summary)
    return results


def collect(pool, tasks, title):
    results = []
    for index, future in enumerate(as_completed(tasks), 1):
        results.extend(future.result())
        print(title, index, '/', len(tasks), flush=True)
    return results


def training_gate(rows, blocked):
    updated = {name: list(blocked[name]) for name in ROBUST}
    diagnostics = {}
    for name in ROBUST:
        diagnostics[name] = {}
        for scene in SCENES:
            if scene in updated[name]:
                continue
            qualified = True
            cells = []
            for capital in CAPITALS:
                for slip in SLIPS:
                    selected = [row for row in rows if row['candidate'] == name and
                                row['capital'] == capital and row['slip_bps'] == slip]
                    average = sum(row['scenario_net'][scene] / capital * 100 for row in selected) / len(selected)
                    positive = sum(row['scenario_net'][scene] > 0 for row in selected)
                    cells.append(dict(capital=capital, slip=slip, average=average, positive_stocks=positive))
                    qualified = qualified and average > 0 and positive >= 3
            count = sum(row['scenario_cycles'][scene] for row in rows if row['candidate'] == name and
                        row['capital'] == 1_000_000 and row['slip_bps'] == 10)
            qualified = bool(qualified and count >= 30)
            diagnostics[name][scene] = dict(cells=cells, canonical_cycles=count, qualified=qualified)
            if not qualified:
                updated[name].append(scene)
    return updated, diagnostics


def report(out, manifest, rows):
    aggregates = []
    scene_rows = []
    horizon_rows = []
    groups = {'all12': set(manifest['stocks']),
              'original7': {symbol for symbol, item in manifest['stocks'].items() if item['group'] == 'original7'},
              'additional5': {symbol for symbol, item in manifest['stocks'].items() if item['group'] == 'additional5'}}
    for name in CANDIDATES:
        for capital in CAPITALS:
            for slip in SLIPS:
                selected = [row for row in rows if row['candidate'] == name and row['capital'] == capital and row['slip_bps'] == slip]
                for group, symbols in groups.items():
                    cells = [row for row in selected if row['symbol'] in symbols]
                    by_stock = {symbol: sum(row['excess_pct'] for row in cells if row['symbol'] == symbol) for symbol in symbols}
                    positive = sum(value > 0 for value in by_stock.values())
                    worsening = max(row['max_drawdown_pct'] - row['hold_drawdown_pct'] for row in cells)
                    average = sum(by_stock.values()) / len(symbols)
                    gate = average > 0 and positive >= {'all12': 7, 'original7': 4, 'additional5': 3}[group] and worsening <= 2
                    aggregates.append(dict(candidate=name, capital=capital, slip_bps=slip, group=group,
                                           mean_fold_sum_excess_pct=average, positive_stocks=positive,
                                           maximum_dd_worsening_pct=worsening, closed=sum(row['closed'] for row in cells),
                                           open_accounts=sum(row['open_leg'] is not None for row in cells),
                                           gate_pass=gate, by_stock=by_stock))
                for scene in REGIMES:
                    scene_rows.append(dict(candidate=name, capital=capital, slip=slip, scene=scene,
                                           mean_net_pct=sum(row['scenario_net'][scene] / capital * 100 for row in selected) / 12,
                                           canonical_cycles=sum(row['scenario_cycles'][scene] for row in selected) if capital == 1_000_000 and slip == 10 else None))
                for horizon in ('30', '60', '240', '720', 'ATR_EXIT'):
                    horizon_rows.append(dict(candidate=name, capital=capital, slip=slip, horizon=horizon,
                                             mean_net_pct=sum(row['horizon_net'].get(horizon, 0.0) / capital * 100 for row in selected) / 12,
                                             closed=sum(row['horizon_cycles'].get(horizon, 0) for row in selected),
                                             entries=sum(row['horizon_entries'].get(horizon, 0) for row in selected)))
    save(out / 'aggregate.json', aggregates)
    pd.DataFrame(scene_rows).to_csv(out / 'scene_validation.csv', index=False)
    pd.DataFrame(horizon_rows).to_csv(out / 'horizon_validation.csv', index=False)
    lines = ['# v0627 empirical scene/horizon minute validation', '',
             'Development rolling validation on 12 frozen stocks; added five stocks are also trained, not untouched transfer.',
             'Values are equal-stock mean of summed fold excess percentage points; not CAGR or continuous compounded returns.',
             '30/60 bars are intraday; 240/720 bars are overnight inventory cycles and cannot establish intraday success.', '',
             '|Candidate|Capital|bp|Mean excess pp|Positive /12|Worst DD delta pp|Closed|Open|Gate all12|',
             '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for row in aggregates:
        if row['group'] == 'all12':
            lines.append(f"|{row['candidate']}|{row['capital']}|{row['slip_bps']}|{row['mean_fold_sum_excess_pct']:.4f}|"
                         f"{row['positive_stocks']}|{row['maximum_dd_worsening_pct']:.4f}|{row['closed']}|{row['open_accounts']}|{row['gate_pass']}|")
    lines += ['', 'Cohort gates, full stock results: aggregate.json. Scene and horizon attribution: *_validation.csv.',
              'Train-only pruning and final frozen models: models/. Labels are counterfactual proxies, not account P&L.',
              'MEAN and fixed-side controls are diagnostic ablations, not qualified deployable scene strategies.']
    (out / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def verify(out):
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    sources = {}
    for symbol, item in manifest['stocks'].items():
        path = Path(item['path'])
        assert sha(path) == item['sha256']
        sources[symbol] = pd.read_csv(path, dtype={'time': str}).set_index('time')
    ledgers = []
    timestamp_rules = 0
    empirical_openings = 0
    baseline_checks = 0
    for fold in FOLDS:
        bundle = json.loads((out / 'models' / (fold[0] + '_final.json')).read_text(encoding='utf-8'))
        for models in bundle['models'].values():
            for model in models.values():
                assert model['cutoff'] == fold[1] and model['cutoff'] < fold[2]
                for rules in model['rules'].values():
                    for rule in rules:
                        assert rule['last_label_end'][:8] <= fold[1]
                        timestamp_rules += 1
    for path in sorted((out / 'accounts').glob('*_valid_*.json')):
        summary = json.loads(path.read_text(encoding='utf-8'))
        symbol = summary['symbol']
        ledgers.append(check_account(path, out, sources[symbol], manifest['stocks'][symbol]['actions']))
        if manifest['stocks'][symbol]['group'] == 'original7' and summary['candidate'] in ('HOLD', 'SYMMETRIC'):
            prior_path = ROOT / 'analysis/dayt_regime_lab_20261117/round02_cost_separated/accounts' / path.name
            prior = json.loads(prior_path.read_text(encoding='utf-8'))
            for key in ('excess_pct', 'closed', 'fills', 'final_equity', 'fees', 'slippage_cost', 'final_shares'):
                np.testing.assert_allclose(summary[key], prior[key], atol=1e-8, rtol=1e-12)
            baseline_checks += 1
        if summary['candidate'].startswith('EMPIRICAL'):
            try:
                fills = pd.read_csv(out / 'ledgers' / (path.stem + '_fills.csv.gz'), dtype={'signal_time': str})
            except pd.errors.EmptyDataError:
                fills = pd.DataFrame()
            if not fills.empty:
                for row in fills.loc[~fills['closing']].to_dict('records'):
                    terms = ast.literal_eval(row['opening_terms'])
                    assert terms['training_cutoff'] < row['signal_time'][:8]
                    assert terms['training_last_label'][:8] <= terms['training_cutoff']
                    assert terms['forecast_net_lower'] >= 0.001
                    assert terms['forecast_side'] == int(np.sign(row['quantity']))
                    empirical_openings += 1
        if len(ledgers) % 100 == 0:
            print('INDEPENDENT LEDGER', len(ledgers), flush=True)
    assert len(ledgers) == 7 * 12 * 3 * 4
    assert baseline_checks == 168
    result = dict(status='passed', validation_accounts=len(ledgers), model_rule_timestamps=timestamp_rules,
                  empirical_openings_checked=empirical_openings, unchanged_baseline_accounts=baseline_checks,
                  maximum_equity_error=max(row['maximum_equity_error'] for row in ledgers), accounts=ledgers)
    save(out / 'independent_audit.json', result)
    print('VERIFIED', len(ledgers), 'accounts;', empirical_openings, 'empirical openings', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if args.verify:
        verify(out)
        return
    manifest = audit(out)
    if not args.run:
        return
    bundles = fit_fold_models(out, manifest)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        tasks = [pool.submit(run_group, str(out), symbol, fold, 'train_initial', bundles[fold[0]])
                 for symbol in manifest['stocks'] for fold in FOLDS]
        training = collect(pool, tasks, 'TRAIN INITIAL')
        for fold in FOLDS:
            bundle = bundles[fold[0]]
            current = [row for row in training if row['fold'] == fold[0]]
            trace = []
            for iteration in range(8):
                gated = training_gate(current, bundle['blocked'])
                trace.append(dict(iteration=iteration, before=bundle['blocked'], after=gated[0], diagnostics=gated[1]))
                if gated[0] == bundle['blocked']:
                    break
                bundle['blocked'] = gated[0]
                phase = 'train_pruned' + str(iteration + 1)
                tasks = [pool.submit(run_group, str(out), symbol, fold, phase, bundle, ROBUST)
                         for symbol in manifest['stocks']]
                current = collect(pool, tasks, fold[0] + ' ' + phase)
            else:
                raise AssertionError('training scene pruning did not converge')
            bundle['pruning_trace'] = trace
            save(out / 'models' / (fold[0] + '_final.json'), bundle)
            print('TRAIN GATES FROZEN', fold[0], bundle['blocked'], flush=True)
        tasks = [pool.submit(run_group, str(out), symbol, fold, 'valid', bundles[fold[0]])
                 for symbol in manifest['stocks'] for fold in FOLDS]
        validation = collect(pool, tasks, 'VALIDATION')
    report(out, manifest, validation)
    print('DONE', len(validation), 'validation accounts;', out, flush=True)


if __name__ == '__main__':
    main()
