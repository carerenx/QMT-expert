"""
Screen main-board stocks for early breakout patterns similar to 长飞光纤(601869)
Key: Find stocks in EARLY stage of a potential super-trend
"""
import sys, os, time, random, csv, urllib.request, requests
from datetime import date, timedelta
from collections import Counter
sys.stdout.reconfigure(encoding='utf-8')

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
DATA_DIR = r'd:\02Project\QMT-expert\data\similar_screening'
os.makedirs(DATA_DIR, exist_ok=True)
EM_SESSION = requests.Session()
EM_SESSION.headers.update({'User-Agent': UA})
_em_last = [0.0]

def em_get(url, params=None, headers=None, timeout=15):
    wait = 1.3 - (time.time() - _em_last[0])
    if wait > 0: time.sleep(wait + random.uniform(0.1, 0.5))
    try: return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout)
    finally: _em_last[0] = time.time()

def is_mainboard(code):
    """Only Shanghai (60xxxx) and Shenzhen main board (00xxxx)"""
    if code.startswith('300') or code.startswith('301'): return False
    if code.startswith('688') or code.startswith('689'): return False
    if code.startswith('8') or code.startswith('4'): return False
    return True

# ============ STEP 1: Collect hot stocks from THS ============
print('='*60)
print('STEP 1: Collecting THS hot stocks (last 8 days)')
print('='*60)

hot_stocks = {}
for i in range(8):
    d = date.today() - timedelta(days=i+1)
    ds = d.strftime('%Y-%m-%d')
    try:
        url = f'http://zx.10jqka.com.cn/event/api/getharden/date/{ds}/orderby/date/orderway/desc/charset/GBK/'
        r = requests.get(url, headers={'User-Agent': UA}, timeout=10)
        data = r.json()
        if data.get('errocode', 0) != 0: continue
        rows = data.get('data') or []
        if len(rows) < 10: continue
        for row in rows:
            code = row.get('code', '')
            if not is_mainboard(code): continue
            reason = row.get('reason', '') or ''
            tags = [t.strip() for t in reason.split('+') if t.strip()]
            if code not in hot_stocks:
                hot_stocks[code] = {'name': row.get('name', ''), 'tags': [], 'count': 0, 'total_gain': 0, 'dates': []}
            hot_stocks[code]['count'] += 1
            hot_stocks[code]['dates'].append(ds)
            hot_stocks[code]['total_gain'] += float(row.get('zhangfu', 0) or 0)
            for t in tags:
                if t not in hot_stocks[code]['tags']:
                    hot_stocks[code]['tags'].append(t)
        print(f'  {ds}: {len(rows)} hot stocks, {len([r for r in rows if is_mainboard(r.get("code",""))])} main-board')
    except: pass
    time.sleep(0.3)

persistent = {c: s for c, s in hot_stocks.items() if s['count'] >= 2}
print(f'\nMain-board persistent hot stocks (>=2 days): {len(persistent)}')

# ============ STEP 2: Sector leaders ============
print('\n' + '='*60)
print('STEP 2: Sector leaders from growth industries')
print('='*60)

key_sectors = [
    ('BK0448', '通信设备'), ('BK1036', '半导体'), ('BK1031', '光伏设备'),
    ('BK1032', '风电设备'), ('BK1204', '国防军工'), ('BK0739', '工程机械'),
    ('BK1038', '光学光电子'), ('BK1039', '电子化学品'), ('BK0459', '元件'),
    ('BK0546', '玻璃玻纤'), ('BK1200', '电力设备'), ('BK0481', '汽车零部件'),
    ('BK0545', '通用设备'), ('BK0735', '计算机设备'), ('BK1211', '汽车'),
    ('BK1033', '电池'), ('BK0910', '专用设备'),
]

sector_map = {}
for bk_code, bk_name in key_sectors:
    try:
        url = 'https://push2.eastmoney.com/api/qt/clist/get'
        params = {'pn':'1','pz':'20','po':'1','np':'1','fltt':'2','invt':'2',
                  'fs': f'b:{bk_code}', 'fields': 'f2,f3,f12,f14,f20,f62'}
        r = em_get(url, params=params)
        d = r.json()
        items = d.get('data', {}).get('diff', []) or []
        cnt = 0
        for it in items[:8]:
            code = it.get('f12', '')
            if not is_mainboard(code): continue
            sector_map[code] = {'name': it.get('f14', ''), 'sector': bk_name,
                              'chg_pct': it.get('f3', 0), 'mcap': it.get('f20', 0),
                              'main_flow': it.get('f62', 0)}
            cnt += 1
        print(f'  {bk_name}: {cnt} main-board leaders')
    except Exception as e:
        print(f'  {bk_name}: ERROR - {str(e)[:50]}')
    time.sleep(0.3)

