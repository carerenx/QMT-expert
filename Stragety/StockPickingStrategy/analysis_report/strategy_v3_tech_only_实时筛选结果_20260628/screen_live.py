"""
V3 Live Screening: 基于科技赛道策略, 实时筛选当前市场"类似长飞光纤早期阶段"的标的
"""
import sys, os, time, random, csv, requests, urllib.request, json
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\strategy_v3_tech_only\data_live'
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

# V3 TECH SECTORS
TECH_SECTORS = [
    ('BK0448','通信设备'), ('BK1036','半导体'), ('BK1038','光学光电子'),
    ('BK1039','电子化学品'), ('BK0459','元件'), ('BK0735','计算机设备'),
    ('BK1204','国防军工'), ('BK0546','玻璃玻纤'),
]

print('='*60)
print('V3 LIVE SCREENING: 寻找"下一个长飞光纤"')
print('='*60)

# ===== STEP 1: Get tech sector candidates =====
print('\n--- Collecting tech sector stocks ---')
candidates = set()
sector_info = {}
for bk_code, bk_name in TECH_SECTORS:
    try:
        url='https://push2.eastmoney.com/api/qt/clist/get'
        params={'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                'fs':f'b:{bk_code}','fields':'f2,f3,f12,f14,f20,f62'}
        r=em_get(url,params=params)
        items=r.json().get('data',{}).get('diff',[]) or []
        cnt=0
        for it in items[:15]:
            code=it.get('f12','')
            if not is_mainboard(code): continue
            candidates.add(code)
            sector_info[code]=bk_name
            cnt+=1
        print(f'  {bk_name}: {cnt} stocks')
    except Exception as e:
        print(f'  {bk_name}: ERROR {str(e)[:50]}')
    time.sleep(0.3)

# Add key tech stocks
tech_extra = ['002475','002129','600487','601869','000100','002463',
              '600703','600584','002281','002415','000063','000725','002106']
for c in tech_extra:
    if is_mainboard(c):
        candidates.add(c)
        if c not in sector_info: sector_info[c]='科技精选'

candidates = list(candidates)
print(f'Total candidates: {len(candidates)}')

# ===== STEP 2: Fetch Tencent quotes + K-line + Score =====
print('\n--- Scoring candidates ---')

def fetch_quotes(codes):
    quotes={}
    for i in range(0,len(codes),80):
        batch=codes[i:i+80]
        prefixed=[f'sh{c}' if c.startswith(('6','9')) else f'sz{c}' for c in batch]
        url='https://qt.gtimg.cn/q='+','.join(prefixed)
        req=urllib.request.Request(url); req.add_header('User-Agent',UA)
        try:
            resp=urllib.request.urlopen(req,timeout=15)
            data=resp.read().decode('gbk')
            for line in data.strip().split(';'):
                if '=' not in line or '"' not in line: continue
                vals=line.split('"')[1].split('~')
                if len(vals)<53: continue
                code=line.split('=')[0].split('_')[-1][2:]
                quotes[code]={
                    'name':vals[1],'price':float(vals[3]) if vals[3] else 0,
                    'pe_ttm':float(vals[39]) if vals[39] else 0,
                    'pb':float(vals[46]) if vals[46] else 0,
                    'mcap_yi':float(vals[44]) if vals[44] else 0,
                    'change_pct':float(vals[32]) if vals[32] else 0,
                    'turnover_pct':float(vals[38]) if vals[38] else 0,
                    'amount_wan':float(vals[37]) if vals[37] else 0,
                }
        except: pass
        time.sleep(0.5)
    return quotes

quotes = fetch_quotes(candidates)
print(f'Quotes: {len(quotes)}')

def score_current(raw_data, code):
    """Score using V3 criteria on CURRENT data"""
    closes=[float(k[2]) for k in raw_data]; highs=[float(k[3]) for k in raw_data]
    lows=[float(k[4]) for k in raw_data]; volumes=[float(k[5]) for k in raw_data]
    current=closes[-1]
    n=len(closes)
    if n<30: return 0,[],{}

    ma5=sum(closes[-5:])/5; ma10=sum(closes[-10:])/10
    ma20=sum(closes[-20:])/20
    ma60=sum(closes[-60:])/60 if n>=60 else ma20
    pct_ma20=(current-ma20)/ma20*100; pct_ma60=(current-ma60)/ma60*100
    vol20=sum(volumes[-20:])/20; vol5=sum(volumes[-5:])/5
    vol_ratio=vol5/vol20 if vol20>0 else 1
    chg20d=(current-closes[-21])/closes[-21]*100 if n>21 else 0
    high10=max(highs[-10:]); dd10d=(current-high10)/high10*100

    # ATR
    tr=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,n)]
    atr14=sum(tr[-14:])/14 if len(tr)>=14 else (max(highs[-5:])-min(lows[-5:]))
    atr_pct=atr14/current*100

    # BB width
    bb_std=np.std(closes[-20:]); bb_width=(4*bb_std)/ma20*100
    range_20d=(max(highs[-20:])-min(lows[-20:]))/ma20*100

    score=0; reasons=[]

    # Trend (25)
    trend_ok=current>ma20>ma60
    ma_ok=ma5>ma10>ma20
    if trend_ok and ma_ok: score+=25; reasons.append('完美多头')
    elif trend_ok: score+=18; reasons.append('多头排列')
    elif current>ma20: score+=10

    # Position (20) - KEY for "early stage"
    if 0<pct_ma60<=15: score+=20; reasons.append(f'极早期({pct_ma60:.0f}%)')
    elif 15<pct_ma60<=30: score+=17; reasons.append(f'早期({pct_ma60:.0f}%)')
    elif 30<pct_ma60<=50: score+=12
    elif pct_ma60>120: score-=10; reasons.append('高位透支')

    # Volume (15)
    if 1.2<vol_ratio<2.0: score+=15; reasons.append(f'温和放量({vol_ratio:.1f}x)')
    elif 1.0<vol_ratio<=1.2: score+=10
    elif 2.0<=vol_ratio<3.0: score+=7
    elif vol_ratio>=3.0: score+=3

    # Momentum (10)
    if 5<chg20d<=20: score+=10; reasons.append(f'稳健(+{chg20d:.0f}%)')
    elif 20<chg20d<=35: score+=7
    elif chg20d>50: score-=5; reasons.append('短期过热')

    # Bottom Accumulation (15) - detect "长飞光纤 2025 mid" pattern
    accum=0
    if abs(pct_ma60)<20: accum+=5
    if bb_width<20: accum+=4
    if range_20d<25: accum+=3
    if vol_ratio<1.2: accum+=3
    if accum>=10: score+=15; reasons.append('底部蓄力')
    elif accum>=7: score+=10
    elif accum>=4: score+=5

    # Sector bonus (10)
    STRONG={'通信设备','半导体','元件','玻璃玻纤','电子化学品'}
    WEAK=set()
    sec=sector_info.get(code,'')
    if sec in STRONG: score+=10; reasons.append(f'强势行业({sec})')
    elif sec in WEAK: score-=5
    else: score+=3

    metrics={'pct_ma60':pct_ma60,'pct_ma20':pct_ma20,'vol_ratio':vol_ratio,
             'chg20d':chg20d,'atr_pct':atr_pct,'bb_width':bb_width,'range_20d':range_20d}
    return score,reasons,metrics

