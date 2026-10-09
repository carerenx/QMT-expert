"""Offline, independent-day replay of actual strategy loops on source minute closes.

No live runner/main is called. All broker interfaces and checkpoint writes are replaced.
v39's run-loop sleep statements alone become generator yields in memory.
"""
import ast
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Stragety/MiniQMT_Stragety')]
DAYT = ROOT / 'Stragety/MiniQMT_Stragety/DayT'
OUT = ROOT / 'analysis/v51_v39_minute'
FILES = {'v51': 'DayT_v51_IntradayStrength.py', 'v39': 'DayTradeing_v39_stragety_miniqmt.py'}
from core.execution_book import ExecutionBook


def fetch():
    sys.path.insert(0, str(ROOT / 'integrations/bigqmt/src'))
    from bigqmt_signal_trader.xtquant_compat import configure
    _, data = configure(account_id='8890145315', timeout_seconds=10)
    OUT.mkdir(exist_ok=True)
    for period, count in [('1m', 24000), ('1d', 400)]:
        frame = data.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'],
            ['601869.SH'], period=period, count=count, dividend_type='none', fill_data=False,
            timeout_seconds=10)['601869.SH']
        frame.to_csv(OUT / (period + '.csv'), index_label='time')


class Clock:
    current = datetime(2026, 1, 1)
    @classmethod
    def now(cls, tz=None): return cls.current
    @classmethod
    def time(cls): return cls.current.timestamp()
    monotonic = time
    @classmethod
    def strftime(cls, fmt): return cls.current.strftime(fmt)
    @classmethod
    def localtime(cls, ts=None): return cls.current.timetuple()
    @staticmethod
    def sleep(seconds): pass
    fromtimestamp = staticmethod(datetime.fromtimestamp)
    strptime = staticmethod(datetime.strptime)


def load_strategy(version):
    from backtest.dayt_registry import STRATEGIES
    path = DAYT / STRATEGIES[version]
    source = path.read_bytes()
    tree = ast.parse(source, filename=str(path))
    # v39 derivatives retain the same generator-based runner contract.  They
    # need the identical in-memory sleep-to-yield conversion for replay.
    if version.startswith('v39'):
        class Sleeps(ast.NodeTransformer):
            def visit_Expr(self, node):
                call = node.value
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and (
                    isinstance(call.func.value, ast.Name) and call.func.value.id == '_time' and call.func.attr == 'sleep'):
                    return ast.copy_location(ast.Expr(ast.Yield(call.args[0])), node)
                return self.generic_visit(node)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'StrategyRunner':
                for method in node.body:
                    if isinstance(method, ast.FunctionDef) and method.name == 'run': Sleeps().visit(method)
    mod = ModuleType('replay_' + version)
    mod.__file__ = str(path)
    exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), mod.__dict__)
    return mod


