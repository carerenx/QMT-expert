"""
V3 Tech-Only Backtest: June 2024 → June 2025
"""
import sys, os, time, random, csv, requests, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v3_tech_only\data_backtest_2024'
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

TECH_SECTORS = [
    ('BK0448','通信设备'),('BK1036','半导体'),('BK1038','光学光电子'),
    ('BK1039','电子化学品'),('BK0459','元件'),('BK0735','计算机设备'),
    ('BK1204','国防军工'),('BK0546','玻璃玻纤'),
]

print('='*60)
print('V3 BACKTEST: June 2024 → June 2025 (1 year)')
print('='*60)

# ===== STEP 1: Candidates =====
print('\n--- Candidates ---')
candidates = set()
sector_info = {}
for bk_code, bk_name in TECH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        cnt=0
        for it in items[:15]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code); sector_info[code]=bk_name; cnt+=1
        print(f'  {bk_name}: {cnt}')
    except: pass
    time.sleep(0.3)

tech_extra=['002475','002129','600487','601869','000100','002463','600703','600584','002281','002415']
for c in tech_extra:
    if is_mainboard(c): candidates.add(c)
candidates=list(candidates)
print(f'Total: {len(candidates)}')

# ===== STEP 2: Fetch 520 bars + Screen at June 2024 =====
print('\n--- Backtesting ---')

def score_v3(raw_data, screen_end):
    closes=[float(k[2]) for k in raw_data[:screen_end]]
    highs=[float(k[3]) for k in raw_data[:screen_end]]
    lows=[float(k[4]) for k in raw_data[:screen_end]]
    volumes=[float(k[5]) for k in raw_data[:screen_end]]
    if len(closes)<30: return 0,[],{}
    current=closes[-1]
    ma5=sum(closes[-5:])/5; ma10=sum(closes[-10:])/10
    ma20=sum(closes[-20:])/20
    ma60=sum(closes[-60:])/60 if len(closes)>=60 else ma20
    pct_ma60=(current-ma60)/ma60*100
    vol20=sum(volumes[-20:])/20; vol5=sum(volumes[-5:])/5
    vol_ratio=vol5/vol20 if vol20>0 else 1
    chg20d=(current-closes[-21])/closes[-21]*100 if len(closes)>21 else 0
    high10=max(highs[-10:])

    score=0; reasons=[]
    if current>ma20>ma60: score+=20; reasons.append('多头排列')
    elif current>ma20: score+=12
    if ma5>ma10>ma20: score+=12; reasons.append('均线发散')
    elif ma5>ma20: score+=6
    if 0<pct_ma60<=15: score+=25; reasons.append(f'极早期({pct_ma60:.0f}%)')
    elif 15<pct_ma60<=25: score+=22
    elif 25<pct_ma60<=35: score+=17
    elif 35<pct_ma60<=50: score+=10
    elif pct_ma60>100: score-=8
    if 1.2<vol_ratio<=1.8: score+=15; reasons.append(f'温和放量({vol_ratio:.1f}x)')
    elif 1.8<vol_ratio<=2.5: score+=11
    elif 1.0<vol_ratio<=1.2: score+=8
    if 5<chg20d<=15: score+=15; reasons.append(f'稳健(+{chg20d:.0f}%)')
    elif 15<chg20d<=25: score+=12
    elif 25<chg20d<=45: score+=7

    # Bottom accumulation
    bb_std=np.std(closes[-20:]); bb_width=(4*bb_std)/ma20*100
    range_20d=(max(highs[-20:])-min(lows[-20:]))/ma20*100
    accum=0
    if abs(pct_ma60)<20: accum+=5
    if bb_width<20: accum+=4
    if range_20d<25: accum+=3
    if vol_ratio<1.2: accum+=3
    if accum>=10: score+=15; reasons.append('底部蓄力')
    elif accum>=7: score+=10
    elif accum>=4: score+=5

    sec=sector_info.get(code,'')
    if sec in {'通信设备','半导体','元件','玻璃玻纤','电子化学品'}: score+=10; reasons.append(f'强势行业({sec})')
    else: score+=3

    return score,reasons,{'pct_ma60':pct_ma60,'vol_ratio':vol_ratio,'chg20d':chg20d}

