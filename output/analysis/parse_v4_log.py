#!/usr/bin/env python3
"""Parse v4 log, check authenticity, generate full trade analysis."""
import re, sys
from collections import defaultdict, Counter

LOG = 'C:/MyW/QMT-Expert/Log/log20260806-4'
with open(LOG, 'r', encoding='utf-8') as f:
    content = f.read()

# ── Parse QMT transaction records (tab-separated) ──
txn_lines = []
strat_lines = []
for line in content.split('\n'):
    if line.startswith('【'):  # timestamp prefix = strategy log
        strat_lines.append(line)
    elif '\t' in line and '操作类型' in line:
        pass  # header
    elif '\t' in line and len(line.split('\t')) >= 14:
        txn_lines.append(line)

# ── Parse strategy log ──
buy_pat = r">>> \[买入\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 金额(\d+) \| alpha144=([\d.e+\-]+)"
sell_pat = r"<<< \[卖出\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 盈亏([+\-][\d.]+)% \| 持有(\d+)天 \| (.+)"
stop_pat = r"\[止损触发\] (\S+)\(([^|]+)\|([^)]+)\) 浮亏-([\d.]+)%"
filter_pat = r"\[过滤\] 黑名单=(\d+) 冷却=(\d+) 跳空=(\d+) ATR=(\d+) 动量=(\d+) MA斜率=(\d+) 趋势=(\d+) 缩量=(\d+)"
init_params = r"突破=(\d+)天\+([\d.]+)%强度.*ATR上限=(\d+)%.*首(\d+)天紧止损(\d+)%"

strat_text = '\n'.join(strat_lines)
buys = []; sells = []; stops = []; filters = []

for line in content.split('\n'):
    m = re.search(buy_pat, line)
    if m:
        c,n,s,sh,p,am,f = m.groups()
        buys.append({'code':c,'name':n,'sector':s,'shares':int(sh),'price':float(p),'amount':int(am),'factor':float(f)})
        continue
    m = re.search(sell_pat, line)
    if m:
        c,n,s,sh,p,pnl,d,r = m.groups()
        sells.append({'code':c,'name':n,'sector':s,'shares':int(sh),'price':float(p),'pnl_pct':float(pnl),'days':int(d),'reason':r})
        continue
    m = re.search(stop_pat, line)
    if m:
        c,n,s,l = m.groups()
        stops.append({'code':c,'name':n,'sector':s,'loss':float(l)})
        continue
    m = re.search(filter_pat, line)
    if m:
        filters.append(dict(zip(['bl','cd','gap','atr','mom','slope','trend','vol'],[int(x) for x in m.groups()])))

print("=" * 70)
print("=== AUTHENTICITY CHECK ===")
print(f"Strategy buys: {len(buys)}, sells: {len(sells)}, stops: {len(stops)}")
print(f"QMT txn records: {len(txn_lines)}")

# Check buy-sell inventory = any buys unmatched?
buy_codes = Counter(b['code'] for b in buys)
sell_codes = Counter(s['code'] for s in sells)
unmatched = []
for code, cnt in buy_codes.items():
    if sell_codes.get(code, 0) < cnt:
        unmatched.append((code, cnt - sell_codes.get(code, 0), 'missing sells'))
for code, cnt in sell_codes.items():
    if buy_codes.get(code, 0) < cnt:
        unmatched.append((code, cnt - buy_codes.get(code, 0), 'missing buys'))
if unmatched:
    print(f"\n⚠ INVENTORY MISMATCH ({len(unmatched)} codes):")
    for c, diff, tag in unmatched[:15]:
        name = buys[[b['code'] for b in buys].index(c)]['name'] if c in buy_codes else ''
        print(f"  {c}({name}): {diff} {tag}")
else:
    print("✅ Buy/sell inventory balanced (no extra sells without buys)")

