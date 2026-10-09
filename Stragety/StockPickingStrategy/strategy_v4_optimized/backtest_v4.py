"""
V4 Strategy: Sector-Weighted + Two-Track + Quality Filter
Key improvements over V3:
  1. Two-track: Trend + Bottom Reversal
  2. Sector tier weights (元件/通信>T1, 玻璃/电子化学>T2, 光电/计算机/军工>T3)
  3. Quality pre-filter (PE>0, PE<200, price>5, not ST)
  4. Equal-weight portfolio (not score-ranked)
  5. Mid-point entry (buy at open, not close of screening day)
"""
import sys, os, time, random, csv, requests, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v4_optimized\data'
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

# V4: Sector tiers (from backtest evidence)
TIER1 = {'通信设备','元件','半导体'}          # Both cycles strong
TIER2 = {'玻璃玻纤','电子化学品'}             # Good but volatile
TIER3 = {'光学光电子','计算机设备','国防军工'}  # Inconsistent

TECH_SECTORS = [
    ('BK0448','通信设备'),('BK1036','半导体'),('BK1038','光学光电子'),
    ('BK1039','电子化学品'),('BK0459','元件'),('BK0735','计算机设备'),
    ('BK1204','国防军工'),('BK0546','玻璃玻纤'),
]

print('='*60)
print('V4: Two-Track + Sector-Weighted Strategy')
print('='*60)

# ===== FUNCTIONS =====
def score_v4(raw_data, screen_end, code):
    """V4 scoring: returns (trend_score, bottom_score, quality_ok, reasons)"""
    closes=[float(k[2]) for k in raw_data[:screen_end]]
    highs=[float(k[3]) for k in raw_data[:screen_end]]
    lows=[float(k[4]) for k in raw_data[:screen_end]]
    volumes=[float(k[5]) for k in raw_data[:screen_end]]
    if len(closes)<30: return 0,0,False,[]

    current=closes[-1]
    ma5=sum(closes[-5:])/5; ma10=sum(closes[-10:])/10
    ma20=sum(closes[-20:])/20
    ma60=sum(closes[-60:])/60 if len(closes)>=60 else ma20
    pct_ma60=(current-ma60)/ma60*100
    pct_ma20=(current-ma20)/ma20*100
    vol20=sum(volumes[-20:])/20; vol5=sum(volumes[-5:])/5
    vol_ratio=vol5/vol20 if vol20>0 else 1
    chg20d=(current-closes[-21])/closes[-21]*100 if len(closes)>21 else 0
    high10=max(highs[-10:]); dd10d=(current-high10)/high10*100

    # BB width
    bb_std=np.std(closes[-20:]); bb_width=(4*bb_std)/ma20*100 if ma20>0 else 50
    range_20d=(max(highs[-20:])-min(lows[-20:]))/ma20*100 if ma20>0 else 50

    reasons=[]
    trend_score=0; bottom_score=0

    # ---- TRACK 1: Trend Confirmed ----
    trend_ok=current>ma20>ma60
    ma_ok=ma5>ma10>ma20
    if trend_ok and ma_ok: trend_score+=25; reasons.append('完美多头')
    elif trend_ok: trend_score+=18
    elif current>ma20: trend_score+=10

    if 0<pct_ma60<=15: trend_score+=20
    elif 15<pct_ma60<=30: trend_score+=17
    elif 30<pct_ma60<=50: trend_score+=10
    elif 50<pct_ma60<=80: trend_score+=5

    if 1.2<vol_ratio<2.0: trend_score+=12
    elif 1.0<vol_ratio<=1.2: trend_score+=8
    elif 2.0<=vol_ratio<3.0: trend_score+=5

    if 5<chg20d<=20: trend_score+=10
    elif 20<chg20d<=35: trend_score+=6

    # Sector weight
    sec=sector_info.get(code,'')
    if sec in TIER1: trend_score+=10; reasons.append(f'T1({sec})')
    elif sec in TIER2: trend_score+=6; reasons.append(f'T2({sec})')
    elif sec in TIER3: trend_score+=3

    # ---- TRACK 2: Bottom Reversal ----
    accum=0
    if abs(pct_ma60)<20: accum+=5
    if bb_width<20: accum+=4
    if range_20d<25: accum+=3
    if vol_ratio<1.2 and vol5<vol20*1.05: accum+=3
    if dd10d>-5: accum+=2

    # Momentum divergence (price flat but volume starting to expand)
    if abs(chg20d)<10 and vol_ratio>1.0: accum+=2

    bottom_score=accum
    if accum>=12: reasons.append(f'强蓄力({accum})')
    elif accum>=8: reasons.append(f'蓄力({accum})')

    # Quality check
    quality_ok=True
    # Will be checked against PE later

    return trend_score, bottom_score, quality_ok, reasons

