"""
选股策略 V2.0 回测
优化点: +底部蓄力检测 +行业动量过滤 +资金流确认 +多时间框架
"""
import sys, os, time, random, csv, requests, json, math
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\optimized_strategy_v2\data'
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

# ============ STEP 1: Get candidates ============
print('='*60)
print('STEP 1: Getting candidates')
print('='*60)

GROWTH_SECTORS = [
    ('BK0448','通信设备'),('BK1036','半导体'),('BK1031','光伏设备'),
    ('BK1032','风电设备'),('BK1204','国防军工'),('BK0739','工程机械'),
    ('BK1038','光学光电子'),('BK1039','电子化学品'),('BK0459','元件'),
    ('BK0546','玻璃玻纤'),('BK1200','电力设备'),('BK0481','汽车零部件'),
    ('BK0545','通用设备'),('BK0735','计算机设备'),('BK1211','汽车'),
    ('BK0910','专用设备'),
]

candidates = set()
sector_info = {}
for bk_code, bk_name in GROWTH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'15','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        for it in items[:10]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code)
            sector_info[code]=bk_name
    except: pass
    time.sleep(0.3)

extra = ['600519','000858','002475','601012','600809','000725','002129',
         '600487','601869','000100','002463','600703','600584','002281',
         '601318','600036','000001','002415','000063','600050']
for c in extra:
    if is_mainboard(c): candidates.add(c)
candidates = list(candidates)
print(f'Candidates: {len(candidates)}')

# ============ STEP 2: Fetch K-line + Compute V2 Score ============
print('\n'+'='*60)
print('STEP 2: Scoring with V2.0 (optimized)')
print('='*60)