results=[]
for code in candidates:
    tc=f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r=requests.get(url,params={'param':f'{tc},day,,,520,qfq'},
                      headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=r.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if not raw or len(raw)<350: continue

        # Screen at bar ~50 (late June 2024)
        screen_end=50
        closes_all=[float(k[2]) for k in raw]
        if screen_end>=len(closes_all)-250: continue  # Need 1 year forward

        score,reasons,metrics=score_v3(raw,screen_end)
        entry_price=closes_all[screen_end]
        # Forward to bar ~300 (June 2025)
        fwd_end=min(screen_end+250,len(closes_all))
        final_price=closes_all[fwd_end-1]
        fwd_ret=(final_price/entry_price-1)*100
        fwd_closes=closes_all[screen_end:fwd_end]
        fwd_highs=np.maximum.accumulate(np.array(fwd_closes))
        max_dd=(np.array(fwd_closes)-fwd_highs)/fwd_highs*100
        max_dd_val=max_dd.min()
        days=fwd_end-screen_end

        results.append({'code':code,'sector':sector_info.get(code,''),
            'screen_date':raw[screen_end-1][0],'entry_price':entry_price,
            'final_price':final_price,'forward_return':fwd_ret,'max_dd_fwd':max_dd_val,
            'score':score,'reasons':reasons,'days_held':days})
        if score>=50: print(f'  {code} S={score:>3} fwd={fwd_ret:>+7.1f}% {" | ".join(reasons[:2])}')
        time.sleep(0.15)
    except: pass

results.sort(key=lambda x:x['score'],reverse=True)
print(f'\nValid: {len(results)}')

# ===== Analysis =====
print('\n'+'='*60)
print('RESULTS')
print('='*60)

for name,lo,hi in [('>=80',80,200),('65-79',65,80),('50-64',50,65),('<50',0,50)]:
    tier=[r for r in results if lo<=r['score']<hi]
    if not tier: continue
    avg_r=np.mean([r['forward_return'] for r in tier])
    avg_dd=np.mean([r['max_dd_fwd'] for r in tier])
    wr=len([r for r in tier if r['forward_return']>0])/len(tier)*100
    top3=sum(1 for r in tier if r['forward_return']>200)
    print(f'  Score {name}: n={len(tier)}, avg={avg_r:.1f}%, maxDD={avg_dd:.1f}%, win={wr:.0f}%, +200%: {top3}')

all_avg=np.mean([r['forward_return'] for r in results])
all_wr=len([r for r in results if r['forward_return']>0])/len(results)*100
print(f'\n  ALL: n={len(results)}, avg={all_avg:.1f}%, win={all_wr:.0f}%')

# By sector
print(f'\n--- By Sector ---')
sp={}
for r in results:
    s=r['sector']
    if s not in sp: sp[s]=[]
    sp[s].append(r['forward_return'])
for s in sorted(sp,key=lambda x:np.mean(sp[x]),reverse=True):
    rets=sp[s]
    print(f'  {s:<12}: n={len(rets):>2}, avg={np.mean(rets):>+7.1f}%, win={len([x for x in rets if x>0])/len(rets)*100:.0f}%')

# Top winners
print(f'\n--- Top Winners (>+200%) ---')
big=[r for r in results if r['forward_return']>200]
big.sort(key=lambda x:x['forward_return'],reverse=True)
for r in big:
    print(f'  {r["code"]} S={r["score"]:>3} fwd={r["forward_return"]:>+7.1f}% {r["sector"]}')

# Top 20 by score
print(f'\n--- Top 20 by Score ---')
for i,r in enumerate(results[:20],1):
    print(f'{i:2d}. {r["code"]} S={r["score"]:>3} fwd={r["forward_return"]:>+7.1f}% DD={r["max_dd_fwd"]:>6.1f}% {r["sector"]}')

# Correlation
scores=[r['score'] for r in results]
fwds=[r['forward_return'] for r in results]
corr=np.corrcoef(scores,fwds)[0,1] if len(scores)>2 else 0
print(f'\nCorrelation(score, fwd_return): {corr:.3f}')

# Save
csv_path=os.path.join(DATA_DIR,'backtest_2024_results.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','screen_date','entry_price','final_price','forward_return','max_dd_fwd','days_held','score','reasons'])
    for r in results:
        w.writerow([r['code'],r['sector'],r['screen_date'],f'{r["entry_price"]:.2f}',f'{r["final_price"]:.2f}',
                   f'{r["forward_return"]:.1f}',f'{r["max_dd_fwd"]:.1f}',r['days_held'],r['score'],'|'.join(r['reasons'])])
print(f'\nSaved: {csv_path}')
print('DONE.')