print(f'Total sector leaders: {len(sector_map)}')

# ============ STEP 3: Tencent quotes ============
print('\n' + '='*60)
print('STEP 3: Fetching Tencent quotes')
print('='*60)

all_codes = list(set(list(persistent.keys()) + list(sector_map.keys())))
print(f'Unique candidates: {len(all_codes)}')

quotes_data = {}
for batch_start in range(0, len(all_codes), 80):
    batch = all_codes[batch_start:batch_start+80]
    prefixed = []
    for c in batch:
        prefixed.append(f'sh{c}' if c.startswith(('6','9')) else f'sz{c}')
    url = 'https://qt.gtimg.cn/q=' + ','.join(prefixed)
    req = urllib.request.Request(url); req.add_header('User-Agent', UA)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read().decode('gbk')
        for line in data.strip().split(';'):
            if '=' not in line or '"' not in line: continue
            key = line.split('=')[0].split('_')[-1]
            vals = line.split('"')[1].split('~')
            if len(vals) < 53: continue
            code = key[2:]
            quotes_data[code] = {
                'name': vals[1], 'price': float(vals[3]) if vals[3] else 0,
                'last_close': float(vals[4]) if vals[4] else 0,
                'change_pct': float(vals[32]) if vals[32] else 0,
                'pe_ttm': float(vals[39]) if vals[39] else 0,
                'pb': float(vals[46]) if vals[46] else 0,
                'mcap_yi': float(vals[44]) if vals[44] else 0,
                'amount_wan': float(vals[37]) if vals[37] else 0,
                'turnover_pct': float(vals[38]) if vals[38] else 0,
                'vol_ratio': float(vals[49]) if vals[49] else 0,
                'pe_static': float(vals[52]) if vals[52] else 0,
            }
    except: pass
    time.sleep(0.5)
    print(f'  Batch {batch_start//80+1}: got {len(quotes_data)} total')

# ============ STEP 4: K-line technical screening ============
print('\n' + '='*60)
print('STEP 4: K-line technical screening')
print('='*60)

def fetch_kline(code):
    tc = f'sh{code}' if code.startswith(('6','9')) else f'sz{code}'
    try:
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        params = {'param': f'{tc},day,,,60,qfq'}
        r = requests.get(url, params=params, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=8)
        d = r.json()
        raw = d.get('data', {}).get(tc, {}).get('qfqday', []) or d.get('data', {}).get(tc, {}).get('day', [])
        return raw
    except: return None

def score_stock(code, quote):
    klines = fetch_kline(code)
    if not klines or len(klines) < 30:
        return 0, [], {}

    closes = [float(k[2]) for k in klines]
    highs = [float(k[3]) for k in klines]
    lows = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    current = closes[-1]

    ma5 = sum(closes[-5:])/5; ma10 = sum(closes[-10:])/10
    ma20 = sum(closes[-20:])/20
    ma60 = sum(closes[-60:])/60 if len(closes)>=60 else ma20

    pct_ma20 = (current-ma20)/ma20*100
    pct_ma60 = (current-ma60)/ma60*100
    vol_avg20 = sum(volumes[-20:])/20
    vol_recent = sum(volumes[-5:])/5
    vol_ratio = vol_recent/vol_avg20 if vol_avg20>0 else 1
    chg_20d = (current-closes[-21])/closes[-21]*100 if len(closes)>21 else 0
    high_10 = max(highs[-10:]); dd_10d = (current-high_10)/high_10*100

    score = 0; reasons = []

    # 1. Trend alignment (most important for early detection)
    if current > ma20 > ma60: score += 20; reasons.append('多头排列')
    elif current > ma20: score += 12; reasons.append('站上MA20')
    if ma5 > ma10 > ma20: score += 12; reasons.append('均线发散向上')
    elif ma5 > ma20: score += 6

    # 2. Early stage check (not too extended - KEY for "前期发现")
    if 0 < pct_ma60 < 25: score += 20; reasons.append(f'距MA60仅{pct_ma60:.0f}%(极早期)')
    elif 25 <= pct_ma60 < 45: score += 12; reasons.append(f'距MA60={pct_ma60:.0f}%(早期)')
    elif 45 <= pct_ma60 < 70: score += 5; reasons.append(f'中期阶段')
    elif pct_ma60 >= 100: score -= 8; reasons.append('已远离MA60(高位)')

    # 3. Recent breakout confirmation
    high_20 = max(highs[-20:])
    if current >= high_20*0.97: score += 10; reasons.append('近期突破新高')

    # 4. Volume: moderate expansion (early stage: not too hot, not too cold)
    if 1.2 < vol_ratio < 2.5: score += 15; reasons.append(f'温和放量({vol_ratio:.1f}x)')
    elif 1.0 < vol_ratio <= 1.2: score += 8
    elif vol_ratio >= 2.5: score += 3; reasons.append('量能过大')

    # 5. Momentum: positive but controlled
    if 5 < chg_20d < 25: score += 12; reasons.append(f'20日涨{chg_20d:.0f}%(稳健)')
    elif 25 <= chg_20d < 50: score += 6; reasons.append(f'20日涨{chg_20d:.0f}%(加速)')

    # 6. Low drawdown = accumulation
    if dd_10d > -6: score += 8; reasons.append('回撤控制好')

    # 7. PE reasonable
    pe = quote.get('pe_ttm', 0)
    if 0 < pe < 40: score += 10; reasons.append(f'PE={pe:.0f}x(低估)')
    elif 40 <= pe < 80: score += 5; reasons.append(f'PE={pe:.0f}x(合理)')

    metrics = {'pct_ma20': pct_ma20, 'pct_ma60': pct_ma60, 'vol_ratio': vol_ratio,
               'chg_20d': chg_20d, 'dd_10d': dd_10d, 'ma5': ma5, 'ma20': ma20, 'ma60': ma60}
    return score, reasons, metrics