results=[]
for code in list(quotes.keys()):
    tc=f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        r=requests.get(url,params={'param':f'{tc},day,,,120,qfq'},
                      headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=r.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if not raw or len(raw)<30: continue

        score,reasons,metrics=score_current(raw,code)
        q=quotes[code]

        # Exclude ST
        if q['name'].startswith('*ST') or q['name'].startswith('ST'): continue
        # Exclude price<3
        if q['price']<3: continue

        results.append({'code':code,'name':q['name'],'price':q['price'],
            'pe_ttm':q['pe_ttm'],'pb':q['pb'],'mcap_yi':q['mcap_yi'],
            'change_pct':q['change_pct'],'turnover_pct':q['turnover_pct'],
            'sector':sector_info.get(code,''),'score':score,'reasons':reasons,'metrics':metrics})

        if score>=55:
            print(f'  {code} {q["name"]:<8} S={score:>3} {" | ".join(reasons[:3])}')
        time.sleep(0.15)
    except: pass

results.sort(key=lambda x:x['score'],reverse=True)
print(f'\nValid results: {len(results)}')

# ===== STEP 3: Save K-line for top 20 =====
print('\n--- Saving K-line for top 20 ---')
for r in results[:20]:
    code=r['code']; safe=r['name'].replace('*','ST').replace('/','_')
    tc=f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        resp=requests.get(url,params={'param':f'{tc},day,,,120,qfq'},
                         headers={'User-Agent':UA,'Referer':'https://gu.qq.com/'},timeout=10)
        d=resp.json()
        raw=d.get('data',{}).get(tc,{}).get('qfqday',[]) or d.get('data',{}).get(tc,{}).get('day',[])
        if raw:
            kp=os.path.join(DATA_DIR,f'kline_{code}_{safe}.csv')
            with open(kp,'w',newline='',encoding='utf-8-sig') as f:
                w=csv.writer(f); w.writerow(['date','open','close','high','low','volume'])
                for k in raw: w.writerow(k[:6])
        time.sleep(0.15)
    except: pass

# ===== SAVE =====
csv_path=os.path.join(DATA_DIR,'live_screening_results.csv')
with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['code','name','price','pe_ttm','pb','mcap_yi','change_pct','turnover',
                'sector','score','pct_ma60','vol_ratio','chg20d','reasons'])
    for r in results:
        m=r['metrics']
        w.writerow([r['code'],r['name'],f'{r["price"]:.2f}',f'{r["pe_ttm"]:.1f}',f'{r["pb"]:.1f}',
                   f'{r["mcap_yi"]:.0f}',f'{r["change_pct"]:.1f}',f'{r["turnover_pct"]:.1f}',
                   r['sector'],r['score'],f'{m["pct_ma60"]:.1f}',f'{m["vol_ratio"]:.1f}',
                   f'{m["chg20d"]:.1f}','|'.join(r['reasons'])])

# ===== PRINT TOP RESULTS =====
print('\n'+'='*60)
print('TOP RECOMMENDATIONS')
print('='*60)
for i,r in enumerate(results[:25],1):
    m=r['metrics']
    stage='🟢极早' if abs(m['pct_ma60'])<15 else ('🟡早期' if abs(m['pct_ma60'])<30 else '🟠中期')
    print(f'{i:2d}. {r["code"]} {r["name"]:<8} ¥{r["price"]:>8.1f} PE={r["pe_ttm"]:>5.0f} '
          f'S={r["score"]:>3} {stage} {r["sector"]:<10} | {" ".join(r["reasons"][:2])}')

print(f'\nData saved to: {DATA_DIR}')
print(f'  live_screening_results.csv ({len(results)} stocks)')
print(f'  kline_*.csv (top 20)')
print('DONE.')