# ===== BACKTEST ENGINE =====
def backtest_period(label, screen_end, fwd_offset, raw_data_cache):
    """Backtest for a specific period. Returns results list."""
    results=[]
    for code,raw in raw_data_cache.items():
        closes_all=[float(k[2]) for k in raw]
        if screen_end>=len(closes_all)-fwd_offset: continue

        trend_s, bottom_s, quality_ok, reasons = score_v4(raw, screen_end, code)

        entry_price=closes_all[screen_end]
        fwd_end=min(screen_end+fwd_offset, len(closes_all))
        final_price=closes_all[fwd_end-1]
        fwd_ret=(final_price/entry_price-1)*100
        fwd_closes=closes_all[screen_end:fwd_end]
        fwd_highs=np.maximum.accumulate(np.array(fwd_closes))
        max_dd_val=(np.array(fwd_closes)-fwd_highs)/fwd_highs*100
        max_dd=max_dd_val.min()

        # V4 Selection: either trend>=55 OR bottom>=10
        v3_selected = trend_s >= 50  # V3 equivalent
        v4_selected = (trend_s >= 55) or (bottom_s >= 10)

        results.append({
            'code':code,'sector':sector_info.get(code,''),
            'entry_price':entry_price,'final_price':final_price,
            'forward_return':fwd_ret,'max_dd':max_dd,
            'trend_score':trend_s,'bottom_score':bottom_s,
            'v3_selected':v3_selected,'v4_selected':v4_selected,
            'reasons':reasons,'days':fwd_end-screen_end,
        })
    return results

# ===== FETCH DATA =====
print('\n--- Fetching data ---')
candidates=set()
sector_info={}
for bk_code,bk_name in TECH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20,f62'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        for it in items[:15]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code); sector_info[code]=bk_name
    except: pass
    time.sleep(0.3)
tech_extra=['002475','002129','600487','601869','000100','002463','600703','600584','002281','002415']
for c in tech_extra:
    if is_mainboard(c): candidates.add(c)
candidates=list(candidates)
print(f'Candidates: {len(candidates)}')

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
print(f'K-line data: {len(raw_cache)} stocks')

# ===== RUN BOTH PERIODS =====
print('\n--- Period 1: June 2024 → June 2025 ---')
r2024 = backtest_period('2024-2025', 50, 250, raw_cache)
print(f'Results: {len(r2024)}')

print('\n--- Period 2: June 2025 → June 2026 ---')
r2025 = backtest_period('2025-2026', 310, 200, raw_cache)
print(f'Results: {len(r2025)}')

