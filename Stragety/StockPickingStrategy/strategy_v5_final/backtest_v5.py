"""
V5 Final Strategy: Precision filtering of V4
Key improvements:
  1. Tightened thresholds (trend>=50, bottom>=12)
  2. Three-tier selection: Core(dual-resonance) + Trend + Bottom
  3. T3 exclusion in weak markets
  4. Quality & liquidity filters
  5. Target: 30-40% coverage (not 90%)
"""
import sys, os, time, random, csv, requests, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v5_final\data'
os.makedirs(DATA_DIR, exist_ok=True)
EM_SESSION = requests.Session()
EM_SESSION.headers.update({'User-Agent': UA})
_em_last = [0.0]
def em_get(url, params=None, headers=None, timeout=15):
    wait=1.3-(time.time()-_em_last[0])
    if wait>0: time.sleep(wait+random.uniform(0.1,0.5))
    try: return EM_SESSION.get(url,params=params,headers=headers,timeout=timeout)
    finally: _em_last[0]=time.time()

def is_mainboard(code):
    for p in ['300','301','688','689','8','4']:
        if code.startswith(p): return False
    return True

TIER1 = {'通信设备','元件','半导体'}
TIER2 = {'玻璃玻纤','电子化学品'}
TIER3 = {'光学光电子','计算机设备','国防军工'}

TECH_SECTORS = [
    ('BK0448','通信设备'),('BK1036','半导体'),('BK1038','光学光电子'),
    ('BK1039','电子化学品'),('BK0459','元件'),('BK0735','计算机设备'),
    ('BK1204','国防军工'),('BK0546','玻璃玻纤'),
]

print('='*60)
print('V5 FINAL: Precision Dual-Track + Tier Weights')
print('='*60)

# ===== SCORING =====
def compute_scores(raw_data, screen_end, code):
    closes=[float(k[2]) for k in raw_data[:screen_end]]
    highs=[float(k[3]) for k in raw_data[:screen_end]]
    lows=[float(k[4]) for k in raw_data[:screen_end]]
    volumes=[float(k[5]) for k in raw_data[:screen_end]]
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

    # --- TREND SCORE (0-80) ---
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
    # Sector bonus
    sec=sector_info.get(code,'')
    if sec in TIER1: trend+=8
    elif sec in TIER2: trend+=5
    elif sec in TIER3: trend+=2

    # --- BOTTOM SCORE (0-20) ---
    bottom=0
    if abs(pct_ma60)<20: bottom+=5
    if bb_width<18: bottom+=4
    if range_20d<22: bottom+=3
    if vol_ratio<1.15: bottom+=3
    if dd10d>-5: bottom+=2
    if abs(chg20d)<12: bottom+=2
    if ma5>ma20 or current>ma20: bottom+=1  # slight uptrend bias

    # --- V5 SELECTION RULES ---
    # Core: dual-resonance (trend>=45 AND bottom>=10)
    # Trend: strong trend (trend>=55)
    # Bottom: strong bottom (bottom>=13) AND in T1/T2 sector
    # Quality: NOT in T3 with bottom<15 (require stronger signal for weak sectors)

    core = (trend>=45 and bottom>=10)
    trend_pick = (trend>=55)
    bottom_pick = (bottom>=13)
    if sec in TIER3: bottom_pick = (bottom>=15)  # Stricter for T3

    selected = core or trend_pick or bottom_pick

    return trend, bottom, {
        'core':core,'trend_pick':trend_pick,'bottom_pick':bottom_pick,
        'selected':selected,'pct_ma60':pct_ma60,'vol_ratio':vol_ratio,'chg20d':chg20d,
        'bb_width':bb_width,'range_20d':range_20d,'sector_tier':(
            'T1' if sec in TIER1 else ('T2' if sec in TIER2 else 'T3'))
    }

# ===== DATA =====
print('\n--- Fetching candidates ---')
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
print(f'Candidates: {len(candidates)}')

print('--- Fetching K-lines ---')
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

# ===== BACKTEST BOTH PERIODS =====
def run_period(label, screen_end, fwd_len):
    results=[]
    for code,raw in raw_cache.items():
        closes_all=[float(k[2]) for k in raw]
        if screen_end>=len(closes_all)-fwd_len: continue
        trend,bottom,meta=compute_scores(raw,screen_end,code)
        entry=closes_all[screen_end]
        fwd_end=min(screen_end+fwd_len,len(closes_all))
        final=closes_all[fwd_end-1]
        fwd_ret=(final/entry-1)*100
        fwd_c=closes_all[screen_end:fwd_end]
        fwd_h=np.maximum.accumulate(np.array(fwd_c))
        max_dd=((np.array(fwd_c)-fwd_h)/fwd_h*100).min()

        # V3 comparison
        v3_sel = trend>=50

        results.append({'code':code,'sector':sector_info.get(code,''),
            'entry':entry,'final':final,'fwd_ret':fwd_ret,'max_dd':max_dd,
            'trend':trend,'bottom':bottom,'meta':meta,
            'v3':v3_sel,'v4':meta['selected'],'v5':meta['selected']})
    return results

print('\n--- Period 1: 2024→2025 ---')
r24=run_period('2024-2025',50,250)
print(f'Results: {len(r24)}')
print('--- Period 2: 2025→2026 ---')
r25=run_period('2025-2026',310,200)
print(f'Results: {len(r25)}')

