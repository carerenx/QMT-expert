"""
回测"前期发现"选股策略: 模拟2025年6月筛选, 跟踪至2026年6月的收益
"""
import sys, os, time, random, csv, requests, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\data\backtest_2025'
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

# ============ STEP 1: Get sector leaders in June 2025 sectors ============
print('='*60)
print('STEP 1: Getting sector leaders')
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
        cnt=0
        for it in items[:10]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code)
            sector_info[code]=bk_name
            cnt+=1
        print(f'  {bk_name}: {cnt} stocks')
    except Exception as e:
        print(f'  {bk_name}: ERROR {str(e)[:40]}')
    time.sleep(0.3)

# Also add known strong stocks from 2025 (manually curated)
extra = ['600519','000858','002475','601012','600809','000725','002129',
         '600487','601869','000100','002463','600703','600584','002281',
         '601318','600036','000001','002415','000063','600050']
for c in extra:
    if is_mainboard(c): candidates.add(c)

candidates = list(candidates)
print(f'\nTotal candidates: {len(candidates)}')

# ============ STEP 2: Fetch 260-day K-line (June 2025 ~ June 2026) ============
print('\n'+'='*60)
print('STEP 2: Fetching 260-day K-line + scoring at June 2025')
print('='*60)

results = []
for code in candidates:
    tc = f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r = requests.get(url, params={'param': f'{tc},day,,,260,qfq'},
                        headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=10)
        d = r.json()
        raw = d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if not raw or len(raw) < 130: continue  # Need at least ~6 months

        closes = [float(k[2]) for k in raw]
        highs = [float(k[3]) for k in raw]
        lows = [float(k[4]) for k in raw]
        volumes = [float(k[5]) for k in raw]
        dates = [k[0] for k in raw]
        total_bars = len(closes)

        # ---- Apply strategy at "June 2025" (bar ~60 from end) ----
        # We look at bars 0-60 as the "screening window" (approx Dec 2024 - June 2025)
        # Then track bars 60→end as forward performance
        screen_end = min(60, total_bars - 20)  # At least 20 bars of forward data
        if screen_end < 30: continue

        screen_closes = closes[:screen_end]
        screen_highs = highs[:screen_end]
        screen_volumes = volumes[:screen_end]
        sc = screen_closes[-1]  # "Current" price at screening date

        # MA calculations at screening point
        ma5 = sum(screen_closes[-5:])/5
        ma10 = sum(screen_closes[-10:])/10
        ma20 = sum(screen_closes[-20:])/20
        ma60 = sum(screen_closes[-60:])/60 if len(screen_closes)>=60 else ma20

        pct_ma20 = (sc - ma20)/ma20*100
        pct_ma60 = (sc - ma60)/ma60*100
        vol_avg = sum(screen_volumes[-20:])/20
        vol_recent = sum(screen_volumes[-5:])/5
        vol_ratio = vol_recent/vol_avg if vol_avg>0 else 1
        chg_20d = (sc - screen_closes[-21])/screen_closes[-21]*100 if len(screen_closes)>21 else 0
        high_10 = max(screen_highs[-10:])
        dd_10d = (sc - high_10)/high_10*100

        # ---- SCORING (same as V1.0 strategy) ----
        score = 0; reasons = []

        # Trend
        trend_aligned = sc > ma20 > ma60
        ma_bullish = ma5 > ma10 > ma20
        if trend_aligned: score+=20; reasons.append('多头排列')
        elif sc>ma20: score+=12
        if ma_bullish: score+=12; reasons.append('均线发散')
        elif ma5>ma20: score+=6

        # Position (early stage)
        if 0<pct_ma60<=15: score+=25; reasons.append(f'极早期({pct_ma60:.0f}%)')
        elif 15<pct_ma60<=25: score+=22
        elif 25<pct_ma60<=35: score+=17
        elif 35<pct_ma60<=50: score+=10
        elif 50<pct_ma60<=70: score+=5
        elif pct_ma60>100: score-=8

        # Volume
        if 1.2<vol_ratio<=1.8: score+=15; reasons.append(f'温和放量({vol_ratio:.1f}x)')
        elif 1.8<vol_ratio<=2.5: score+=11
        elif 1.0<vol_ratio<=1.2: score+=8

        # Momentum
        if 5<chg_20d<=15: score+=15; reasons.append(f'稳健(+{chg_20d:.0f}%)')
        elif 15<chg_20d<=25: score+=12
        elif 25<chg_20d<=45: score+=7
        elif chg_20d>60: score-=3

        # ---- Forward Performance ----
        forward_closes = closes[screen_end:]
        forward_dates = dates[screen_end:]
        if len(forward_closes) < 20: continue

        entry_price = closes[screen_end]  # Buy at screening date close
        final_price = closes[-1]
        forward_return = (final_price/entry_price - 1)*100

        # Max drawdown in forward period
        fwd_highs = np.maximum.accumulate(np.array(forward_closes))
        fwd_dd = (np.array(forward_closes) - fwd_highs)/fwd_highs*100
        max_dd_fwd = fwd_dd.min()
        max_dd_date = forward_dates[np.argmin(fwd_dd)] if len(fwd_dd)>0 else ''

        # Annualized
        days_held = len(forward_closes)
        ann_ret = ((1+forward_return/100)**(252/days_held)-1)*100 if days_held>0 else 0

        # Monthly returns
        monthly_rets = []
        for m_start in range(0, len(forward_closes), 22):
            m_end = min(m_start+22, len(forward_closes))
            if m_end > m_start:
                mr = (forward_closes[m_end-1]/forward_closes[m_start]-1)*100
                monthly_rets.append(mr)

        results.append({
            'code': code, 'sector': sector_info.get(code,''),
            'screen_date': dates[screen_end-1],
            'entry_price': entry_price, 'final_price': final_price,
            'forward_return': forward_return, 'annual_return': ann_ret,
            'max_dd_fwd': max_dd_fwd, 'days_held': days_held,
            'score': score, 'reasons': reasons,
            'pct_ma60': pct_ma60, 'pct_ma20': pct_ma20,
            'vol_ratio': vol_ratio, 'chg_20d': chg_20d, 'dd_10d': dd_10d,
            'monthly_rets': monthly_rets,
        })

        if score >= 50:
            print(f'  {code} score={score:>3} forward={forward_return:>+7.1f}% | {" | ".join(reasons[:2])}')

        time.sleep(0.15)

    except Exception as e:
        pass