candidates = []
for code in list(quotes_data.keys()):
    q = quotes_data[code]
    score, reasons, metrics = score_stock(code, q)
    if score < 25: continue  # Filter weak signals

    tags = persistent.get(code, {}).get('tags', [])
    sector = sector_map.get(code, {}).get('sector', '')

    candidates.append({
        'code': code, 'name': q['name'], 'price': q['price'],
        'pe_ttm': q['pe_ttm'], 'pb': q['pb'], 'mcap_yi': q['mcap_yi'],
        'change_pct': q['change_pct'], 'turnover_pct': q['turnover_pct'],
        'score': score, 'reasons': reasons, 'metrics': metrics,
        'tags': tags, 'sector': sector, 'hot_count': persistent.get(code, {}).get('count', 0),
    })
    print(f'  {code} {q["name"]:<8} score={score:>3} {" | ".join(reasons[:3])}')

candidates.sort(key=lambda x: x['score'], reverse=True)

# Save
with open(os.path.join(DATA_DIR, 'candidates_scored.csv'), 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(['code','name','price','pe_ttm','pb','mcap_yi','change_pct','turnover',
                'score','pct_ma20','pct_ma60','vol_ratio','chg_20d','dd_10d',
                'reasons','tags','sector','hot_count'])
    for c in candidates:
        m = c['metrics']
        w.writerow([c['code'],c['name'],f'{c["price"]:.2f}',f'{c["pe_ttm"]:.1f}',
                   f'{c["pb"]:.1f}',f'{c["mcap_yi"]:.0f}',f'{c["change_pct"]:.1f}',
                   f'{c["turnover_pct"]:.1f}',c['score'],
                   f'{m["pct_ma20"]:.1f}',f'{m["pct_ma60"]:.1f}',
                   f'{m["vol_ratio"]:.1f}',f'{m["chg_20d"]:.1f}',f'{m["dd_10d"]:.1f}',
                   '|'.join(c['reasons']),'|'.join(c['tags']),c['sector'],c['hot_count']])

# Also save K-line data for top 30
print('\n' + '='*60)
print('STEP 5: Saving K-line data for top 30')
print('='*60)
for c in candidates[:30]:
    klines = fetch_kline(c['code'])
    if klines:
        kp = os.path.join(DATA_DIR, f'kline_{c["code"]}_{c["name"]}.csv')
        with open(kp, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['date','open','close','high','low','volume'])
            for k in klines:
                w.writerow(k[:6])
        print(f'  {c["code"]} {c["name"]}: {len(klines)} bars')
    time.sleep(0.2)

print(f'\nTop 30:')
for i, c in enumerate(candidates[:30], 1):
    m = c['metrics']
    tags_str = ','.join(c['tags'][:3]) if c['tags'] else c.get('sector','')
    print(f'{i:2d}. {c["code"]} {c["name"]:<8} {c["price"]:>8.1f} PE={c["pe_ttm"]:>5.0f} S={c["score"]:>3} '
          f'{m["chg_20d"]:>+5.0f}% | {tags_str} | {" | ".join(c["reasons"][:2])}')

print(f'\nTotal: {len(candidates)} candidates scored >= 25')
print(f'Data saved to: {DATA_DIR}')