class Broker:
    code = '601869.SH'
    data_connected = trade_connected = True
    def __init__(self, daily, bars, slip, cash=100000., shares=200):
        self.daily, self.bars, self.slip = daily, bars, slip
        self.cash, self.position, self.sellable = cash, shares, shares
        self.idx = 0
        self.orders, self.trades = {}, []
        self.last_order_id = None
        self._account_obj = SimpleNamespace(account_id='OFFLINE')
        self.trader = SimpleNamespace(query_stock_order=lambda account, oid: self.orders[oid],
            query_stock_positions=lambda account: self.query_positions(),
            query_stock_orders=lambda account: list(self.orders.values()))
        self.avg_cost = float(bars.iloc[0].open)
        self.last_close = float(daily.iloc[-1].close)
        self.history = []
        self.book, self.closed = ExecutionBook(), []
        self.label = ''

    def connect_data(self): return True
    def connect_trade(self): return True
    def disconnect(self): pass
    def refresh_daily_cache(self, *args, **kwargs): pass
    def cancel_order(self, *args): return True
    def get_stock_name(self, *args): return self.code.split('.')[0]
    def load_daily_snapshot(self, length, **kwargs):
        frame = self.daily.tail(length).copy()
        assert frame.index.max() < self.bars.index[0][:8]
        self.history.append(str(frame.index.max()))
        return dict(adjusted=frame, raw=frame, last_complete_date=frame.index[-1])
    def get_history_data(self, length, period, field):
        return {self.code: self.daily.tail(length)[field].tolist()}
    def get_full_tick(self, codes):
        seen = self.bars.iloc[:self.idx + 1]
        row = seen.iloc[-1]
        price = float(row.close)
        return {self.code: dict(lastPrice=price, open=float(self.bars.iloc[0].open),
            high=float(seen.high.max()), low=float(seen.low.min()), lastClose=self.last_close,
            volume=float(seen.volume.sum()), pvolume=float(seen.volume.sum())*100, amount=float(seen.amount.sum()),
            bidPrice=[price]*5, askPrice=[price]*5, bidVol=[10000]*5, askVol=[10000]*5,
            time=int(Clock.time()*1000), stockStatus=0)}
    def query_positions(self):
        # The stub must report the symbol actually being replayed.  Strategies
        # match holdings on m_strInstrumentID, so a hardcoded id here makes every
        # symbol other than that one look like a zero position: the strategy sees
        # no sellable base, disables both lanes, and never trades.  StrictBroker
        # already derives the instrument from self.code; this mirrors it.
        instrument = self.code.split('.')[0]
        return [SimpleNamespace(stock_code=self.code, volume=self.position, can_use_volume=self.sellable,
            open_price=self.avg_cost, stock_name=instrument, m_strInstrumentID=instrument,
            m_nVolume=self.position, m_nCanUseVolume=self.sellable, m_dOpenPrice=self.avg_cost)] if self.position else []
    def query_account(self):
        equity = self.cash + self.position * float(self.bars.iloc[self.idx].close)
        return SimpleNamespace(cash=self.cash, total_asset=equity, m_dAvailable=self.cash, m_dBalance=equity)
    def order(self, code, shares, style, price, *args):
        # Idealized close execution: no future minute is read or returned to strategy.
        # This cannot reproduce sub-minute queue/latency and is disclosed in the report.
        oid = len(self.orders) + 1
        self.last_order_id = oid
        fill, qty = 0., 0
        if self.idx < len(self.bars):
            nxt = self.bars.iloc[self.idx]
            fill = round(float(nxt.close) + (self.slip if shares > 0 else -self.slip), 2)
            marketable = style != 'FIX' or (float(nxt.close) <= price if shares > 0 else float(nxt.close) >= price)
            if style == 'FIX':
                fill = min(fill, price) if shares > 0 else max(fill, price)
            # No inferred fills on zero-volume bars or unavailable T+1 shares.
            if marketable and nxt.volume > 0:
                qty = min(abs(shares), int(self.cash / fill / 100)*100 if shares > 0 else self.sellable)
            if qty:
                signed = qty if shares > 0 else -qty
                self.cash -= signed * fill
                self.position += signed
                if signed < 0: self.sellable -= qty
                self.trades.append(dict(time=str(nxt.name), decision_time=str(self.bars.index[self.idx]),
                    label=self.label, shares=signed, price=fill, turnover=qty*fill))
                gross, completed, cycle = self.book.record(oid, self.label, signed, fill)
                if completed: self.closed.append(cycle)
        self.orders[oid] = SimpleNamespace(order_id=oid, order_sysid=str(oid), stock_code=code,
            order_status=56 if qty == abs(shares) else 53, traded_volume=qty,
            traded_price=fill if qty else 0., order_volume=abs(shares))
        return oid