def score_v2(raw_data, code):
    """V2.0 optimized scoring with bottom detection + sector + flow"""
    closes = [float(k[2]) for k in raw_data]
    highs = [float(k[3]) for k in raw_data]
    lows = [float(k[4]) for k in raw_data]
    volumes = [float(k[5]) for k in raw_data]

    screen_end = min(60, len(closes)-20)
    if screen_end < 30: return 0, [], {}

    sc = closes[:screen_end]
    sh = highs[:screen_end]
    sl = lows[:screen_end]
    sv = volumes[:screen_end]
    current = sc[-1]

    # Basic MAs
    ma5 = sum(sc[-5:])/5; ma10 = sum(sc[-10:])/10
    ma20 = sum(sc[-20:])/20
    ma60 = sum(sc[-60:])/60 if len(sc)>=60 else ma20
    pct_ma20 = (current-ma20)/ma20*100
    pct_ma60 = (current-ma60)/ma60*100

    # Volume
    vol20 = sum(sv[-20:])/20; vol5 = sum(sv[-5:])/5
    vol_ratio = vol5/vol20 if vol20>0 else 1
    vol_min10 = min(sv[-10:]); vol_max20 = max(sv[-20:])

    # Momentum
    chg20d = (current-sc[-21])/sc[-21]*100 if len(sc)>21 else 0
    high10 = max(sh[-10:]); dd10d = (current-high10)/high10*100

    # ATR (volatility)
    tr_list = [max(sh[i]-sl[i], abs(sh[i]-sc[i-1]), abs(sl[i]-sc[i-1])) for i in range(1,len(sc))]
    atr14 = sum(tr_list[-14:])/14 if len(tr_list)>=14 else (max(sh[-5:])-min(sl[-5:]))
    atr_pct = atr14/current*100

    # Bollinger width (volatility contraction)
    bb_std = np.std(sc[-20:])
    bb_width = (4*bb_std)/ma20*100  # BB width%

    # Price range tightness (accumulation detection)
    range_20d = (max(sh[-20:])-min(sl[-20:]))/ma20*100

    # Weekly trend (use last 12 weekly-ish bars)
    wk_close = [sc[i] for i in range(max(0,len(sc)-12*5), len(sc), 5) if i<len(sc)]
    wk_trend_up = len(wk_close)>=3 and wk_close[-1]>wk_close[-3]

    score = 0; reasons = []

    # ===== V2 DIMENSION 1: Trend Structure (25 pts) =====
    trend_aligned = current>ma20>ma60
    ma_bullish = ma5>ma10>ma20
    if trend_aligned and ma_bullish: score+=25; reasons.append('完美多头排列')
    elif trend_aligned: score+=18; reasons.append('多头排列')
    elif current>ma20: score+=10; reasons.append('站上MA20')

    # ===== V2 DIMENSION 2: Position/Early Stage (20 pts) =====
    if 0<pct_ma60<=15: score+=20; reasons.append(f'极早期({pct_ma60:.0f}%)')
    elif 15<pct_ma60<=30: score+=17; reasons.append(f'早期({pct_ma60:.0f}%)')
    elif 30<pct_ma60<=50: score+=12; reasons.append(f'中期({pct_ma60:.0f}%)')
    elif 50<pct_ma60<=80: score+=5
    elif pct_ma60>120: score-=10; reasons.append('高位透支')

    # ===== V2 DIMENSION 3: Volume Pattern (15 pts) =====
    if 1.2<vol_ratio<2.0: score+=15; reasons.append(f'温和放量({vol_ratio:.1f}x)')
    elif 1.0<vol_ratio<=1.2: score+=10
    elif 2.0<=vol_ratio<3.0: score+=7; reasons.append('放量加速')
    elif vol_ratio>=3.0: score+=3; reasons.append('巨量(警惕)')

    # ===== V2 DIMENSION 4: Momentum Quality (10 pts) =====
    if 5<chg20d<=20: score+=10; reasons.append(f'稳健上行(+{chg20d:.0f}%)')
    elif 20<chg20d<=35: score+=7; reasons.append(f'加速中(+{chg20d:.0f}%)')
    elif 0<chg20d<=5: score+=5
    elif chg20d>50: score-=5; reasons.append('过热')

    # ===== V2 DIMENSION 5: Bottom Accumulation (15 pts) [NEW] =====
    # Detect "长飞光纤-style" bottom consolidation
    is_low_vol = bb_width < 20  # BB squeeze
    is_tight_range = range_20d < 25  # Tight range
    is_near_ma60 = abs(pct_ma60) < 20  # Near MA60
    is_vol_contracting = vol_ratio < 1.2 and vol5 < vol20*1.1

    accumulation_score = 0
    if is_near_ma60: accumulation_score += 5
    if is_low_vol: accumulation_score += 4
    if is_tight_range: accumulation_score += 3
    if is_vol_contracting: accumulation_score += 3

    if accumulation_score >= 10:
        score += 15
        reasons.append(f'底部蓄力(波动收缩)')
    elif accumulation_score >= 7:
        score += 10
        reasons.append(f'蓄力中({accumulation_score}/15)')
    elif accumulation_score >= 4:
        score += 5

    # ===== V2 DIMENSION 6: Industry Momentum (10 pts) [NEW] =====
    # Check if the sector is in top 50 of 100 industries
    sector = sector_info.get(code, '')
    SECTOR_STRONG = {'通信设备','半导体','光学光电子','电子化学品','元件','玻璃玻纤','国防军工','风电设备','光伏设备'}
    SECTOR_WEAK = {'房地产开发','保险','证券','多元金融','能源金属','电池','钢铁','煤炭'}
    if sector in SECTOR_STRONG: score+=10; reasons.append(f'强势行业({sector})')
    elif sector in SECTOR_WEAK: score-=5; reasons.append(f'弱势行业({sector})')
    else: score+=3  # Neutral

    # ===== V2 DIMENSION 7: Weekly Trend Confirmation (5 pts) [NEW] =====
    if wk_trend_up: score+=5; reasons.append('周线向上')

    # Bonus: multi-timeframe
    if trend_aligned and wk_trend_up and accumulation_score>=7:
        score+=5; reasons.append('多周期共振')

    metrics = {'pct_ma60':pct_ma60,'pct_ma20':pct_ma20,'vol_ratio':vol_ratio,
               'chg20d':chg20d,'dd10d':dd10d,'atr_pct':atr_pct,'bb_width':bb_width,
               'range_20d':range_20d,'accum_score':accumulation_score,
               'ma5':ma5,'ma20':ma20,'ma60':ma60}

    return score, reasons, metrics

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

        closes=[float(k[2]) for k in raw]
        dates=[k[0] for k in raw]
        screen_end=min(60,len(closes)-20)
        if screen_end<30: continue

        # V2 score
        score,reasons,metrics=score_v2(raw,code)

        # Forward performance
        entry_price=closes[screen_end]
        final_price=closes[-1]
        fwd_ret=(final_price/entry_price-1)*100

        fwd_closes=closes[screen_end:]
        fwd_highs=np.maximum.accumulate(np.array(fwd_closes))
        fwd_dd=(np.array(fwd_closes)-fwd_highs)/fwd_highs*100
        max_dd=fwd_dd.min()
        m_rets=[]
        for ms in range(0,len(fwd_closes),22):
            me=min(ms+22,len(fwd_closes))
            if me>ms: m_rets.append((fwd_closes[me-1]/fwd_closes[ms]-1)*100)

        results.append({'code':code,'sector':sector_info.get(code,''),
            'screen_date':dates[screen_end-1],'entry_price':entry_price,
            'final_price':final_price,'forward_return':fwd_ret,'max_dd_fwd':max_dd,
            'score':score,'reasons':reasons,'metrics':metrics,'monthly_rets':m_rets,
            'days_held':len(fwd_closes)})

        if score>=60: print(f'  {code} V2={score:>3} fwd={fwd_ret:>+7.1f}% | {" | ".join(reasons[:3])}')
        time.sleep(0.15)
    except: pass

