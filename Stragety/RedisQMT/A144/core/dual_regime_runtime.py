# -*- coding: utf-8 -*-
"""Minute-clock Redis QMT portfolio runtime with restart-safe ownership.

Market API basis: Python API pp.70-71 (OHLCV, 1d/1m, unadjusted/front).
Timeout/chunk/bypass_formula and order methods are verified project shim APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .dual_regime_execution import Journal, RedisBroker, exclusive_state, fee, new_state, price_limit


ROOT = Path(__file__).resolve().parents[4]
TIMES = list(pd.date_range('2000-01-01 09:31', '2000-01-01 11:30', freq='min').strftime('%H%M%S'))
TIMES += list(pd.date_range('2000-01-01 13:01', '2000-01-01 15:00', freq='min').strftime('%H%M%S'))


def _log(message):
    print(f'[{datetime.now():%H:%M:%S}] {message}', flush=True)


def normalize(frame, end, daily=True):
    if frame is None or frame.empty:
        raise ValueError('Missing Redis QMT history')
    result = frame.copy()
    result.index = result.index.astype(str).str[:8 if daily else 14]
    if result.index.duplicated().any() or not result.index.str.fullmatch(r'\d{8}' if daily else r'\d{14}').all():
        raise ValueError('Duplicate or invalid market timestamps')
    result = result.sort_index().loc[lambda rows: rows.index <= end]
    columns = ['open', 'high', 'low', 'close', 'volume', 'amount']
    result[columns] = result[columns].apply(pd.to_numeric, errors='raise')
    if result.empty or not result[columns].apply(lambda column: column.map(math.isfinite)).all().all():
        raise ValueError('Empty/nonfinite market history')
    if (result[['open', 'high', 'low', 'close']] <= 0).any().any():
        raise ValueError('Zero/negative market price')
    if (result.high < result[['open', 'close', 'low']].max(axis=1)).any() or (result.low > result[['open', 'close', 'high']].min(axis=1)).any():
        raise ValueError('Inconsistent OHLC')
    if (result[['volume', 'amount']] < 0).any().any():
        raise ValueError('Negative market volume/amount')
    return result


def load_universe(path, dates, today, lookback=5):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not data.get('source') or data.get('valid_through', '') < today:
        raise ValueError('Universe source/valid_through missing or expired')
    snapshots = data['snapshots']
    for effective, codes in snapshots.items():
        if len(codes) != 500 or len(set(codes)) != 500:
            raise ValueError('Every CSI500 snapshot must contain 500 distinct codes')
        if not all(pd.Series(codes).str.fullmatch(r'\d{6}\.(SH|SZ)')):
            raise ValueError('Invalid CSI500 code')
    memberships = {}
    for date in dates:
        available = [effective for effective in snapshots if effective <= date]
        if available:
            memberships[date] = set(snapshots[max(available)])
    if any(date not in memberships for date in dates[-max(6, lookback + 1):]):
        raise ValueError('Need historical membership for all six breadth-confirmation sessions')
    current = [effective for effective in snapshots if effective <= today]
    if not current:
        raise ValueError('No current membership snapshot')
    return memberships, set(snapshots[max(current)])


class Market:
    def __init__(self, xtdata, universe, policy):
        self.xtdata = xtdata
        self.universe = universe
        self.policy = policy
        self.day = ''

    def request(self, codes, period, end, count, adjustment='none', start=''):
        return self.xtdata.get_market_data_ex(
            field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
            stock_list=sorted(codes), period=period, start_time=start, end_time=end,
            count=count, dividend_type=adjustment, fill_data=False,
            chunk_size=40, timeout_seconds=30, bypass_formula=True)

    def prepare(self, today, state):
        if self.day == today:
            return
        calendar = self.xtdata.get_trading_dates('SH', end_time=today, count=300)
        dates = []
        for value in calendar or []:
            text = str(value)
            if len(text) == 8 and text.isdigit():
                dates.append(text)
            elif isinstance(value, (int, float)):
                dates.append(pd.Timestamp(value, unit='ms', tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y%m%d'))
            else:
                raise ValueError('Unsupported trading calendar timestamp')
        dates = sorted(set(dates))
        if today not in dates:
            raise ValueError('Today is not a confirmed trading day')
        prior = [date for date in dates if date < today]
        if len(prior) < 200:
            raise ValueError('Incomplete trading calendar')
        asof = prior[-1]
        memberships, current = load_universe(self.universe, prior, today, self.policy.PRIMARY_BREADTH_LOOKBACK)
        stock_codes = set.union(*memberships.values())
        stock_codes.update(code for code, position in state['positions'].items() if position['sleeve'] == 'primary')
        codes = stock_codes | set(self.policy.CODES) | {'000905.SH'}
        raw = self.request(codes, '1d', asof, 300)
        adjusted = self.request(stock_codes, '1d', asof, 300, 'front')
        daily = {code: normalize(raw.get(code), asof) for code in codes}
        for code, frame in daily.items():
            if frame.index[-1] != asof:
                raise ValueError('Stale daily history: ' + code)
            minimum = self.policy.REVERSION_MA_DAYS if code in self.policy.REVERSION_CODES else 61 if code in self.policy.COMMODITY_CODES else 60 if code == '000905.SH' else 50
            if len(frame) < minimum:
                raise ValueError('Insufficient daily warm-up: ' + code)
        parts = []
        for code in sorted(stock_codes):
            adj = normalize(adjusted.get(code), asof)
            frame = daily[code].join(adj[['open', 'high', 'low', 'close']].add_prefix('adj_'), how='inner')
            if frame.index[-1] != asof:
                raise ValueError('Missing adjusted close: ' + code)
            frame['date'] = frame.index
            frame['code'] = code
            parts.append(frame.reset_index(drop=True))
        primary = self.policy.primary_signals(pd.concat(parts, ignore_index=True), daily['000905.SH'], memberships)
        for date in prior[-max(6, self.policy.PRIMARY_BREADTH_LOOKBACK + 1):]:
            rows = primary.loc[primary.date.eq(date) & primary.member]
            if set(rows.code) != memberships[date] or rows.ma50.isna().any():
                raise ValueError('Incomplete historical breadth: ' + date)
        latest = primary.loc[primary.date.eq(asof)]
        breadth = float(latest.breadth.iloc[0])
        market_ok = bool(latest.market_strict.iloc[0])
        self.weak = not market_ok or breadth < self.policy.BREADTH_MIN
        self.secondary = self.policy.secondary_signals(daily, not market_ok)
        self.candidates = latest.loc[latest.entry & latest.code.isin(current)].sort_values(['score', 'code'], ascending=[False, True])
        self.primary = primary
        self.daily = daily
        self.dates = prior + [today]
        self.asof = asof
        self.day = today
        state.setdefault('universe_audit', {})[today] = hashlib.sha256(Path(self.universe).read_bytes()).hexdigest()
        _log(f'[Regime] completed={asof} index_ok={market_ok} breadth={breadth:.1%} secondary_enabled={self.weak}')

    def bars(self, codes, date, signal):
        data = self.request(codes, '1m', signal, -1, start=date + '093100')
        result = {}
        required = [date + value for value in TIMES if date + value <= signal]
        for code in codes:
            frame = normalize(data.get(code), signal, daily=False)
            frame = frame.loc[frame.index.isin(required)]
            if list(frame.index) != required:
                raise ValueError('Missing current-session minute(s): ' + code)
            result[code] = frame
        return result

    def recover_secondary_exits(self, code, position, current):
        start = position.get('last_exit_signal', position['entry_stamp'])
        if start[:8] < max(self.dates[0], '20251010'):
            raise ValueError('Held position exceeds verified calendar/history; manual reconciliation required')
        frames = [current]
        if start[:8] < self.day:
            end = self.asof + '150000'
            data = self.request([code], '1m', end, -1, start=start[:8] + '093100')
            previous = normalize(data.get(code), end, daily=False)
            required = [date + value for date in self.dates if start[:8] <= date < self.day for value in TIMES]
            previous = previous.loc[previous.index.isin(required)]
            if list(previous.index) != required:
                raise ValueError('Missing downtime exit history: ' + code)
            frames.insert(0, previous)
        history = pd.concat(frames)
        for stamp, row in history.loc[history.index >= start].iterrows():
            if stamp == position.get('last_exit_signal') or stamp < position['entry_stamp'] or stamp[8:] == '150000':
                continue
            # Each prior session uses its own completed-day MA5, never today's
            # MA5 retroactively when catching up after downtime.
            daily = self.daily[code].loc[self.daily[code].index < stamp[:8]]
            if len(daily) < self.policy.REVERSION_RECOVERY_MA_DAYS:
                raise ValueError('Missing downtime daily warm-up: ' + code)
            signal = dict(self.secondary[code], recovery_ma5=float(daily.close.tail(self.policy.REVERSION_RECOVERY_MA_DAYS).mean()))
            day_age = sum(position['entry_date'] <= day < stamp[:8] for day in self.dates)
            age = day_age * 240 + TIMES.index(stamp[8:]) - TIMES.index(position['entry_stamp'][8:])
            reason = self.policy.secondary_exit(position, signal, row, stamp[8:], age)
            position['last_exit_signal'] = stamp
            if reason:
                return reason
        return ''


def build_intents(policy, market, journal, broker_positions, broker_cash, stamp, signal, bars):
    state = journal.state
    date = stamp[:8]
    if state['day'] != date:
        state['parents'] = {}
        state['counts'] = {}
        state['day'] = date
    equity = state['cash'] + sum(position['quantity'] * bars[code].iloc[-1]['close']
        for code, position in state['positions'].items())
    available = min(state['cash'], broker_cash)
    # No same-poll sale proceeds are available to any BUY in this batch.
    for order in journal.active():
        if order['side'] == 'BUY':
            remaining = order['quantity'] - order['applied_quantity']
            available -= remaining * order['limit'] + fee(remaining * order['limit'], 'BUY', date, order['sleeve'])
    for code, position in state['positions'].items():
        if position['exit_reason']:
            continue
        if position['sleeve'] == 'primary':
            history = market.primary.loc[market.primary.code.eq(code)]
            pending = history.loc[history.date.ge(position['entry_date']) & history.date.gt(position.get('last_exit_date', ''))]
            for _, row in pending.iterrows():
                position['exit_reason'] = policy.primary_exit(position, row, history)
                position['last_exit_date'] = str(row['date'])
                if position['exit_reason']:
                    break
        else:
            position['exit_reason'] = market.recover_secondary_exits(code, position, bars[code])
        if position['exit_reason']:
            state['parents'].pop(code, None)
    for code in list(state['parents']):
        parent = state['parents'][code]
        age = TIMES.index(signal[8:]) - TIMES.index(parent['signal'][8:])
        if age >= 5 or (parent['sleeve'] == 'secondary' and not market.weak):
            state['parents'].pop(code)
    choices = []
    if not market.weak and '093100' <= signal[8:] <= '093500':
        choices = [(row.code, 'primary', float(row.weight), float(row.amount20)) for row in market.candidates.itertuples()]
    if market.weak:
        for code in sorted(policy.CODES):
            frame = bars[code]
            midpoint = (frame.high.max() + frame.low.min()) / 2
            if policy.secondary_entry(market.secondary[code], frame.iloc[-1], midpoint, signal[8:], True):
                choices.append((code, 'secondary', policy.SECONDARY_WEIGHT, float(market.daily[code].amount.tail(20).mean())))
    for code, sleeve, weight, amount20 in choices:
        if code in state['parents'] or code in state['positions'] or code in broker_positions or journal.active(code):
            continue
        count = state['counts'].get(code, 0)
        if sleeve == 'primary':
            primary_count = sum(value for symbol, value in state['counts'].items() if symbol not in policy.CODES)
            slots = {symbol for symbol, item in state['positions'].items() if item['sleeve'] == 'primary'}
            slots.update(symbol for symbol, item in state['parents'].items() if item['sleeve'] == 'primary')
            cooldown = state['cooldowns'].get(code)
            if cooldown and sum(cooldown['date'] < day <= date for day in market.dates) < cooldown['sessions']:
                continue
            if primary_count >= policy.MAX_PRIMARY_ENTRIES or len(slots) >= policy.MAX_PRIMARY_POSITIONS:
                continue
        elif count >= policy.MAX_SECONDARY_ENTRIES:
            continue
        reserved = sum(max(0., item['budget'] - item['spent']) for item in state['parents'].values())
        budget = min(equity * weight, amount20 * .001, max(0., available - reserved))
        if budget < bars[code].iloc[-1]['close'] * 101 + 5:
            continue
        state['parents'][code] = dict(sleeve=sleeve, signal=signal, budget=budget, spent=0.)
        state['counts'][code] = count + 1
    intents = []
    for code in sorted(set(state['parents']) | set(state['positions'])):
        if journal.active(code):
            continue
        position = state['positions'].get(code)
        parent = state['parents'].get(code)
        side = 'SELL' if position and position['exit_reason'] else 'BUY' if parent else ''
        if not side:
            continue
        sleeve = position['sleeve'] if side == 'SELL' else parent['sleeve']
        row = bars[code].iloc[-1]
        limit = price_limit(float(row['close']), side, sleeve)
        quantity = math.floor(float(row['volume']) * .05) * 100  # QMT volume is lots.
        prior = float(market.daily[code].close.iloc[-1])
        change = limit / prior - 1
        boundary = .195 if code.startswith(('300', '301', '688', '689')) else .048
        if sleeve == 'secondary':
            if abs(change) >= .095:
                continue
        elif (side == 'BUY' and change >= boundary) or (side == 'SELL' and change <= -boundary):
            continue
        if side == 'SELL':
            quantity = min(quantity, position['quantity'], broker_positions.get(code, {}).get('sellable', 0))
        else:
            budget = min(available, parent['budget'] - parent['spent'])
            quantity = min(quantity, max(0, int(budget / limit // 100) * 100))
            while quantity > 0 and quantity * limit + fee(quantity * limit, side, date, sleeve) > budget:
                quantity -= 100
            if sleeve == 'primary' and code.startswith(('688', '689')) and quantity < 200:
                quantity = 0
            if quantity > 0:
                available -= quantity * limit + fee(quantity * limit, side, date, sleeve)
        if quantity <= 0:
            continue
        key = f'A144V12-{signal}-{code[:6]}-{side[0]}'
        intents.append(dict(key=key, code=code, sleeve=sleeve, side=side, quantity=int(quantity),
            limit=limit, date=date, signal=signal, execution_stamp=stamp))
    return intents


def run(policy, argv=None):
    parser = argparse.ArgumentParser(description='A144 primary + weak-market secondary Redis QMT live portfolio')
    parser.add_argument('--mode', choices=('check', 'live'), default='check')
    parser.add_argument('--universe', type=Path, required=True, help='verified dated CSI500 membership JSON')
    parser.add_argument('--state', type=Path, default=Path(policy.__file__).parent / 'state/dual_regime_v12.json')
    parser.add_argument('--capital', type=float, default=1e6, help='dedicated initial budget, only on first start')
    args = parser.parse_args(argv)
    if not math.isfinite(args.capital) or args.capital <= 0:
        parser.error('capital must be finite and positive')
    if not args.universe.is_file():
        parser.error('verified universe file does not exist')
    sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))
    import xtquant
    if Path(xtquant.__file__).resolve().parent != ROOT / 'integrations/bigqmt/src/xtquant':
        raise RuntimeError('Expected project BigQMT shim; native SDK already imported')
    from bigqmt_signal_trader.xtquant_compat import StockAccount, configure, load_client_config
    from Stragety.RedisQMT.A144.config import ACCOUNT
    config = load_client_config()
    if str(config.get('account_id')) != ACCOUNT or config.get('redis_config', {}).get('transport', 'redis') != 'redis':
        raise RuntimeError('Expected configured strategy account over Redis transport')
    account_lock = Path(policy.__file__).parent / 'state' / f'{policy.STRATEGY}_{ACCOUNT}'
    with exclusive_state(account_lock):
        trader, xtdata = configure(account_id=ACCOUNT, timeout_seconds=30)
        ping = trader.client.call('ping')
        if not isinstance(ping, dict) or not ping.get('pong') or str(ping.get('account_id')) != ACCOUNT:
            raise RuntimeError('Redis QMT ping/account validation failed')
        account = StockAccount(ACCOUNT)
        broker = RedisBroker(trader, account)
        if not args.state.exists():
            initial_available = broker.snapshot()[0]
            if args.capital > initial_available:
                raise RuntimeError('Initial dedicated capital exceeds actual available cash; set --capital explicitly')
        state = json.loads(args.state.read_text(encoding='utf-8')) if args.state.exists() else new_state(ACCOUNT, args.capital)
        if state['schema'] != 1 or state['account'] != ACCOUNT or state['strategy'] != policy.STRATEGY:
            raise RuntimeError('Checkpoint schema/account/strategy mismatch')
        # Never silently continue an existing position with edited strategy code.
        implementation = [Path(policy.__file__), Path(__file__),
                          Path(__file__).with_name('dual_regime_execution.py'),
                          Path(__file__).with_name('trend.py')]
        code_hash = hashlib.sha256(b''.join(path.read_bytes() for path in implementation)).hexdigest()
        if state.get('policy_sha256', code_hash) != code_hash:
            raise RuntimeError('Strategy changed since checkpoint; explicit migration required')
        state['policy_sha256'] = code_hash
        journal = Journal(args.state, state)
        market = Market(xtdata, args.universe, policy)
        journal.save()
        while True:
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            date = now.strftime('%Y%m%d')
            stamp = now.strftime('%Y%m%d%H%M00')
            unresolved = journal.reconcile(broker, date)
            cash, actual = broker.snapshot()
            journal.check_positions(actual)
            if args.mode == 'check':
                market.prepare(date, state)
                _log(f'[Check] owned={len(state["positions"])} pending={len(journal.active())} unresolved={len(unresolved)}; no orders submitted')
                return 0
            if unresolved:
                if '093200' <= stamp[8:] <= '113000' or '130100' <= stamp[8:] <= '145600':
                    for key in unresolved:
                        journal.recover_submission(broker, key, stamp)
                _log('[Recovery] awaiting broker identity confirmation; no replacement: ' + ','.join(unresolved))
                time.sleep(5)
                continue
            journal.cancel_stale(broker, stamp)
            session = '093200' <= stamp[8:] <= '113000' or '130100' <= stamp[8:] <= '145600'
            if not session:
                time.sleep(5)
                continue
            market.prepare(date, state)
            completed = [date + value for value in TIMES if date + value < stamp]
            if not completed:
                time.sleep(2)
                continue
            signal = completed[-1]
            if state['last_signal'] >= signal:
                time.sleep(2)
                continue
            codes = set(policy.CODES) | set(state['positions']) | set(state['parents'])
            if not market.weak and signal[8:] <= '093500':
                codes.update(market.candidates.code)
            bars = market.bars(codes, date, signal)
            intents = build_intents(policy, market, journal, actual, cash, stamp, signal, bars)
            journal.save()  # Persist parent budgets and latched exits before RPCs.
            for intent in intents:
                journal.submit(broker, intent, now.isoformat())
                _log(f'[Order] {intent["side"]} {intent["code"]} qty={intent["quantity"]} limit={intent["limit"]:.3f} sleeve={intent["sleeve"]}')
            state['last_signal'] = signal
            journal.save()
            time.sleep(2)