results.sort(key=lambda x: x['score'], reverse=True)
print(f'\nTotal valid results: {len(results)}')

# ============ STEP 3: Analysis ============
print('\n'+'='*60)
print('STEP 3: Backtest Analysis')
print('='*60)

# BH benchmarks
if results:
    all_fwd = [r['forward_return'] for r in results]
    avg_fwd = np.mean(all_fwd)
    med_fwd = np.median(all_fwd)
    print(f'All {len(results)} stocks: avg forward={avg_fwd:.1f}%, median={med_fwd:.1f}%')

# By score tier
for tier_name, min_score in [('>=80',80),('65-79',65),('50-64',50),('<50',0)]:
    tier = [r for r in results if r['score']>=min_score and (tier_name=='<50' or r['score']<min_score+15)]
    if not tier: continue
    avg_r = np.mean([r['forward_return'] for r in tier])
    avg_dd = np.mean([r['max_dd_fwd'] for r in tier])
    win_rate = len([r for r in tier if r['forward_return']>0])/len(tier)*100
    print(f'  Score {tier_name}: n={len(tier)}, avg_ret={avg_r:.1f}%, avg_maxDD={avg_dd:.1f}%, win_rate={win_rate:.0f}%')

# Top 20 by score
print(f'\n=== TOP 20 by Strategy Score ===')
top = [r for r in results if r['score']>=50][:20]
for i, r in enumerate(top, 1):
    months_str = ','.join([f'{m:+.0f}%' for m in r['monthly_rets'][:6]]) if r['monthly_rets'] else ''
    print(f'{i:2d}. {r["code"]} S={r["score"]:>3} | forward={r["forward_return"]:>+8.1f}% | maxDD={r["max_dd_fwd"]:>6.1f}% | ann={r["annual_return"]:>+7.1f}% | {r["sector"]} | {" ".join(r["reasons"][:2])}')

# Top 20 by forward return (actual performance - what if we had perfect foresight)
print(f'\n=== TOP 20 by Actual Forward Return (hindsight) ===')
by_ret = sorted(results, key=lambda x: x['forward_return'], reverse=True)[:20]
for i, r in enumerate(by_ret, 1):
    print(f'{i:2d}. {r["code"]} S={r["score"]:>3} | forward={r["forward_return"]:>+8.1f}% | ann={r["annual_return"]:>+7.1f}% | {r["sector"]}')

# Correlation: score vs forward return
scores = [r['score'] for r in results]
fwd_rets = [r['forward_return'] for r in results]
corr = np.corrcoef(scores, fwd_rets)[0,1] if len(scores)>2 else 0
print(f'\nCorrelation(score, forward_return): {corr:.3f}')

# ============ SAVE ============
csv_path = os.path.join(DATA_DIR, 'backtest_results.csv')
with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['code','sector','screen_date','entry_price','final_price','forward_return','annual_return',
                'max_dd_fwd','days_held','score','pct_ma60','pct_ma20','vol_ratio','chg_20d','reasons'])
    for r in results:
        w.writerow([r['code'],r['sector'],r['screen_date'],f'{r["entry_price"]:.2f}',f'{r["final_price"]:.2f}',
                   f'{r["forward_return"]:.1f}',f'{r["annual_return"]:.1f}',f'{r["max_dd_fwd"]:.1f}',
                   r['days_held'],r['score'],f'{r["pct_ma60"]:.1f}',f'{r["pct_ma20"]:.1f}',
                   f'{r["vol_ratio"]:.1f}',f'{r["chg_20d"]:.1f}','|'.join(r["reasons"])])

print(f'\nSaved: {csv_path}')
print('DONE.')