def replay(version, daily, bars, slip=0, initial_cash=100000.0,
           initial_shares=200, symbol='601869.SH', overrides=None,
           cfg_overrides=None):
    mod = load_strategy(version)
    for key, value in (overrides or {}).items():
        if not hasattr(mod, key):
            raise ValueError('unknown research setting: ' + key)
        setattr(mod, key, value)
    # Shared-config knobs (e.g. STOP_LOSS_PCT) live on core.config, not on the
    # strategy module.  They are scoped to this replay through the ExitStack
    # below so variants in one process cannot leak into each other.
    cfg_overrides = dict(cfg_overrides or {})
    for key in cfg_overrides:
        if not hasattr(mod.cfg, key):
            raise ValueError('unknown config setting: ' + key)
    broker = Broker(
        daily, bars, slip, cash=initial_cash, shares=initial_shares)
    broker.code = symbol
    logs = []
    class ConnectorFactory:
        def __new__(cls): return broker
        load_daily_snapshot = Broker.load_daily_snapshot
        get_history_data = Broker.get_history_data
    Clock.current = datetime.strptime(bars.index[0], '%Y%m%d%H%M%S')
    with ExitStack() as stack:
        replacements = [(mod, 'MiniQMTConnector', ConnectorFactory), (mod, '_time', Clock),
                (mod, 'datetime', Clock), (mod, '_log', lambda msg: logs.append(str(msg))),
                (mod, 'get_logger', lambda: SimpleNamespace(close=lambda: None)),
                (mod, 'set_global_conn', lambda *a: None), (mod, 'order_shares', broker.order),
                (mod, 'get_trade_detail_data', lambda a,b,kind: broker.query_positions() if kind == 'POSITION' else [broker.query_account()]),
                (mod.cfg, 'now_hms', lambda: Clock.current.strftime('%H:%M:%S'))]
        replacements.extend(
            (mod.cfg, key, value) for key, value in cfg_overrides.items())
        symbol_code = symbol.split('.')[0]
        for obj, name, value in (
                (mod, 'STOCK_CODE', symbol_code),
                (mod, 'STOCK_QMT', symbol),
                (mod.cfg, 'STOCK_CODE', symbol_code),
                (mod.cfg, 'STOCK_QMT', symbol)):
            if hasattr(obj, name):
                replacements.append((obj, name, value))
        for obj, name, value in replacements:
            stack.enter_context(patch.object(obj, name, value))
        if not version.startswith('v39'):
            portfolio = mod.PortfolioRunner(False)
            runner = mod.StrategyRunner(portfolio, broker.code)
            portfolio.runners[broker.code] = runner
            if hasattr(runner, 'baseline_shares'):
                runner.baseline_shares = broker.position
            stack.enter_context(patch.object(portfolio, 'save_checkpoint', lambda *a, **kw: None))
        else:
            runner = mod.StrategyRunner(False)
        original_submit = runner._submit_order
        def submit(shares, price, label, style='COMPETE'):
            broker.label = label
            return original_submit(shares, price, label, style)
        stack.enter_context(patch.object(runner, '_submit_order', submit))
        gen = runner.run()
        equity, failure = [], None
        for i, stamp in enumerate(bars.index):
            broker.idx = i
            Clock.current = datetime.strptime(stamp, '%Y%m%d%H%M%S')
            try:
                next(gen)
            except StopIteration:
                failure = 'strategy loop stopped at ' + stamp
                break
            equity.append(broker.cash + broker.position * float(bars.iloc[i].close))
        gen.close()
        errors = [line for line in logs if '[ERROR]' in line or 'init failed' in line]
        if errors: failure = errors[:3]
        if not runner.st.get('initialized'): failure = failure or 'not initialized'
        opening, closing = float(bars.iloc[0].open), float(bars.iloc[-1].close)
        final = broker.cash + broker.position*closing
        hold = initial_cash + initial_shares*closing
        # A net-position mismatch measures residual exposure, not all hedged open legs.
        short_left = sum(q for p,q in runner.st.get('short_legs', []))
        long_left = sum(q for p,q in runner.st.get('long_legs', []))
        return dict(date=bars.index[0][:8], version=version, slip=slip, failure=failure,
            bars=len(bars), trades=broker.trades, orders=len(broker.orders),
            turnover=sum(t['turnover'] for t in broker.trades),
            initial_equity=initial_cash+initial_shares*opening, final_equity=final,
            account_gross=final-initial_cash-initial_shares*opening,
            hold_gross=initial_shares*(closing-opening),
            excess_gross=final-hold, final_position=broker.position,
            position_gap=broker.position-initial_shares,
            short_unclosed=short_left, long_unclosed=long_left,
            mom_unclosed=runner.st.get('mom_leg_shares',0), state=runner.st.get('fstate'),
            cycles=len(broker.closed), cycle_gross=broker.closed,
            ledger_unclosed={k:sum(q for p,q in v) for k,v in broker.book.legs.items()},
            strategy_reported_gross=runner.total_pnl,
            max_drawdown=float(np.max(1-np.array(equity)/np.maximum.accumulate(equity))) if equity else None,
            logs=[line for line in logs if any(k in line for k in ('[ERROR]', '[EXECUTION]', '[REV-T done]', '[CYCLE-CLOSED]'))])


def main():
    if '--fetch' in sys.argv: fetch()
    daily = pd.read_csv(OUT/'1d.csv', dtype={'time': str}).set_index('time').sort_index()
    minute = pd.read_csv(OUT/'1m.csv', dtype={'time': str}).set_index('time').sort_index()
    results, skipped = [], []
    for date, bars in minute.groupby(minute.index.str[:8]):
        hist = daily.loc[daily.index < date]
        if len(bars) < 230 or bars.index[0][8:12] not in ('0930','0931') or len(hist)<80:
            skipped.append(dict(date=date,bars=len(bars),reason='partial session or insufficient history'))
            continue
        for version in FILES:
            for slip in (0., .01):
                result = replay(version,hist,bars,slip)
                results.append(result)
                if result['failure']:
                    print('FAILED', version,date,result['failure'],flush=True)
                    (OUT/'results.json').write_text(json.dumps(dict(results=results,skipped=skipped),ensure_ascii=False,indent=2),encoding='utf-8')
                    return
        print(date,[(r['version'],r['slip'],len(r['trades']),round(r['excess_gross'],2)) for r in results[-4:]],flush=True)
        if '--smoke' in sys.argv: break
    payload = dict(results=results, skipped=skipped, hashes={v:hashlib.sha256((DAYT/f).read_bytes()).hexdigest() for v,f in FILES.items()})
    (OUT/'results.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__': main()
