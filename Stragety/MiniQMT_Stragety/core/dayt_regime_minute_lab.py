"""Offline, causal regime features and continuous paired-T minute accounts.

No xtquant, broker, network, live checkpoint or versioned-strategy imports.
Minute labels are conservatively exposed +60s; orders use the signal three
indices earlier, then the current minute OPEN proxy with explicit impact cost.
"""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd


REGIMES = ('SHOCK_UP', 'SHOCK_DOWN', 'UP_TREND', 'DOWN_TREND',
           'RANGE_HIGH_VOL', 'RANGE_LOW_VOL', 'ILLIQUID', 'UNKNOWN')
CANDIDATES = ('HOLD', 'REV15', 'REV20', 'REV30', 'FWD15', 'SYMMETRIC',
              'PULLBACK', 'MOMENTUM', 'FIXED_ROUTER')
FIXED_MAP = {'SHOCK_UP': 'MOMENTUM', 'SHOCK_DOWN': 'MOMENTUM',
             'UP_TREND': 'PULLBACK', 'DOWN_TREND': 'REV20',
             'RANGE_HIGH_VOL': 'SYMMETRIC', 'RANGE_LOW_VOL': 'HOLD',
             'ILLIQUID': 'HOLD', 'UNKNOWN': 'HOLD'}


def fee(quantity, price):
    value = abs(quantity) * price
    return max(5.0, value * 0.00025) + value * 0.00001 + (value * 0.0005 if quantity < 0 else 0.0)


def features(frame, market, actions):
    """Feature row t uses rows <=t; previous daily indicators are shifted once."""
    result = frame.copy()
    result['date'] = result.index.str[:8]
    result['session'] = result['date'] + np.where(result.index.str[8:12] < '1200', 'AM', 'PM')
    result['minute'] = result.index.str[8:12]
    result = result.loc[result['minute'] != '0930'].copy()
    cash_by_day = {}
    for action in actions:
        if any(float(value) != 0 for value in action['raw_factor_values'][1:6]):
            raise ValueError('unsupported noncash corporate action')
        cash_by_day[action['date']] = cash_by_day.get(action['date'], 0.0) + float(action['cash_dividend_per_share'])
    result['cash_action'] = result['date'].map(cash_by_day).fillna(0.0)
    day_action = result.groupby('date')['cash_action'].first().cumsum()
    result['cum_cash_action'] = result['date'].map(day_action)
    # Additive continuity series only uses actions known by that date.
    continuity = result['cum_cash_action']
    daily = pd.DataFrame({
        'high': (result['high'] + continuity).groupby(result['date']).max(),
        'low': (result['low'] + continuity).groupby(result['date']).min(),
        'close': (result['close'] + continuity).groupby(result['date']).last(),
        'raw_close': result.groupby('date')['close'].last(),
    })
    prior_close = daily['close'].shift(1)
    tr = pd.concat([daily['high'] - daily['low'],
                    (daily['high'] - prior_close).abs(),
                    (daily['low'] - prior_close).abs()], axis=1).max(axis=1)
    daily['atr'] = tr.rolling(14, min_periods=14).mean().shift(1)
    daily['ma20'] = daily['close'].rolling(20, min_periods=20).mean().shift(1)
    daily['ma20_old'] = daily['close'].rolling(20, min_periods=20).mean().shift(6)
    daily['prior_raw'] = daily['raw_close'].shift(1)
    result['prior_close'] = result['date'].map(daily['prior_raw']) - result['cash_action']
    result['atr_pct'] = result['date'].map(daily['atr']) / result['prior_close']
    result['daily_up'] = result['date'].map(daily['ma20']) > result['date'].map(daily['ma20_old'])
    groups = result.groupby('session', sort=False)
    close = result['close']
    result['ret3'] = groups['close'].pct_change(3, fill_method=None)
    result['ret5'] = groups['close'].pct_change(5, fill_method=None)
    result['ret20'] = groups['close'].pct_change(20, fill_method=None)
    result['step'] = groups['close'].pct_change(fill_method=None).abs()
    travel = result.groupby('session', sort=False)['step'].transform(lambda x: x.rolling(20, min_periods=20).sum())
    result['efficiency'] = result['ret20'].abs() / travel.replace(0, np.nan)
    result['typical_value'] = ((result['high'] + result['low'] + close) / 3) * result['volume']
    numerator = result.groupby('session', sort=False)['typical_value'].cumsum()
    denominator = groups['volume'].cumsum()
    result['vwap'] = numerator / denominator.replace(0, np.nan)
    result['vwap_gap'] = close / result['vwap'] - 1
    result['high10'] = groups['close'].transform(lambda x: x.shift(1).rolling(10, min_periods=10).max())
    result['low10'] = groups['close'].transform(lambda x: x.shift(1).rolling(10, min_periods=10).min())
    result['mean_amount20'] = groups['amount'].transform(lambda x: x.rolling(20, min_periods=20).mean())
    index_close = market['close'].reindex(result.index)
    index_group = index_close.groupby(result['session'], sort=False)
    result['index_ret3'] = index_group.pct_change(3, fill_method=None)
    result['index_ret5'] = index_group.pct_change(5, fill_method=None)
    result['regime'] = classify(result)
    return result