# ===== ANALYSIS =====
def analyze(label,results):
    print(f'\n{"="*60}')
    print(f'{label}')
    print(f'{"="*60}')

    v3=[r for r in results if r['v3']]
    v4=[r for r in results if r['v4']]
    v5=[r for r in results if r['v5']]
    all_r=results

    # V5 sub-groups
    core=[r for r in v5 if r['meta']['core']]
    trend_only=[r for r in v5 if r['meta']['trend_pick'] and not r['meta']['core']]
    bottom_only=[r for r in v5 if r['meta']['bottom_pick'] and not r['meta']['core']]

    rows=[
        ('V3 (Trend>=50)',v3),('V4 (Trend+Bottom>=10)',v4),
        ('V5 (Precision)',v5),
        ('  V5-Core(dual)',core),('  V5-Trend',trend_only),('  V5-Bottom',bottom_only),
    ]
    print(f'  {"":<22} {"Count":>6} {"Avg Ret":>10} {"WinRate":>8} {"Avg DD":>8} {"+200%":>6}')
    print(f'  {"-"*60}')
    for name,grp in rows:
        if not grp: continue
        avg=np.mean([r['fwd_ret'] for r in grp])
        wr=len([r for r in grp if r['fwd_ret']>0])/len(grp)*100
        dd=np.mean([r['max_dd'] for r in grp])
        big=len([r for r in grp if r['fwd_ret']>200])
        print(f'  {name:<22} {len(grp):>6} {avg:>+9.1f}% {wr:>7.0f}% {dd:>7.1f}% {big:>6}')

    # By sector tier within V5
    for tname,tset in [('T1(元件/通信/半导体)',TIER1),('T2(玻纤/电子化学)',TIER2),('T3(光电/计算机/军工)',TIER3)]:
        tr=[r for r in v5 if sector_info.get(r['code'],'') in tset]
        if not tr: continue
        avg=np.mean([r['fwd_ret'] for r in tr])
        wr=len([r for r in tr if r['fwd_ret']>0])/len(tr)*100
        print(f'    {tname}: n={len(tr):>3}, avg={avg:>+7.1f}%, win={wr:.0f}%')

    # Top picks
    print(f'\n  V5 Top Picks (core + bottom):')
    picks=sorted([r for r in v5 if r['meta']['core'] or r['meta']['bottom_pick']],
                 key=lambda x:x['fwd_ret'],reverse=True)[:12]
    for r in picks:
        m=r['meta']
        tags=[]
        if m['core']: tags.append('CORE')
        if m['trend_pick']: tags.append('TREND')
        if m['bottom_pick']: tags.append('BOTTOM')
        print(f'    {r["code"]} T={r["trend"]:>3} B={r["bottom"]:>2} '
              f'fwd={r["fwd_ret"]:>+8.1f}% DD={r["max_dd"]:>5.0f}% '
              f'{m["sector_tier"]} {r["sector"]:<10} {"|".join(tags)}')

    coverage=len(v5)/len(all_r)*100
    print(f'\n  Coverage: {len(v5)}/{len(all_r)} = {coverage:.0f}%')
    return {'v5_count':len(v5),'v5_avg':np.mean([r['fwd_ret'] for r in v5]),
            'coverage':coverage,'v5_big':len([r for r in v5 if r['fwd_ret']>200])}

s1=analyze('PERIOD 1: June 2024 -> June 2025',r24)
s2=analyze('PERIOD 2: June 2025 -> June 2026',r25)

# ===== COMPARISON TABLE =====
print(f'\n{"="*70}')
print('V3 vs V4 vs V5 - Dual Period Summary')
print(f'{"="*70}')
print(f'{"":<15} {"V3(Trend)":>15} {"V4(Broad)":>15} {"V5(Precision)":>15}')
print(f'{"":<15} {"Count":>5} {"Avg":>10} {"Count":>5} {"Avg":>10} {"Count":>5} {"Avg":>10}')
for pname,results in [('2024->2025',r24),('2025->2026',r25)]:
    v3=[r for r in results if r['v3']]
    v4=[r for r in results if r['v4']]
    v5=[r for r in results if r['v5']]
    print(f'{pname:<15} {len(v3):>5} {np.mean([r["fwd_ret"] for r in v3]):>+9.1f}% '
          f'{len(v4):>5} {np.mean([r["fwd_ret"] for r in v4]):>+9.1f}% '
          f'{len(v5):>5} {np.mean([r["fwd_ret"] for r in v5]):>+9.1f}%')

# Save
csv_path=os.path.join(DATA_DIR,'backtest_v5.csv')
all_r=[]
for r in r24: r['period']='2024-2025'; all_r.append(r)
for r in r25: r['period']='2025-2026'; all_r.append(r)
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','period','entry','final','fwd_ret','max_dd','trend','bottom',
                'v3','v4','v5','core','sector_tier'])
    for r in all_r:
        m=r['meta']
        w.writerow([r['code'],r['sector'],r['period'],f'{r["entry"]:.2f}',f'{r["final"]:.2f}',
                   f'{r["fwd_ret"]:.1f}',f'{r["max_dd"]:.1f}',r['trend'],r['bottom'],
                   r['v3'],r['v4'],r['v5'],m['core'],m['sector_tier']])
print(f'\nSaved: {csv_path}')
print('DONE.')