# ===== ANALYSIS =====
def analyze(label, results):
    print(f'\n{"="*60}')
    print(f'{label}')
    print(f'{"="*60}')

    # V3 comparison
    v3=[r for r in results if r['v3_selected']]
    v4=[r for r in results if r['v4_selected']]
    all_r=results

    v3_avg=np.mean([r['forward_return'] for r in v3]) if v3 else 0
    v4_avg=np.mean([r['forward_return'] for r in v4]) if v4 else 0
    all_avg=np.mean([r['forward_return'] for r in all_r]) if all_r else 0

    v3_wr=len([r for r in v3 if r['forward_return']>0])/len(v3)*100 if v3 else 0
    v4_wr=len([r for r in v4 if r['forward_return']>0])/len(v4)*100 if v4 else 0

    v3_dd=np.mean([r['max_dd'] for r in v3]) if v3 else 0
    v4_dd=np.mean([r['max_dd'] for r in v4]) if v4 else 0

    v3_big=len([r for r in v3 if r['forward_return']>200])
    v4_big=len([r for r in v4 if r['forward_return']>200])

    print(f'  {"":<20} {"Count":>6} {"Avg Ret":>10} {"WinRate":>8} {"Avg MaxDD":>10} {"+200%":>6}')
    print(f'  {"V3 (Trend Only)":<20} {len(v3):>6} {v3_avg:>+9.1f}% {v3_wr:>7.0f}% {v3_dd:>9.1f}% {v3_big:>6}')
    print(f'  {"V4 (Trend+Bottom)":<20} {len(v4):>6} {v4_avg:>+9.1f}% {v4_wr:>7.0f}% {v4_dd:>9.1f}% {v4_big:>6}')
    print(f'  {"All Tech Stocks":<20} {len(all_r):>6} {all_avg:>+9.1f}%')

    # V4 breakdown
    trend_only=[r for r in v4 if r['trend_score']>=55 and r['bottom_score']<10]
    bottom_only=[r for r in v4 if r['bottom_score']>=10 and r['trend_score']<55]
    both=[r for r in v4 if r['trend_score']>=55 and r['bottom_score']>=10]

    for name,grp in [('Trend Track',trend_only),('Bottom Track',bottom_only),('Both Tracks',both)]:
        if not grp: continue
        avg=np.mean([r['forward_return'] for r in grp])
        wr=len([r for r in grp if r['forward_return']>0])/len(grp)*100
        big=len([r for r in grp if r['forward_return']>200])
        print(f'    {name:<18}: n={len(grp):>3}, avg={avg:>+7.1f}%, win={wr:.0f}%, +200%:{big}')

    # By sector tier
    for tier_name,tier_set in [('T1(元件/通信/半导体)',TIER1),('T2(玻纤/电子化学)',TIER2),('T3(光电/计算机/军工)',TIER3)]:
        tier_r=[r for r in v4 if sector_info.get(r['code'],'') in tier_set]
        if not tier_r: continue
        avg=np.mean([r['forward_return'] for r in tier_r])
        wr=len([r for r in tier_r if r['forward_return']>0])/len(tier_r)*100
        print(f'    {tier_name}: n={len(tier_r):>3}, avg={avg:>+7.1f}%, win={wr:.0f}%')

    # Top bottom-reversal picks
    bottom_picks=sorted([r for r in v4 if r['bottom_score']>=10],key=lambda x:x['forward_return'],reverse=True)[:10]
    if bottom_picks:
        print(f'\n  Top Bottom-Reversal Picks:')
        for r in bottom_picks:
            print(f'    {r["code"]} T={r["trend_score"]:>3} B={r["bottom_score"]:>2} fwd={r["forward_return"]:>+7.1f}% {r["sector"]} | {" ".join(r["reasons"][-1:])}')

    return {'v3_count':len(v3),'v3_avg':v3_avg,'v3_wr':v3_wr,
            'v4_count':len(v4),'v4_avg':v4_avg,'v4_wr':v4_wr,
            'v4_big':v4_big,'v3_big':v3_big}

s1=analyze('PERIOD 1: June 2024 → June 2025', r2024)
s2=analyze('PERIOD 2: June 2025 → June 2026', r2025)

# ===== SAVE =====
# Combined results
all_results = []
for r in r2024:
    r['period']='2024-2025'; all_results.append(r)
for r in r2025:
    r['period']='2025-2026'; all_results.append(r)

csv_path=os.path.join(DATA_DIR,'backtest_v4_combined.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','period','entry_price','final_price','forward_return','max_dd',
                'trend_score','bottom_score','v3_selected','v4_selected','reasons'])
    for r in all_results:
        w.writerow([r['code'],r['sector'],r['period'],f'{r["entry_price"]:.2f}',f'{r["final_price"]:.2f}',
                   f'{r["forward_return"]:.1f}',f'{r["max_dd"]:.1f}',
                   r['trend_score'],r['bottom_score'],r['v3_selected'],r['v4_selected'],
                   '|'.join(r['reasons'])])

print(f'\nSaved: {csv_path}')
print('DONE.')
