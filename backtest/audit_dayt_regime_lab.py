"""Independent fill/fee/cash/T+1/equity reconstruction from frozen raw minutes."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'output/inventory_band_sizing_20261022/data'


def check_account(path, out, source, actions):
    summary = json.loads(path.read_text(encoding='utf-8'))
    key = path.stem
    equity = pd.read_csv(out / 'ledgers' / (key + '_equity.csv.gz'), dtype={'time': str}).set_index('time')
    try:
        fills = pd.read_csv(out / 'ledgers' / (key + '_fills.csv.gz'), dtype={'time': str, 'signal_time': str})
    except pd.errors.EmptyDataError:
        fills = pd.DataFrame(columns=['time', 'quantity', 'price', 'fee'])
    raw = source.loc[equity.index]
    initial = int(summary['capital'] * 0.5 / raw.iloc[0]['open'] / 100) * 100
    starting_cash = summary['capital'] - initial * raw.iloc[0]['open']
    signed = pd.Series(0.0, index=equity.index)
    payments = signed.copy()
    assert len(fills) == summary['fills']
    assert fills['time'].is_unique
    if not fills.empty:
        values = fills['quantity'].abs() * fills['price']
        expected_fee = np.maximum(5.0, values * 0.00025) + values * 0.00001 + np.where(fills['quantity'] < 0, values * 0.0005, 0)
        np.testing.assert_allclose(fills['fee'], expected_fee, atol=1e-8)
        assert (fills['quantity'].abs() % 100 == 0).all()
        assert fills['time'].isin(equity.index).all()
        matched = raw.loc[fills['time']]
        assert (fills['quantity'].abs().to_numpy() <= np.floor(matched['volume'].to_numpy() / 100) * 100).all()
        np.testing.assert_allclose(fills['price'], matched['open'].to_numpy(), atol=1e-8)
        np.testing.assert_allclose(fills['slippage_cost'], values * summary['slip_bps'] / 10000, atol=1e-8)
        assert (fills['price'].to_numpy() >= matched['low'].to_numpy() - 1e-8).all()
        assert (fills['price'].to_numpy() <= matched['high'].to_numpy() + 1e-8).all()
        signal = pd.to_datetime(fills['signal_time'], format='%Y%m%d%H%M%S')
        execution = pd.to_datetime(fills['time'], format='%Y%m%d%H%M%S')
        assert ((execution - signal).dt.total_seconds() >= 180).all()
        assert ((execution - pd.Timedelta(seconds=60)) > signal + pd.Timedelta(seconds=60)).all()
        signed.loc[fills['time']] = fills['quantity'].to_numpy()
        payments.loc[fills['time']] = (fills['quantity'] * fills['price'] + fills['fee'] + fills['slippage_cost']).to_numpy()
    shares = initial + signed.cumsum()
    cash = starting_cash - payments.cumsum()
    assert cash.min() >= -1e-6 and shares.min() >= 0
    if not fills.empty:
        np.testing.assert_allclose(fills['shares'], shares.loc[fills['time']], atol=1e-8)
        np.testing.assert_allclose(fills['cash'], cash.loc[fills['time']], atol=1e-6)
    date = equity.index.str[:8]
    day_before = (shares - signed).groupby(date).first()
    sellable = day_before.reindex(date).to_numpy() + signed.clip(upper=0).groupby(date).cumsum().to_numpy()
    assert sellable.min() >= 0
    if not fills.empty:
        np.testing.assert_allclose(fills['sellable'], pd.Series(sellable, index=equity.index).loc[fills['time']])
    dividends = pd.Series(0.0, index=equity.index)
    hold_dividends = dividends.copy()
    first_day = date[0]
    for action in actions:
        day = action['date']
        if first_day < day <= date[-1]:
            first = equity.index[date == day][0]
            dividends.loc[first] += day_before.loc[day] * action['cash_dividend_per_share']
            hold_dividends.loc[first] += initial * action['cash_dividend_per_share']
    actual = cash + shares * raw['close'] + dividends.cumsum()
    hold = starting_cash + initial * raw['close'] + hold_dividends.cumsum()
    np.testing.assert_allclose(actual, equity['equity'], atol=1e-5, rtol=1e-12)
    np.testing.assert_allclose(hold, equity['hold'], atol=1e-5, rtol=1e-12)
    assert abs((actual.iloc[-1] - hold.iloc[-1]) / summary['capital'] * 100 - summary['excess_pct']) < 1e-8
    assert abs(dividends.sum() - summary['dividend_receivable']) < 1e-6
    return dict(account=key, minutes=len(equity), fills=len(fills), maximum_equity_error=float((actual - equity['equity']).abs().max()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis/dayt_regime_lab_20261117/round02_cost_separated')
    parser.add_argument('--expected-accounts', type=int, default=840)
    args = parser.parse_args()
    manifest = json.loads((DATA / 'manifest.json').read_text(encoding='utf-8'))
    source = {symbol: pd.read_csv(DATA / (symbol + '_1m.csv'), dtype={'time': str}).set_index('time')
              for symbol in manifest['symbols']}
    results = []
    for path in sorted((args.out / 'accounts').glob('*_valid_*.json')):
        summary = json.loads(path.read_text(encoding='utf-8'))
        actions = [action for action in manifest['actions'] if action['symbol'] == summary['symbol']]
        results.append(check_account(path, args.out, source[summary['symbol']], actions))
        if len(results) % 100 == 0:
            print('AUDITED', len(results), flush=True)
    expected = args.expected_accounts
    assert len(results) == expected, (len(results), expected)
    result = dict(status='passed', accounts=len(results), fills=sum(row['fills'] for row in results),
                  maximum_equity_error=max(row['maximum_equity_error'] for row in results), results=results)
    (args.out / 'independent_ledger_audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('INDEPENDENT AUDIT PASSED', len(results), 'validation accounts', flush=True)


if __name__ == '__main__':
    main()