# Check QMT txn consistency
txn_buys = [l for l in txn_lines if '买入' in l.split('\t')[-1]]
txn_sells = [l for l in txn_lines if '卖出' in l.split('\t')[-1]]
print(f"QMT buy records: {len(txn_buys)}, sell records: {len(txn_sells)}")

# Final asset check
asset_lines = [l for l in strat_lines if '资产=' in l and '摘要' in l]
if asset_lines:
    for l in asset_lines[:2]: print(f"  Start: {l.strip()[:120]}")
    for l in asset_lines[-2:]: print(f"  End:   {l.strip()[:120]}")

# Cross-check key numbers
print(f"\n=== CONSISTENCY CROSS-CHECK ===")
print(f"Strat buys={len(buys)} vs QMT buy txs={len(txn_buys)}")
print(f"Strat sells={len(sells)} vs QMT sell txs={len(txn_sells)}")
# Count "买入" in strategy log
buy_lines_in_strat = sum(1 for l in strat_lines if '>>> [买入]' in l)
sell_lines_in_strat = sum(1 for l in strat_lines if '<<< [卖出]' in l)
print(f"Buy lines in strat={buy_lines_in_strat}, Sell lines in strat={sell_lines_in_strat}")

# Check for duplicate trade issues
print(f"\n=== REBUY FREQUENCY ===")
buy_freq = Counter(b['code'] for b in buys)
multi = {c:n for c,n in buy_freq.items() if n >= 3}
print(f"Stocks bought >=3 times: {len(multi)}")
top_rebuy = sorted(multi.items(), key=lambda x: x[1], reverse=True)[:10]
for c, cnt in top_rebuy:
    name = [b['name'] for b in buys if b['code']==c][0]
    pnls = [s['pnl_pct'] for s in sells if s['code']==c]
    print(f"  {c}({name}): bought {cnt}x, avg pnl={sum(pnls)/len(pnls):+.1f}%" if pnls else f"  {c}({name}): bought {cnt}x, no sells yet")

print()
print("=" * 70)
print("=== BY REASON ===")
reason_stats = defaultdict(lambda: {'c':0,'pnl':0.0,'d':0})
for s in sells:
    if '大盘防御' in s['reason']: rk = '大盘防御'
    elif '紧止损' in s['reason']: rk = '紧止损(-12%)'
    elif '标准止损' in s['reason']: rk = '标准止损(-18%)'
    elif '止损' in s['reason']: rk = '止损(通用)'
    elif '到期' in s['reason']: rk = '持有到期'
    elif '补卖' in s['reason']: rk = '补卖'
    else: rk = s['reason']
    reason_stats[rk]['c'] += 1
    reason_stats[rk]['pnl'] += s['pnl_pct']
    reason_stats[rk]['d'] += s['days']
for r, st in sorted(reason_stats.items(), key=lambda x: x[1]['c'], reverse=True):
    print(f"  {r:20s}: {st['c']:4d}笔, 均盈亏{st['pnl']/st['c']:+7.1f}%, 均持有{st['d']/st['c']:5.0f}天")

print()
print("=" * 70)
print("=== BY SECTOR ===")
sec_st = defaultdict(lambda: {'c':0,'pnl':0.0,'d':0,'stops':0,'w':0})
for s in sells:
    sec = s['sector']
    sec_st[sec]['c'] += 1; sec_st[sec]['pnl'] += s['pnl_pct']; sec_st[sec]['d'] += s['days']
    if s['pnl_pct'] > 0: sec_st[sec]['w'] += 1
for sl in stops: sec_st[sl['sector']]['stops'] += 1
for sec, st in sorted(sec_st.items(), key=lambda x: x[1]['c'], reverse=True):
    c=st['c']; print(f"  {sec:12s}: {c:3d}笔, 均盈亏{st['pnl']/c:+6.1f}%, 胜率{st['w']/c*100:4.0f}%, 均持{st['d']/c:4.0f}天, 止损{st['stops']}次")

