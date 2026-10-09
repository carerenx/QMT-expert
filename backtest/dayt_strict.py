"""Continuous replay of the real strategy loop; never calls its main/live entry.

Synchronous strategy fill waits advance the exchange clock, not the strategy.
Only subsequent OPEN events can fill. CLOSE events expose completed OHLCV.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import hashlib
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from output.analysis.compare_v51_v39_minute import Broker, Clock, load_strategy, OUT
from backtest.dayt_exchange import Exchange
from backtest.dayt_registry import register_with_legacy_loader
import pandas as pd


class StrictBroker(Broker):
    def __init__(self,daily,minute,rate,code='601869.SH',stock_name='601869',
                 initial_cash=100000.0,initial_shares=200):
        self.exchange=Exchange(cash=initial_cash,shares=initial_shares,rate=rate)
        super().__init__(daily,minute,0,initial_cash,initial_shares)
        self.code=code
        self.stock_name=stock_name
        self.all_daily=daily
        self.days={d:f for d,f in minute.groupby(minute.index.str[:8])}
        self.events=[]
        for day,bars in self.days.items():
            self.events.append((datetime.strptime(day+'092900','%Y%m%d%H%M%S'),'PREOPEN',day,0,None))
            stamps=[datetime.strptime(value,'%Y%m%d%H%M%S') for value in bars.index]
            intraday_gaps=[later-earlier for earlier,later in zip(stamps,stamps[1:])
                           if later.date()==earlier.date() and later-earlier<=timedelta(minutes=60)]
            bar_span=min(intraday_gaps) if intraday_gaps else timedelta(minutes=1)
            for i,(stamp,row) in enumerate(bars.iterrows()):
                if stamp[8:12]=='0930': continue  # opening auction is not a continuous minute
                close=datetime.strptime(stamp,'%Y%m%d%H%M%S')
                self.events.extend([(close-bar_span,'OPEN',day,i,row),
                                    (close,'CLOSE',day,i,row)])
        self.cursor=0
        self.current_day=None
        self.tick={}
        self.equities=[]
        self.recorded=set()
        self.opened_times={}
        self.cycle_details=[]
        self.risk_active_fn=None
        self.trader.query_stock_order=lambda account,oid:self.query_order(oid)
        self.trader.query_stock_orders=lambda account:list(self.exchange.orders.values())

    @property
    def cash(self): return self.exchange.cash
    @cash.setter
    def cash(self,value): self.exchange.cash=value
    @property
    def position(self): return self.exchange.shares
    @position.setter
    def position(self,value): self.exchange.shares=value
    @property
    def sellable(self): return self.exchange.sellable
    @sellable.setter
    def sellable(self,value): self.exchange.sellable=value

    def load_daily_snapshot(self,length,**kwargs):
        frame=self.daily.tail(length).copy()
        assert frame.index.max()<self.current_day
        return dict(adjusted=frame,raw=frame,last_complete_date=frame.index[-1])

    def get_full_tick(self,codes): return {self.code:dict(self.tick)}
    def query_account(self):
        return SimpleNamespace(cash=self.cash,total_asset=self.exchange.equity,
                               m_dAvailable=self.cash,m_dBalance=self.exchange.equity)
    def get_stock_name(self,*args): return self.stock_name
    def query_positions(self):
        instrument=self.code.split('.')[0]
        return [SimpleNamespace(stock_code=self.code,volume=self.position,
            can_use_volume=self.sellable,open_price=self.avg_cost,
            stock_name=self.stock_name,m_strInstrumentID=instrument,
            m_nVolume=self.position,m_nCanUseVolume=self.sellable,
            m_dOpenPrice=self.avg_cost)] if self.position else []

    def order(self,code,shares,style,price,*args):
        oid=self.exchange.submit(int(shares),float(price) if style=='FIX' else None,self.label)
        self.last_order_id=oid
        order=self.exchange.orders[oid]
        order.stock_code=code
        order.order_sysid=str(oid)
        return oid

    def cancel_order(self,oid):
        self.exchange.cancel(oid)
        self.query_order(oid)
        return True

    def transfer_short_to_risk(self, quantity):
        """Mirror a strategy bookkeeping transfer; this is not an exchange fill."""
        short_legs = list(self.book.legs.get('SHORT', []))
        if sum(shares for _, shares in short_legs) != int(quantity):
            raise RuntimeError('strict broker short/risk transfer mismatch')
        self.book.legs.setdefault('RISK', []).extend(short_legs)
        self.book.legs['SHORT'] = []
        self.book.cycle_gross['RISK'] = (
            self.book.cycle_gross.get('RISK', 0.0) +
            self.book.cycle_gross.get('SHORT', 0.0))
        self.book.cycle_gross['SHORT'] = 0.0
        opened = self.opened_times.pop('SHORT', None)
        if opened and 'RISK' not in self.opened_times:
            self.opened_times['RISK'] = opened

    def query_order(self,oid):
        order=self.exchange.orders[oid]
        if order.order_status in (53,54,56,57) and order.traded_volume and oid not in self.recorded:
            signed=order.traded_volume*(1 if order.signed_quantity>0 else -1)
            risk=order.label in ('RISK-OFF sell','RISK-RESTORE buy')
            short=(order.label=='REV-T sell' or 'buyback' in order.label or
                   order.label=='MOM short' or risk)
            group=('RISK' if risk else
                   ('MOM ' if order.label.startswith('MOM') else '')+
                   ('SHORT' if short else 'LONG'))
            if not self.book.legs.get(group): self.opened_times[group]=Clock.current.isoformat()
            gross,complete,cycle=self.book.record(oid,order.label,signed,order.traded_price)
            if complete:
                self.closed.append(cycle)
                self.cycle_details.append(dict(group=group,opened=self.opened_times.pop(group),
                                               closed=Clock.current.isoformat(),gross=cycle))
            self.recorded.add(oid)
        return order

    def advance(self):
        stamp,phase,day,i,row=self.events[self.cursor]
        self.cursor+=1
        Clock.current=stamp
        self.current_day=day
        self.daily=self.all_daily.loc[self.all_daily.index<day]
        self.bars=self.days[day]
        self.idx=i
        self.last_close=float(self.daily.iloc[-1].close)
        if phase=='PREOPEN':
            # Lifecycle maintenance only. Today's open/high/low/volume are not yet visible.
            self.exchange.opening(day,stamp.isoformat(),self.last_close,0,tradable=False)
            self.tick=dict(lastPrice=self.last_close,open=0.,high=0.,low=0.,lastClose=self.last_close,
                           volume=0.,pvolume=0.,amount=0.,bidPrice=[],askPrice=[],bidVol=[],askVol=[],
                           time=int(datetime.strptime(str(self.daily.index[-1])+'150000','%Y%m%d%H%M%S').timestamp()*1000),
                           stockStatus=0)
            return phase
        if phase=='OPEN':
            # This is an explicit fill-model restriction, not a claim about queue liquidity.
            locked=(row.high==row.low and abs(float(row.open)/self.last_close-1)>=.095)
            self.exchange.opening(day,stamp.isoformat(),float(row.open),float(row.volume)*100,
                tradable=stamp.strftime('%H:%M')>='09:30' and
                         (stamp.strftime('%H:%M')<'11:30' or '13:00'<=stamp.strftime('%H:%M')<'15:00'),
                one_sided_limit=locked)
            for oid in self.exchange.orders:
                self.query_order(oid)
            seen=self.bars.iloc[:i]
            price=float(row.open)
        else:
            self.exchange.close(float(row.close))
            seen=self.bars.iloc[:i+1]
            price=float(row.close)
            self.equities.append(dict(time=stamp.isoformat(),equity=self.exchange.equity,
                cash=self.cash,position=self.position,price=price,
                risk_active=bool(self.risk_active_fn and self.risk_active_fn()),
                unfinished_abs=sum(q for legs in self.book.legs.values() for _,q in legs),
                occupied=sum(q*(max(p,price) if ('SHORT' in group or group == 'RISK') else p)
                             for group,legs in self.book.legs.items() for p,q in legs)))
        self.tick=dict(lastPrice=price,open=float(self.bars.iloc[0].open),
                       high=max(price,float(seen.high.max())),low=min(price,float(seen.low.min())),
                       lastClose=self.last_close,volume=float(seen.volume.sum()),
                       pvolume=float(seen.volume.sum())*100,amount=float(seen.amount.sum()),
                       bidPrice=[price]*5,askPrice=[price]*5,bidVol=[10000]*5,askVol=[10000]*5,
                       time=int(stamp.timestamp()*1000),stockStatus=0)
        return phase

    def sleep(self,seconds):
        deadline=Clock.current+timedelta(seconds=seconds)
        while self.cursor<len(self.events) and self.events[self.cursor][0]<=deadline:
            self.advance()
        Clock.current=deadline


def replay(version,daily,minute,rate=.0005,overrides=None,legacy_carry=False,
           symbol='601869.SH',stock_name='601869',initial_cash=100000.0,
           initial_shares=200,research_short_cutoff=None):
    if legacy_carry and not version.startswith(('v39','v51')):
        raise ValueError('legacy carry is only defined for v39/v51')
    register_with_legacy_loader()
    mod=load_strategy(version)
    for key,value in (overrides or {}).items():
        if not hasattr(mod,key): raise ValueError('unknown research setting: '+key)
        setattr(mod,key,value)
    broker=StrictBroker(daily,minute,rate,symbol,stock_name,initial_cash,initial_shares)
    logs=[]
    class Factory:
        def __new__(cls): return broker
        load_daily_snapshot=StrictBroker.load_daily_snapshot
        get_history_data=Broker.get_history_data
    with ExitStack() as stack:
        replacements=[(mod,'MiniQMTConnector',Factory),(mod,'_time',Clock),(mod,'datetime',Clock),
            (Clock,'sleep',broker.sleep),(mod,'_log',lambda msg:logs.append(str(msg))),
            (mod,'get_logger',lambda:SimpleNamespace(close=lambda:None)),
            (mod,'set_global_conn',lambda *a:None),(mod,'order_shares',broker.order),
            (mod,'get_trade_detail_data',lambda a,b,k:broker.query_positions() if k=='POSITION' else [broker.query_account()]),
            (mod.cfg,'now_hms',lambda:Clock.current.strftime('%H:%M:%S'))]
        symbol_code=symbol.split('.')[0]
        for obj,name,value in ((mod,'STOCK_CODE',symbol_code),(mod,'STOCK_QMT',symbol),
                               (mod,'STOCK_NAME',stock_name),(mod.cfg,'STOCK_CODE',symbol_code),
                               (mod.cfg,'STOCK_QMT',symbol),(mod.cfg,'STOCK_NAME',stock_name)):
            if hasattr(obj,name): replacements.append((obj,name,value))
        for obj,name,value in replacements:
            stack.enter_context(patch.object(obj,name,value))
        if version.startswith('v39'): runner=mod.StrategyRunner(False)
        else:
            portfolio=mod.PortfolioRunner(False)
            runner=mod.StrategyRunner(portfolio,broker.code)
            portfolio.runners[broker.code]=runner
            broker.risk_active_fn=lambda: bool(
                getattr(runner, 'drawdown', None) and
                runner.drawdown.target_fraction < 1.0)
            if hasattr(runner, 'baseline_shares'):
                runner.baseline_shares=initial_shares
            stack.enter_context(patch.object(portfolio,'save_checkpoint',lambda *a,**kw:None))
            if research_short_cutoff and hasattr(runner, '_short_cycle_risk_reason'):
                original_risk_reason=runner._short_cycle_risk_reason
                def cutoff_risk_reason(price):
                    reason=original_risk_reason(price)
                    if reason:
                        return reason
                    cycle=runner.cycle
                    now_hms=Clock.current.strftime('%H:%M:%S')
                    if (cycle and cycle.direction=='SHORT' and cycle.quantity>0 and
                            now_hms>=research_short_cutoff):
                        return 'SESSION_END'
                    return ''
                stack.enter_context(patch.object(
                    runner,'_short_cycle_risk_reason',cutoff_risk_reason))
        original=runner._submit_order
        def submit(shares,price,label,style='COMPETE'):
            broker.label=label
            return original(shares,price,label,style)
        stack.enter_context(patch.object(runner,'_submit_order',submit))
        carry=None
        if legacy_carry:
            from backtest.dayt_legacy_carry import LegacyCarry
            carry=LegacyCarry(runner,broker,mod,version)
            carry.install(stack,patch)
        # Initialize current_day to first trading day so v39's _daily_init() can load daily snapshot
        if broker.events:
            broker.current_day = broker.events[0][2]
            # Filter daily data to only include dates before current_day (same as advance())
            broker.daily = broker.all_daily.loc[broker.all_daily.index < broker.current_day]
        gen=runner.run()
        failure=None
        previous_day=None
        try:
            while broker.cursor<len(broker.events):
                phase=broker.advance()
                if phase not in ('CLOSE','PREOPEN'): continue
                if carry:
                    try: carry.before_event(broker.current_day)
                    except RuntimeError as error:
                        failure='INVALID: '+str(error); break
                expected_carry_shares = None
                # v39 silently resets legs at a new day. Detect, do not repair it.
                if (not carry and previous_day and previous_day != broker.current_day and
                        version.startswith('v39') and
                        not getattr(mod, 'SUPPORTS_OVERNIGHT_CARRY', False) and
                        any(broker.book.legs.values())):
                    failure='INVALID: legacy daily reset would discard open execution legs at '+broker.current_day
                    break
                if (previous_day and previous_day != broker.current_day and
                        getattr(mod, 'SUPPORTS_OVERNIGHT_CARRY', False)):
                    expected_carry_shares = sum(
                        quantity for legs in broker.book.legs.values()
                        for _, quantity in legs)
                previous_day=broker.current_day
                if not version.startswith('v39') and phase=='PREOPEN':
                    try: portfolio._prepare_trading_day()
                    except RuntimeError as error:
                        failure='STOPPED: original portfolio rollover: '+str(error)
                        break
                try: next(gen)
                except StopIteration:
                    failure='STOPPED: strategy loop ended at '+Clock.current.isoformat(); break
                errors=[line for line in logs[-20:] if '[ERROR]' in line or 'init failed' in line]
                if errors:
                    failure='INVALID: '+str(errors[-1]); break
                if expected_carry_shares:
                    restored_shares = (
                        sum(quantity for _, quantity in runner.st.get('short_legs', [])) +
                        sum(quantity for _, quantity in runner.st.get('long_legs', [])) +
                        int(runner.st.get('mom_leg_shares', 0) or 0) +
                        sum(quantity for _, quantity in
                            runner.execution_book.legs.get('RISK', [])))
                    if restored_shares != expected_carry_shares:
                        failure = ('INVALID: overnight carry mismatch at {}: '
                                   'ledger={} strategy={}'.format(
                                       broker.current_day, expected_carry_shares,
                                       restored_shares))
                        break
        finally: gen.close()
        stopped_at=Clock.current.isoformat()
        # Mark retained inventory through period end; never silently delete open risk.
        while broker.cursor<len(broker.events): broker.advance()
        initial=initial_cash+initial_shares*float(minute.iloc[0].open)
        final=broker.exchange.equity
        equity=pd.Series([initial]+[row['equity'] for row in broker.equities])
        session_days=list(broker.days)
        durations=[sum(start[:10].replace('-','')<=day<=end[:10].replace('-','') for day in session_days)
                   for start,end in ([(row['opened'],row['closed']) for row in broker.cycle_details]+
                                    [(start,Clock.current.isoformat()) for start in broker.opened_times.values()])]
        checkpoint = (runner.checkpoint_record()
                      if hasattr(runner, 'baseline_shares') else {})
        return dict(version=version,rate=rate,failure=failure,stopped_at=stopped_at if failure else None,
            adaptation='LEGACY_CARRY_EXITS_ONLY' if carry else 'ORIGINAL',
            carry_events=carry.events if carry else [],
            owned_cycle_records=checkpoint.get('v52'),
            drawdown_record=checkpoint.get('drawdown'),
            settings=overrides or {},days=len(broker.days),
            account_net=final-initial,
            excess_net=final-(initial_cash+initial_shares*float(minute.iloc[-1].close)),
            maximum_drawdown=float((1-equity/equity.cummax()).max()),fees=sum(o.fee for o in broker.exchange.orders.values()),
            completed_cycles=len(broker.closed),open_legs=broker.book.legs,
            final_cash=broker.cash,final_position=broker.position,
            unfinished_abs_peak=max((row['unfinished_abs'] for row in broker.equities),default=0),
            capital_occupation_peak=max((row['occupied'] for row in broker.equities),default=0),
            longest_holding_sessions=max(durations,default=0),cycles=broker.cycle_details,
            unclosed=[dict(group=group,price=price,quantity=quantity,opened=broker.opened_times.get(group),
                           mark=float(minute.iloc[-1].close),
                           unrealized=quantity*((price-float(minute.iloc[-1].close)) if ('SHORT' in group or group == 'RISK') else (float(minute.iloc[-1].close)-price)))
                      for group,legs in broker.book.legs.items() for price,quantity in legs],
            rejection_counts=dict(Counter(row['reason'] for row in broker.exchange.rejections)),
            orders=[vars(order) for order in broker.exchange.orders.values()],
            trades=broker.exchange.fills,equity=broker.equities,rejections=broker.exchange.rejections,
            logs=logs[-30:],acceptance='NOT_ELIGIBLE' if failure else 'RESEARCH_ONLY')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',choices=list(register_with_legacy_loader().FILES),required=True)
    parser.add_argument('--rate',type=float,default=.0005)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--train',action='store_true',help='first 69 sessions only')
    parser.add_argument('--threshold',type=float,choices=[0.,.2,.4],default=.2)
    parser.add_argument('--no-directional',action='store_true')
    parser.add_argument('--no-overnight',action='store_true')
    parser.add_argument('--no-long',action='store_true',help='research control only')
    parser.add_argument('--legacy-carry',action='store_true',help='explicit offline overnight adaptation for v39/v51; not original baseline')
    args=parser.parse_args()
    if args.output.exists(): raise FileExistsError('refusing to overwrite existing research result')
    daily=pd.read_csv(OUT/'1d.csv',dtype={'time':str}).set_index('time').sort_index()
    minute=pd.read_csv(OUT/'1m.csv',dtype={'time':str}).set_index('time').sort_index()
    minute=minute.loc[minute.index.str[:8]>='20260421']
    if args.train: minute=minute.loc[minute.index.str[:8]<='20260730']
    settings=dict(DIRECTIONAL_THRESHOLD=args.threshold,DIRECTIONAL_ENABLED=not args.no_directional,
                  OVERNIGHT_ENABLED=not args.no_overnight,LONG_RESEARCH_DISABLED=args.no_long) if args.version.startswith('v52') else {}
    manifest=json.loads((ROOT/'backtest/dayt_golden_20260910/manifest.json').read_text(encoding='utf-8'))
    hashes={}
    for name,expected in manifest['files'].items():
        if '/core/' in name or '/infra/' in name or name.endswith(('1m.csv','1d.csv')) or name.endswith('.py') and '/DayT/' in name:
            actual=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
            if actual!=expected: raise RuntimeError('frozen dependency differs: '+name)
            hashes[name]=actual
    result=replay(args.version,daily,minute,args.rate,settings,args.legacy_carry)
    from backtest.dayt_registry import STRATEGIES
    for path in (Path(__file__),ROOT/'backtest/dayt_exchange.py',
                 ROOT/'analysis/compare_v51_v39_minute.py',
                 ROOT/'Stragety/MiniQMT_Stragety/core/directional_overnight.py',
                 ROOT/'Stragety/MiniQMT_Stragety/DayT'/STRATEGIES[args.version]):
        hashes[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    result['hashes']=hashes
    if args.legacy_carry:
        path=ROOT/'backtest/dayt_legacy_carry.py'
        hashes[path.relative_to(ROOT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print({k:result[k] for k in ('version','rate','failure','account_net','excess_net','maximum_drawdown','completed_cycles')})


if __name__=='__main__': main()