def classify(frame):
    regime = np.full(len(frame), 'UNKNOWN', dtype=object)
    valid = np.isfinite(frame[['atr_pct', 'ret20', 'efficiency', 'vwap_gap',
                               'index_ret3', 'index_ret5', 'mean_amount20']].to_numpy()).all(axis=1)
    liquid = valid & (frame['mean_amount20'].to_numpy() >= 1_000_000)
    regime[valid & ~liquid] = 'ILLIQUID'
    regime[liquid & (frame['atr_pct'].to_numpy() >= 0.03)] = 'RANGE_HIGH_VOL'
    regime[liquid & (frame['atr_pct'].to_numpy() < 0.03)] = 'RANGE_LOW_VOL'
    directional = liquid & (frame['efficiency'].to_numpy() >= 0.35)
    up = directional & (frame['ret20'].to_numpy() >= 0.004) & (frame['vwap_gap'].to_numpy() > 0)
    down = directional & (frame['ret20'].to_numpy() <= -0.004) & (frame['vwap_gap'].to_numpy() < 0)
    regime[up] = 'UP_TREND'
    regime[down] = 'DOWN_TREND'
    shock_up = (((frame.ret3 >= 0.01) & (frame.index_ret3 >= 0.0015)) |
                ((frame.ret5 >= 0.015) & (frame.index_ret5 >= 0.0025)) | (frame.ret3 >= 0.02))
    shock_down = (((frame.ret3 <= -0.01) & (frame.index_ret3 <= -0.0015)) |
                  ((frame.ret5 <= -0.015) & (frame.index_ret5 <= -0.0025)) | (frame.ret3 <= -0.02))
    regime[liquid & shock_up.to_numpy()] = 'SHOCK_UP'
    regime[liquid & shock_down.to_numpy()] = 'SHOCK_DOWN'
    return regime


@dataclass(frozen=True)
class Policy:
    name: str
    distance: float = 0.15
    max_minutes: int = 60

    def entry(self, row):
        mode = row['regime']
        if self.name == 'HOLD' or mode in ('UNKNOWN', 'ILLIQUID'):
            return 0
        band = min(0.03, max(0.002, row['atr_pct'] * self.distance))
        gap = row['vwap_gap']
        if self.name.startswith('REV'):
            return -1 if gap >= band and row['ret3'] < 0 else 0
        if self.name == 'FWD15':
            return 1 if gap <= -band and row['ret3'] > 0 else 0
        if self.name == 'SYMMETRIC':
            if gap >= band and row['ret3'] < 0:
                return -1
            return 1 if gap <= -band and row['ret3'] > 0 else 0
        if self.name == 'PULLBACK':
            return 1 if row['daily_up'] and -band <= gap <= 0.002 and row['ret3'] > 0 else 0
        if self.name == 'MOMENTUM':
            if row['close'] > row['high10'] and row['ret3'] >= 0.003:
                return 1
            return -1 if row['close'] < row['low10'] and row['ret3'] <= -0.003 else 0
        raise ValueError('unregistered policy ' + self.name)


POLICIES = {name: Policy(name, {'REV20': 0.20, 'REV30': 0.30}.get(name, 0.15))
            for name in CANDIDATES if name != 'FIXED_ROUTER'}