print()
print("=" * 70)
print("=== OVERALL ===")
tp = sum(s['pnl_pct'] for s in sells); ap = tp/len(sells)
wins = [s for s in sells if s['pnl_pct']>0]; losses = [s for s in sells if s['pnl_pct']<=0]
wr = len(wins)/len(sells)*100
aw = sum(w['pnl_pct'] for w in wins)/len(wins) if wins else 0
al = sum(l['pnl_pct'] for l in losses)/len(losses) if losses else 0
ad = sum(s['days'] for s in sells)/len(sells)
plr = abs(aw/al) if al != 0 else 0
print(f"  交易: {len(sells)}笔  胜率: {wr:.1f}%({len(wins)}W/{len(losses)}L)")
print(f"  均盈: {aw:+.1f}%  均亏: {al:+.1f}%  盈亏比: {plr:.2f}")
print(f"  均持: {ad:.1f}天  总盈亏和: {tp:+.1f}%")

print()
print("=" * 70)
print("=== BY HOLD DAYS ===")
for lo,hi,label in [(1,1,'1天'),(2,3,'2-3天'),(4,7,'4-7天'),(8,14,'8-14天'),(15,20,'15-20天'),(21,99,'21+')]:
    b = [s for s in sells if lo<=s['days']<=hi]
    if b:
        a=sum(s['pnl_pct'] for s in b)/len(b); w=len([s for s in b if s['pnl_pct']>0])/len(b)*100
        print(f"  {label:10s}: {len(b):3d}笔({len(b)/len(sells)*100:4.0f}%), 均盈亏{a:+6.1f}%, 胜率{w:4.0f}%")

# Early stop effectiveness
print()
print("=" * 70)
print("=== EARLY STOP EFFECTIVENESS ===")
early_stops = [s for s in sells if '紧止损' in s['reason']]
late_stops = [s for s in sells if '标准止损' in s['reason'] or ('硬止损' in s['reason'] and '紧' not in s['reason'])]
if early_stops:
    print(f"  紧止损(-12%): {len(early_stops)}笔, 均亏{sum(s['pnl_pct'] for s in early_stops)/len(early_stops):.1f}%")
if late_stops:
    print(f"  标准止损(-18%): {len(late_stops)}笔, 均亏{sum(s['pnl_pct'] for s in late_stops)/len(late_stops):.1f}%")

# Filter stats
print()
print("=" * 70)
print("=== FILTER EFFECTIVENESS ===")
if filters:
    for k in ['atr','mom','slope','trend','vol','bl','cd','gap']:
        vals = [f[k] for f in filters]
        print(f"  {k:6s}: 均{sum(vals)/len(vals):5.1f}次, 总={sum(vals)}")

# Final assets
print()
print("=" * 70)
print("=== FINAL STATE ===")
last_summaries = [l for l in strat_lines if '摘要' in l][-3:]
for l in last_summaries: print(f"  {l.strip()[:150]}")

# Empty periods
empty = [l for l in strat_lines if '空仓' in l and '摘要' in l]
total_bars = len([l for l in strat_lines if '摘要' in l])
print(f"\n  空仓日: {len(empty)}/{total_bars} = {len(empty)/total_bars*100:.1f}%")

# Top/Bottom trades
print()
print("=" * 70)
print("=== WORST 15 ===")
for s in sorted(sells, key=lambda x: x['pnl_pct'])[:15]:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持{s['days']:2d}d {s['reason'][:30]}")
print()
print("=== BEST 15 ===")
for s in sorted(sells, key=lambda x: x['pnl_pct'], reverse=True)[:15]:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持{s['days']:2d}d {s['reason'][:30]}")

# Final asset value check
last = asset_lines[-1] if asset_lines else ''
import re as re2
m = re2.search(r'资产=(\d+)万', last)
if m:
    final = int(m.group(1))
    print(f"\n  终点资产: {final}万 (起始30万) → +{final-30}万 ({(final-30)/30*100:+.1f}%)")
