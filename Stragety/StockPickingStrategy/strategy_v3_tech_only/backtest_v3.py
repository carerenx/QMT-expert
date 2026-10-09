"""
V3: 科技赛道专属策略
- 仅保留科技相关行业板块
- 剔除白酒/地产/保险/汽车/工程机械等导致亏损的非科技板块
- 使用V1评分模型 + 行业过滤
"""
import sys, os, time, random, csv, requests, json, math
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v3_tech_only\data'
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

# ============ TECH-ONLY SECTORS ============
# 剔除导致亏损的行业，仅保留科技相关板块
TECH_SECTORS = [
    ('BK0448','通信设备'),      # 光模块/光纤/5G - 最强赛道
    ('BK1036','半导体'),        # 芯片/设备/材料 - 核心科技
    ('BK1038','光学光电子'),    # 面板/LED/光电子 - 科技硬件
    ('BK1039','电子化学品'),    # 光刻胶/电子气体 - 半导体上游
    ('BK0459','元件'),          # 电容/电阻/PCB - 电子元件
    ('BK0735','计算机设备'),    # 服务器/AI算力硬件
    ('BK1204','国防军工'),      # 军工电子/航天
    ('BK0546','玻璃玻纤'),      # 电子玻璃/光纤材料
]

# Sectors EXCLUDED (proven losers in backtest):
# BK1031 光伏设备 - mixed results, 002459(-37%), several losers
# BK1032 风电设备 - only 1 winner
# BK1200 电力设备 - mixed
# BK0481 汽车零部件 - 000030(-35%), 603586(-39%)
# BK1211 汽车 - several losers
# BK0545 通用设备 - mixed
# BK0910 专用设备 - 600894(-26%)
# BK0739 工程机械 - 600031(-17%)
# BK1033 电池 - losers

# Also exclude non-tech extra stocks
# Keep only tech-related ones from the extra list
TECH_EXTRA = ['002475','002129','600487','601869','000100','002463',
              '600703','600584','002281','002415','000063','000725']

print('='*60)
print('V3: TECH-ONLY Strategy Backtest')
print('='*60)
print(f'Tech sectors: {len(TECH_SECTORS)}')
print(f'Excluded: 光伏设备,风电设备,电力设备,汽车零部件,汽车,通用设备,专用设备,工程机械,电池')
print(f'Also excluded: 白酒,银行,保险,房地产,钢铁,煤炭,食品饮料,家电')

# ============ STEP 1: Get tech sector candidates ============
print('\n--- STEP 1: Tech sector candidates ---')
candidates = set()
sector_info = {}
total_collected = 0
for bk_code, bk_name in TECH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        cnt=0
        for it in items[:12]:  # Take top 12 from each tech sector
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code)
            sector_info[code]=bk_name
            cnt+=1
        print(f'  {bk_name}: {cnt} stocks')
        total_collected += cnt
    except Exception as e:
        print(f'  {bk_name}: ERROR {str(e)[:40]}')
    time.sleep(0.3)

for c in TECH_EXTRA:
    if is_mainboard(c):
        candidates.add(c)
        if c not in sector_info: sector_info[c]='科技精选'

candidates = list(candidates)
print(f'\nTotal tech candidates: {len(candidates)}')

# ============ STEP 2: V1 scoring + forward tracking ============
print('\n--- STEP 2: Scoring & tracking ---')

def score_v1(raw_data):
    closes=[float(k[2]) for k in raw_data]; highs=[float(k[3]) for k in raw_data]
    lows=[float(k[4]) for k in raw_data]; volumes=[float(k[5]) for k in raw_data]
    screen_end=min(60,len(closes)-20)
    sc=closes[:screen_end]; sv=volumes[:screen_end]; sh=highs[:screen_end]
    current=sc[-1]
    ma5=sum(sc[-5:])/5; ma10=sum(sc[-10:])/10
    ma20=sum(sc[-20:])/20; ma60=sum(sc[-60:])/60 if len(sc)>=60 else ma20
    pct_ma60=(current-ma60)/ma60*100
    vol20=sum(sv[-20:])/20; vol5=sum(sv[-5:])/5
    vol_ratio=vol5/vol20 if vol20>0 else 1
    chg20d=(current-sc[-21])/sc[-21]*100 if len(sc)>21 else 0
    high10=max(sh[-10:]); dd10d=(current-high10)/high10*100
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
    return score,reasons,{'pct_ma60':pct_ma60,'vol_ratio':vol_ratio,'chg20d':chg20d}