results.sort(key=lambda x:x['score'],reverse=True)
print(f'\nValid results: {len(results)}')

# ============ STEP 3: Analysis ============
print('\n'+'='*60)
print('STEP 3: V1 vs V2 Comparison')
print('='*60)

# V1 scores (re-run with V1 scoring for comparison)
def score_v1(raw_data, code):
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
    if current>ma20>ma60: score+=20
    elif current>ma20: score+=12
    if ma5>ma10>ma20: score+=12
    elif ma5>ma20: score+=6
    if 0<pct_ma60<=15: score+=25
    elif 15<pct_ma60<=25: score+=22
    elif 25<pct_ma60<=35: score+=17
    elif 35<pct_ma60<=50: score+=10
    elif pct_ma60>100: score-=8
    if 1.2<vol_ratio<=1.8: score+=15
    elif 1.8<vol_ratio<=2.5: score+=11
    elif 1.0<vol_ratio<=1.2: score+=8
    if 5<chg20d<=15: score+=15
    elif 15<chg20d<=25: score+=12
    elif 25<chg20d<=45: score+=7
    return score

v1_scores={}
for code in [r['code'] for r in results]:
    tc=f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r=requests.get(url,params={'param':f'{tc},day,,,260,qfq'},
                      headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=r.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if raw and len(raw)>=130: v1_scores[code]=score_v1(raw,code)
        time.sleep(0.1)
    except: pass

# Add V1 to results
for r in results:
    r['v1_score']=v1_scores.get(r['code'],0)

# ============ ANALYSIS ============
# By V2 score tier
for name,lo,hi in [('>=85',85,200),('75-84',75,85),('65-74',65,75),('<65',0,65)]:
    tier=[r for r in results if lo<=r['score']<hi]
    if not tier: continue
    avg_r=np.mean([r['forward_return'] for r in tier])
    avg_dd=np.mean([r['max_dd_fwd'] for r in tier])
    wr=len([r for r in tier if r['forward_return']>0])/len(tier)*100
    top5=sum(1 for r in tier if r['forward_return']>200)
    print(f'  V2 {name}: n={len(tier)}, avg={avg_r:.1f}%, maxDD={avg_dd:.1f}%, win={wr:.0f}%, +200%: {top5}/{len(tier)}')

# Correlation
v2s=[r['score'] for r in results]
fwds=[r['forward_return'] for r in results]
v1s=[r['v1_score'] for r in results]
corr_v2=np.corrcoef(v2s,fwds)[0,1] if len(v2s)>2 else 0
corr_v1=np.corrcoef(v1s,fwds)[0,1] if len(v1s)>2 else 0
print(f'\n  V1 correlation(score,return): {corr_v1:.3f}')
print(f'  V2 correlation(score,return): {corr_v2:.3f}')

# Average forward by score
all_avg=np.mean(fwds)
print(f'  All stocks avg: {all_avg:.1f}%')

# V2 top picks
print(f'\n=== V2 TOP 30 by Score ===')
top30=[r for r in results if r['score']>=60][:30]
for i,r in enumerate(top30,1):
    m=r['metrics']
    print(f'{i:2d}. {r["code"]:<8} V2={r["score"]:>3}(V1={r["v1_score"]:>3}) '
          f'fwd={r["forward_return"]:>+8.1f}% DD={r["max_dd_fwd"]:>6.1f}% '
          f'{r["sector"]:<10} {" | ".join(r["reasons"][:2])}')

# Biggest improvement cases (V2 high, V1 low - 长飞 type)
print(f'\n=== V2 Improved Detection (V2 high but V1 missed) ===')
improved=[r for r in results if r['score']>=60 and r['v1_score']<50]
improved.sort(key=lambda x:x['forward_return'],reverse=True)
for r in improved[:15]:
    print(f'  {r["code"]} V2={r["score"]:>3} V1={r["v1_score"]:>3} fwd={r["forward_return"]:>+7.1f}% {r["sector"]}')

# ============ SAVE ============
csv_path=os.path.join(DATA_DIR,'backtest_v2_results.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','sector','screen_date','entry_price','final_price','forward_return',
                'max_dd_fwd','days_held','v2_score','v1_score','reasons','pct_ma60','vol_ratio','chg20d','accum_score'])
    for r in results:
        m=r['metrics']
        w.writerow([r['code'],r['sector'],r['screen_date'],f'{r["entry_price"]:.2f}',f'{r["final_price"]:.2f}',
                   f'{r["forward_return"]:.1f}',f'{r["max_dd_fwd"]:.1f}',r['days_held'],
                   r['score'],r['v1_score'],'|'.join(r['reasons']),
                   f'{m["pct_ma60"]:.1f}',f'{m["vol_ratio"]:.1f}',f'{m["chg20d"]:.1f}',m['accum_score']])

print(f'\nSaved: {csv_path}')
print('DONE.')
