"""
V6: Tiered Position + T3 Exclusion
Key changes from V5:
  1. T3 excluded (光学光电子/计算机/军工) in normal markets
  2. Higher bottom threshold (>=15)
  3. Tiered weights: TREND(60%) + CORE(30%) + BOTTOM(10%)
  4. Equal-weight comparison for each tier
"""
import sys, os, time, random, csv, requests, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v6_final\data'
os.makedirs(DATA_DIR, exist_ok=True)
EM_SESSION = requests.Session(); EM_SESSION.headers.update({'User-Agent':UA})
_em_last=[0.0]
def em_get(url,params=None,headers=None,timeout=15):
    wait=1.3-(time.time()-_em_last[0])
    if wait>0: time.sleep(wait+random.uniform(0.1,0.5))
    try: return EM_SESSION.get(url,params=params,headers=headers,timeout=timeout)
    finally: _em_last[0]=time.time()

def is_mainboard(code):
    for p in ['300','301','688','689','8','4']:
        if code.startswith(p): return False
    return True

TIER1={'通信设备','元件','半导体'}
TIER2={'玻璃玻纤','电子化学品'}
TIER3={'光学光电子','计算机设备','国防军工'}

TECH_SECTORS=[('BK0448','通信设备'),('BK1036','半导体'),('BK1038','光学光电子'),
    ('BK1039','电子化学品'),('BK0459','元件'),('BK0735','计算机设备'),
    ('BK1204','国防军工'),('BK0546','玻璃玻纤')]

print('='*60)
print('V6 FINAL: Tiered Position + T3 Exclusion')
print('='*60)

# ===== SCORING =====
def compute(raw, screen_end, code):
    closes=[float(k[2]) for k in raw[:screen_end]]
    highs=[float(k[3]) for k in raw[:screen_end]]
    lows=[float(k[4]) for k in raw[:screen_end]]
    volumes=[float(k[5]) for k in raw[:screen_end]]
    if len(closes)<30: return 0,0,{}
    current=closes[-1]; n=len(closes)
    ma5=sum(closes[-5:])/5; ma10=sum(closes[-10:])/10
    ma20=sum(closes[-20:])/20
    ma60=sum(closes[-60:])/60 if n>=60 else ma20
    pct_ma60=(current-ma60)/ma60*100
    vol20=sum(volumes[-20:])/20; vol5=sum(volumes[-5:])/5
    vol_ratio=vol5/vol20 if vol20>0 else 1
    chg20d=(current-closes[-21])/closes[-21]*100 if n>21 else 0
    high10=max(highs[-10:]); dd10d=(current-high10)/high10*100
    bb_std=np.std(closes[-20:]); bb_width=(4*bb_std)/ma20*100 if ma20>0 else 50
    range_20d=(max(highs[-20:])-min(lows[-20:]))/ma20*100 if ma20>0 else 50

    # TREND (0-80)
    trend=0
    if current>ma20>ma60: trend+=22
    elif current>ma20: trend+=12
    if ma5>ma10>ma20: trend+=10
    elif ma5>ma20: trend+=5
    if 0<pct_ma60<=15: trend+=18
    elif 15<pct_ma60<=30: trend+=15
    elif 30<pct_ma60<=50: trend+=8
    elif 50<pct_ma60<=80: trend+=4
    if 1.2<vol_ratio<2.0: trend+=10
    elif 1.0<vol_ratio<=1.2: trend+=7
    elif 2.0<=vol_ratio<3.0: trend+=4
    if 5<chg20d<=20: trend+=8
    elif 20<chg20d<=35: trend+=5
    sec=sector_info.get(code,'')
    if sec in TIER1: trend+=10
    elif sec in TIER2: trend+=6

    # BOTTOM (0-22)
    bottom=0
    if abs(pct_ma60)<20: bottom+=5
    if bb_width<18: bottom+=4
    if range_20d<22: bottom+=3
    if vol_ratio<1.15: bottom+=3
    if dd10d>-5: bottom+=2
    if abs(chg20d)<12: bottom+=2
    if ma5>ma20 or current>ma20: bottom+=1

    return trend,bottom,{'pct_ma60':pct_ma60,'vol_ratio':vol_ratio,'chg20d':chg20d}

# ===== DATA =====
print('Fetching...')
candidates=set(); sector_info={}
for bk_code,bk_name in TECH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        for it in items[:15]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code); sector_info[code]=bk_name
    except: pass
    time.sleep(0.3)
for c in ['002475','002129','600487','601869','000100','002463','600703','600584','002281','002415']:
    if is_mainboard(c): candidates.add(c)
candidates=list(candidates)