def prepare(frame):
    return (frame.to_dict('records'), frame.index.tolist(), frame['date'].tolist(),
            pd.to_datetime(frame.index, format='%Y%m%d%H%M%S').asi8 // 1_000_000_000)


def replay(frame, candidate, capital, slip_bps, mapping=None, prepared=None, policy_catalog=None):
    """One continuous account, fixed initial inventory, one paired leg at a time."""
    if frame.empty:
        raise ValueError('empty replay')
    rows, stamps, day_values, epochs = prepare(frame) if prepared is None else prepared
    initial_price = float(rows[0]['open'])
    base = int(capital * 0.5 / initial_price / 100) * 100
    shares = base
    cash = capital - shares * initial_price
    hold_cash = cash
    sellable = shares
    receivable = 0.0
    hold_receivable = 0.0
    leg = None
    last_day = None
    all_fills = []
    cycles = []
    equities = []
    hold_equities = []
    rejection = {'cash': 0, 't1': 0, 'volume': 0, 'price': 0, 'edge': 0}
    scenario_net = dict.fromkeys(REGIMES, 0.0)
    scenario_cycles = dict.fromkeys(REGIMES, 0)
    scenario_minutes = dict.fromkeys(REGIMES, 0)
    selected_map = FIXED_MAP if candidate == 'FIXED_ROUTER' else mapping
    catalog = POLICIES if policy_catalog is None else policy_catalog
    peak = capital
    hold_peak = capital
    maximum_dd = 0.0
    hold_dd = 0.0
    paid = 0.0
    impact_paid = 0.0
    total_net = 0.0
    for i, row in enumerate(rows):
        day = day_values[i]
        if day != last_day:
            sellable = shares
            action = float(row['cash_action'])
            if last_day is not None and action:
                receivable += shares * action
                hold_receivable += base * action
                if leg:
                    leg['dividends'] += leg['side'] * leg['quantity'] * action
            last_day = day
        mode = row['regime']
        scenario_minutes[mode] += 1
        signal = rows[i - 3] if i >= 3 else None
        contiguous = (i >= 3 and row['session'] == signal['session']
                      and epochs[i] - epochs[i - 3] == 180)
        direction = 0
        chosen = None
        closing = False
        reason = ''
        if contiguous and signal['minute'] <= '1450':
            if leg:
                side = leg['side']
                economic_entry = leg['price'] - leg['dividends'] / (side * leg['quantity'])
                move = side * (signal['close'] / economic_entry - 1) if economic_entry > 0 else 0
                target = leg.get('target_pct', max(0.003, min(0.03, leg['atr_pct'] * leg['distance'])))
                stop = leg.get('stop_pct', max(0.005, min(0.02, leg['atr_pct'] * 0.2)))
                age = (epochs[i - 3] - leg['signal_epoch']) / 60
                if 'max_trading_bars' in leg:
                    holding_due = i - 3 - leg['signal_index'] >= leg['max_trading_bars']
                else:
                    holding_due = age >= leg['max_minutes']
                if move >= target or move <= -stop or holding_due:
                    direction = -side
                    closing = True
                    reason = 'profit' if move >= target else ('risk' if move <= -stop else 'timeout')
            elif '0945' <= signal['minute'] <= '1420':
                name = selected_map.get(signal['regime'], 'HOLD') if selected_map is not None else candidate
                chosen = catalog[name]
                direction = chosen.entry(signal)
        if direction:
            price = float(row['open'])
            previous_close = float(row['prior_close'])
            sealed = row['high'] == row['low'] and previous_close > 0 and abs(row['open'] / previous_close - 1) >= 0.095
            capacity = int(max(0.0, row['volume']) * 100 * 0.01 / 100) * 100
            desired = leg['quantity'] if closing else int(base * 0.20 / 100) * 100
            quantity = min(desired, capacity)
            if quantity < 100:
                rejection['volume'] += 1
                quantity = 0
            elif sealed or not row['low'] <= price <= row['high']:
                rejection['price'] += 1
                quantity = 0
            elif direction < 0:
                quantity = min(quantity, sellable // 100 * 100)
                if quantity == 0:
                    rejection['t1'] += 1
            else:
                while quantity >= 100 and quantity * price + fee(quantity, price) + quantity * price * slip_bps / 10_000 > cash + 1e-8:
                    quantity -= 100
                if quantity == 0:
                    rejection['cash'] += 1
            terms = {}
            if quantity and not closing and hasattr(chosen, 'execution_terms'):
                terms = chosen.execution_terms(signal, quantity, price, direction)
                if terms is None:
                    rejection['edge'] += 1
                    quantity = 0
            if quantity:
                signed = direction * quantity
                broker_fee = fee(signed, price)
                impact = quantity * price * slip_bps / 10_000
                cost = broker_fee + impact
                cash -= signed * price + cost
                shares += signed
                paid += broker_fee
                impact_paid += impact
                if signed < 0:
                    sellable -= quantity
                assert cash >= -1e-6 and shares >= 0 and 0 <= sellable <= shares
                fill = dict(time=stamps[i], event_epoch=int(epochs[i] - 60),
                            signal_time=stamps[i - 3], available_epoch=int(epochs[i - 3] + 60),
                            quantity=signed, price=price, fee=broker_fee, slippage_cost=impact, cash=cash, shares=shares,
                            sellable=sellable, closing=closing, reason=reason)
                assert fill['event_epoch'] > fill['available_epoch']
                if closing:
                    proportion = quantity / leg['quantity']
                    entry_fee = leg['fee'] * proportion
                    dividend = leg['dividends'] * proportion
                    profit = leg['side'] * quantity * (price - leg['price']) - cost - entry_fee + dividend
                    scenario_net[leg['regime']] += profit
                    total_net += profit
                    leg['quantity'] -= quantity
                    leg['fee'] -= entry_fee
                    leg['dividends'] -= dividend
                    leg['realized'] += profit
                    fill.update(regime=leg['regime'], policy=leg['policy'], net_contribution=profit)
                    if leg['quantity'] == 0:
                        scenario_cycles[leg['regime']] += 1
                        cycles.append(dict(regime=leg['regime'], policy=leg['policy'], net=leg['realized'],
                                           opened=leg['opened'], closed=stamps[i],
                                           wait_minutes=(epochs[i] - leg['signal_epoch']) / 60))
                        leg = None
                else:
                    leg = dict(side=direction, quantity=quantity, price=price, fee=cost,
                               dividends=0.0, realized=0.0, atr_pct=float(signal['atr_pct']),
                               distance=chosen.distance, max_minutes=chosen.max_minutes,
                               regime=signal['regime'], policy=chosen.name,
                               opened=stamps[i], signal_epoch=int(epochs[i - 3]), signal_index=i - 3)
                    leg.update(terms)
                    fill.update(opening_terms=terms)
                    fill.update(regime=leg['regime'], policy=chosen.name, net_contribution=None)
                all_fills.append(fill)
        equity = cash + shares * float(row['close']) + receivable
        hold_equity = hold_cash + base * float(row['close']) + hold_receivable
        equities.append(equity)
        hold_equities.append(hold_equity)
        peak = max(peak, equity)
        hold_peak = max(hold_peak, hold_equity)
        maximum_dd = max(maximum_dd, 1 - equity / peak)
        hold_dd = max(hold_dd, 1 - hold_equity / hold_peak)
    open_value = 0.0
    if leg:
        open_value = (leg['side'] * leg['quantity'] * (float(rows[-1]['close']) - leg['price'])
                      - leg['fee'] + leg['dividends'])
        scenario_net[leg['regime']] += open_value
    excess = equities[-1] - hold_equities[-1]
    assert abs(excess - total_net - open_value) < max(1e-5, capital * 1e-10), (excess, total_net, open_value)
    assert shares == base + sum(fill['quantity'] for fill in all_fills)
    assert abs(cash - (hold_cash - sum(fill['quantity'] * fill['price'] + fill['fee'] + fill['slippage_cost'] for fill in all_fills))) < 1e-5
    wait = [cycle['wait_minutes'] for cycle in cycles]
    summary = dict(candidate=candidate, capital=capital, slip_bps=slip_bps,
                   first=stamps[0], last=stamps[-1], sessions=len(set(day_values)), minutes=len(rows),
                   initial_shares=base, final_shares=shares, final_cash=cash, final_equity=equities[-1],
                   return_pct=(equities[-1] / capital - 1) * 100,
                   hold_return_pct=(hold_equities[-1] / capital - 1) * 100,
                   excess_pct=excess / capital * 100, max_drawdown_pct=maximum_dd * 100,
                   hold_drawdown_pct=hold_dd * 100, fees=paid, slippage_cost=impact_paid,
                   fills=len(all_fills), closed=len(cycles),
                   win_rate=sum(c['net'] > 0 for c in cycles) / len(cycles) if cycles else None,
                   waiting_minutes_mean=float(np.mean(wait)) if wait else None,
                   open_leg=leg, open_contribution=open_value, dividend_receivable=receivable,
                   rejections=rejection, scenario_net=scenario_net,
                   scenario_cycles=scenario_cycles, scenario_minutes=scenario_minutes)
    return summary, all_fills, cycles, np.asarray(equities), np.asarray(hold_equities)