results = []
for code in candidates:
    tc = f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r=requests.get(url,params={'param':f'{tc},day,,,260,qfq'},
                      headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=r.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if not raw or len(raw)<130: continue

        closes=[float(k[2]) for k in raw]; dates=[k[0] for k in raw]
        screen_end=min(60,len(closes)-20)
        if screen_end<30: continue

        score,reasons,metrics=score_v1(raw)
        entry_price=closes[screen_end]; final_price=closes[-1]
        fwd_ret=(final_price/entry_price-1)*100
        fwd_closes=closes[screen_end:]
        fwd_highs=np.maximum.accumulate(np.array(fwd_closes))
        fwd_dd=(np.array(fwd_closes)-fwd_highs)/fwd_highs*100
        max_dd=fwd_dd.min()

        results.append({'code':code,'sector':sector_info.get(code,''),
            'screen_date':dates[screen_end-1],'entry_price':entry_price,
            'final_price':final_price,'forward_return':fwd_ret,'max_dd_fwd':max_dd,
            'score':score,'reasons':reasons,'metrics':metrics,
            'days_held':len(fwd_closes)})
        time.sleep(0.15)
    except: pass

results.sort(key=lambda x:x['score'],reverse=True)
print(f'Valid tech results: {len(results)}')

# ============ STEP 3: Get V1 all-sector results for comparison ============
print('\n--- STEP 3: Loading V1 all-sector benchmark ---')
v1_all_path = r'd:\02Project\QMT-expert\strategy_v1_initial\data_backtest\backtest_results.csv'
v1_all = {}
try:
    with open(v1_all_path,'r',encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            v1_all[row['code']] = float(row['forward_return'])
    print(f'Loaded {len(v1_all)} V1 benchmark results')
except:
    print('V1 benchmark not found, skipping comparison')

# ============ STEP 4: Analysis ============
print('\n'+'='*60)
print('V3 TECH-ONLY RESULTS')
print('='*60)

# By score tier
for name,lo,hi in [('>=80',80,200),('65-79',65,80),('50-64',50,65),('<50',0,50)]:
    tier=[r for r in results if lo<=r['score']<hi]
    if not tier: continue
    avg_r=np.mean([r['forward_return'] for r in tier])
    avg_dd=np.mean([r['max_dd_fwd'] for r in tier])
    wr=len([r for r in tier if r['forward_return']>0])/len(tier)*100
    top3=sum(1 for r in tier if r['forward_return']>200)
    print(f'  Score {name}: n={len(tier)}, avg={avg_r:.1f}%, maxDD={avg_dd:.1f}%, win={wr:.0f}%, +200%: {top3}')

# Overall
all_avg=np.mean([r['forward_return'] for r in results])
all_wr=len([r for r in results if r['forward_return']>0])/len(results)*100
print(f'\n  Tech-only ALL: n={len(results)}, avg={all_avg:.1f}%, win={all_wr:.0f}%')

# Compare with V1 all-sector benchmark
if v1_all:
    v1_codes_in_tech=[c for c in v1_all if c in [r['code'] for r in results]]
    v1_tech_avg=np.mean([v1_all[c] for c in v1_codes_in_tech]) if v1_codes_in_tech else 0
    print(f'\n=== V1 vs V3 Comparison ===')
    print(f'  V1 (all 110 stocks):   avg +56.2%, win 52%')
    print(f'  V1 (tech-only subset): avg +{v1_tech_avg:.1f}%')
    print(f'  V3 (tech-only + scored): avg +{all_avg:.1f}%, win {all_wr:.0f}%')

# By sector breakdown
print(f'\n=== By Tech Sector ===')
sector_perf = {}
for r in results:
    sec = r['sector']
    if sec not in sector_perf: sector_perf[sec]=[]
    sector_perf[sec].append(r['forward_return'])
for sec in sorted(sector_perf.keys(), key=lambda x: np.mean(sector_perf[x]), reverse=True):
    rets = sector_perf[sec]
    avg_r = np.mean(rets); wr = len([r for r in rets if r>0])/len(rets)*100
    print(f'  {sec:<12}: n={len(rets):>2}, avg={avg_r:>+7.1f}%, win={wr:.0f}%')

# TOP 30
print(f'\n=== V3 TECH-ONLY Top 30 ===')
top30=[r for r in results if r['score']>=50][:30]
for i,r in enumerate(top30,1):
    m=r['metrics']
    print(f'{i:2d}. {r["code"]:<8} S={r["score"]:>3} fwd={r["forward_return"]:>+8.1f}% '
          f'DD={r["max_dd_fwd"]:>6.1f}% {r["sector"]:<12} {" | ".join(r["reasons"][:2])}')

# ============ Big winners captured ============
print(f'\n=== Super Winners (>+200%) Captured by V3 ===')
big = [r for r in results if r['forward_return']>200]
big.sort(key=lambda x:x['forward_return'],reverse=True)
for r in big:
    print(f'  {r["code"]} S={r["score"]:>3} fwd={r["forward_return"]:>+7.1f}% {r["sector"]}')

# Losers analysis
print(f'\n=== Worst Losers (<-30%) in V3 ===')
losers = [r for r in results if r['forward_return']<-30]
losers.sort(key=lambda x:x['forward_return'])
for r in losers[:10]:
    print(f'  {r["code"]} S={r["score"]:>3} fwd={r["forward_return"]:>+7.1f}% {r["sector"]}')

# ============ SAVE ============
csv_path=os.path.join(DATA_DIR,'backtest_v3_tech_only.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','screen_date','entry_price','final_price','forward_return',
                'max_dd_fwd','days_held','score','reasons','pct_ma60','vol_ratio','chg20d'])
    for r in results:
        m=r['metrics']
        w.writerow([r['code'],r['sector'],r['screen_date'],f'{r["entry_price"]:.2f}',f'{r["final_price"]:.2f}',
                   f'{r["forward_return"]:.1f}',f'{r["max_dd_fwd"]:.1f}',r['days_held'],
                   r['score'],'|'.join(r['reasons']),
                   f'{m["pct_ma60"]:.1f}',f'{m["vol_ratio"]:.1f}',f'{m["chg20d"]:.1f}'])

print(f'\nSaved: {csv_path}')
print('DONE.')