raw_cache={}
for code in candidates:
    tc=f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r=requests.get(url,params={'param':f'{tc},day,,,520,qfq'},
                      headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=r.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if raw and len(raw)>=350: raw_cache[code]=raw
    except: pass
    time.sleep(0.12)
print(f'K-line: {len(raw_cache)} stocks')

# ===== BACKTEST =====
def run(label,screen_end,fwd_len):
    results=[]
    for code,raw in raw_cache.items():
        closes_all=[float(k[2]) for k in raw]
        if screen_end>=len(closes_all)-fwd_len: continue
        trend,bottom,meta=compute(raw,screen_end,code)
        entry=closes_all[screen_end]
        fwd_end=min(screen_end+fwd_len,len(closes_all))
        final=closes_all[fwd_end-1]
        fwd_ret=(final/entry-1)*100
        fwd_c=closes_all[screen_end:fwd_end]
        fwd_h=np.maximum.accumulate(np.array(fwd_c))
        max_dd=((np.array(fwd_c)-fwd_h)/fwd_h*100).min()
        sec=sector_info.get(code,'')
        tier='T1' if sec in TIER1 else ('T2' if sec in TIER2 else 'T3')

        # Selection rules for each version
        v3_sel = trend>=50
        v5_sel = (trend>=45 and bottom>=10) or (trend>=55) or (bottom>=13)
        # V6: T1+T2 only, tighter thresholds
        in_t12 = sec in TIER1 or sec in TIER2
        v6_trend  = in_t12 and trend>=50
        v6_core   = in_t12 and trend>=40 and bottom>=12
        v6_bottom = in_t12 and bottom>=15
        v6_sel = v6_trend or v6_core or v6_bottom

        results.append({'code':code,'sector':sec,'tier':tier,
            'fwd_ret':fwd_ret,'max_dd':max_dd,'trend':trend,'bottom':bottom,
            'v3':v3_sel,'v5':v5_sel,'v6':v6_sel,
            'v6_trend':v6_trend,'v6_core':v6_core,'v6_bottom':v6_bottom})
    return results

print('\n--- Period 1: 2024→2025 ---')
r24=run('2024-2025',50,250)
print(f'Results: {len(r24)}')
print('--- Period 2: 2025→2026 ---')
r25=run('2025-2026',310,200)
print(f'Results: {len(r25)}')

# ===== TIERED PORTFOLIO SIMULATION =====
def portfolio_return(results, selected, weights=None):
    """Equal-weight portfolio return of selected stocks"""
    sel=[r for r in results if selected(r)]
    if not sel: return 0,0,0,0
    if weights:
        rets=[r['fwd_ret']*weights(r) for r in sel]
        total_w=sum(weights(r) for r in sel)
        avg=sum(rets)/total_w if total_w>0 else 0
    else:
        avg=np.mean([r['fwd_ret'] for r in sel])
    wr=len([r for r in sel if r['fwd_ret']>0])/len(sel)*100
    dd=np.mean([r['max_dd'] for r in sel])
    big=len([r for r in sel if r['fwd_ret']>200])
    return avg,wr,dd,big,len(sel)

def w_tiered(r):
    """Tiered weights: TREND=60%, CORE=30%, BOTTOM=10%"""
    w=0
    if r['v6_trend']: w+=0.6
    if r['v6_core']: w+=0.3
    if r['v6_bottom']: w+=0.1
    return min(w,1.0)  # cap at 100%

# ===== ANALYSIS =====
def analyze(label,results):
    print(f'\n{"="*65}')
    print(f'{label}')
    print(f'{"="*65}')

    versions=[
        ('V3 (Trend>=50)',        lambda r: r['v3']),
        ('V5 (Trend+Bottom>=13)', lambda r: r['v5']),
        ('V6-TREND (T12, T>=50)', lambda r: r['v6_trend']),
        ('V6-CORE (T12, T>=40,B>=12)', lambda r: r['v6_core']),
        ('V6-BOTTOM (T12, B>=15)', lambda r: r['v6_bottom']),
        ('V6-ALL (equal-wt)',     lambda r: r['v6']),
        ('V6-ALL (tiered-wt)',    lambda r: r['v6'], w_tiered),
    ]

    print(f'  {"":<28} {"Count":>6} {"Avg Ret":>10} {"WinRate":>8} {"Avg DD":>8} {"+200%":>6}')
    print(f'  {"-"*65}')
    rows=[]
    # V3
    sel=[r for r in results if r['v3']]
    if sel:
        rows.append(('V3 (Trend>=50)',len(sel),np.mean([r['fwd_ret'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>0])/len(sel)*100,
                    np.mean([r['max_dd'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>200])))

    # V5
    sel=[r for r in results if r['v5']]
    if sel:
        rows.append(('V5 (T+B>=13)',len(sel),np.mean([r['fwd_ret'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>0])/len(sel)*100,
                    np.mean([r['max_dd'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>200])))

    # V6 sub-tiers
    for name,cond in [('V6-TREND(T12,T>=50)',lambda r:r['v6_trend']),
                      ('V6-CORE(T12,T>=40,B>=12)',lambda r:r['v6_core']),
                      ('V6-BOTTOM(T12,B>=15)',lambda r:r['v6_bottom'])]:
        sel=[r for r in results if cond(r)]
        if sel:
            rows.append((name,len(sel),np.mean([r['fwd_ret'] for r in sel]),
                        len([r for r in sel if r['fwd_ret']>0])/len(sel)*100,
                        np.mean([r['max_dd'] for r in sel]),
                        len([r for r in sel if r['fwd_ret']>200])))

    # V6 equal-weight
    sel=[r for r in results if r['v6']]
    if sel:
        rows.append(('V6-ALL (equal-wt)',len(sel),np.mean([r['fwd_ret'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>0])/len(sel)*100,
                    np.mean([r['max_dd'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>200])))

    # V6 tiered-weight
    sel=[r for r in results if r['v6']]
    if sel:
        weighted_rets=[]
        for r in sel:
            w=0
            if r['v6_trend']: w+=0.6
            if r['v6_core']: w+=0.3
            if r['v6_bottom']: w+=0.1
            weighted_rets.append(r['fwd_ret']*min(w,1.0))
        total_w=sum(min((0.6 if r['v6_trend'] else 0)+(0.3 if r['v6_core'] else 0)+(0.1 if r['v6_bottom'] else 0),1.0) for r in sel)
        tiered_avg=sum(weighted_rets)/total_w if total_w>0 else 0
        rows.append(('V6-ALL (tiered-wt)',len(sel),tiered_avg,
                    len([r for r in sel if r['fwd_ret']>0])/len(sel)*100,
                    np.mean([r['max_dd'] for r in sel]),
                    len([r for r in sel if r['fwd_ret']>200])))

    # All tech
    rows.append(('ALL Tech Stocks',len(results),np.mean([r['fwd_ret'] for r in results]),
                len([r for r in results if r['fwd_ret']>0])/len(results)*100,
                np.mean([r['max_dd'] for r in results]),
                len([r for r in results if r['fwd_ret']>200])))

    for name,cnt,avg,wr,dd,big in rows:
        print(f'  {name:<28} {cnt:>6} {avg:>+9.1f}% {wr:>7.0f}% {dd:>7.1f}% {big:>6}')

    # T1+T2 only breakdown
    t12=[r for r in results if r['tier'] in ('T1','T2')]
    print(f'\n  T1+T2 only: {len(t12)} stocks, avg={np.mean([r["fwd_ret"] for r in t12]):+.1f}%, '
          f'win={len([r for r in t12 if r["fwd_ret"]>0])/len(t12)*100:.0f}%')

    return rows

s1=analyze('PERIOD 1: 2024->2025',r24)
s2=analyze('PERIOD 2: 2025->2026',r25)

# ===== HEAD-TO-HEAD =====
print(f'\n{"="*70}')
print('V3 vs V5 vs V6 - Head to Head')
print(f'{"="*70}')
for pname,results in [('2024->2025',r24),('2025->2026',r25)]:
    v3=[r for r in results if r['v3']]
    v5=[r for r in results if r['v5']]
    v6=[r for r in results if r['v6']]
    v6_t12=[r for r in v6 if r['tier'] in ('T1','T2')]
    print(f'\n{pname}:')
    print(f'  V3:  {len(v3):>3} stocks, avg={np.mean([r["fwd_ret"] for r in v3]):>+7.1f}%, '
          f'win={len([r for r in v3 if r["fwd_ret"]>0])/len(v3)*100:.0f}%')
    print(f'  V5:  {len(v5):>3} stocks, avg={np.mean([r["fwd_ret"] for r in v5]):>+7.1f}%, '
          f'win={len([r for r in v5 if r["fwd_ret"]>0])/len(v5)*100:.0f}%')
    print(f'  V6:  {len(v6):>3} stocks, avg={np.mean([r["fwd_ret"] for r in v6]):>+7.1f}%, '
          f'win={len([r for r in v6 if r["fwd_ret"]>0])/len(v6)*100:.0f}%')

# ===== SAVE =====
all_r=[]
for r in r24: r['period']='2024-2025'; all_r.append(r)
for r in r25: r['period']='2025-2026'; all_r.append(r)
csv_path=os.path.join(DATA_DIR,'backtest_v6.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','tier','period','fwd_ret','max_dd','trend','bottom',
                'v3','v5','v6','v6_trend','v6_core','v6_bottom'])
    for r in all_r:
        w.writerow([r['code'],r['sector'],r['tier'],r['period'],f'{r["fwd_ret"]:.1f}',f'{r["max_dd"]:.1f}',
                   r['trend'],r['bottom'],r['v3'],r['v5'],r['v6'],r['v6_trend'],r['v6_core'],r['v6_bottom']])
print(f'\nSaved: {csv_path}')
print('DONE.')
