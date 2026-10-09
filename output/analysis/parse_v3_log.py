#!/usr/bin/env python3
"""Parse QMT v3 backtest log and generate trade analysis with v2 comparison."""
import re
import sys
from collections import defaultdict

LOG_PATH = 'C:/MyW/QMT-Expert/Log/log20260806-3'

with open(LOG_PATH, 'r', encoding='utf-8') as f:
    content = f.read()

buys = []
sells = []
stop_losses = []
signals = []
filter_lines = []

buy_pat = r">>> \[买入\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 金额(\d+) \| alpha144=([\d.e+\-]+)"
sell_pat = r"<<< \[卖出\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 盈亏([+\-][\d.]+)% \| 持有(\d+)天 \| (.+)"
stop_pat = r"\[止损触发\] (\S+)\(([^|]+)\|([^)]+)\) 浮亏-([\d.]+)%"
signal_pat = r"\[信号\] (\S+)\(([^|]+)\|([^)]+)\) 突破(\d+)日新高"
filter_pat = r"\[过滤\] 黑名单=(\d+) 冷却=(\d+) 跳空=(\d+) 趋势=(\d+) 缩量=(\d+)"

for line in content.split('\n'):
    m = re.search(buy_pat, line)
    if m:
        code, name, sector, shares, price, amount, factor = m.groups()
        buys.append({'code': code, 'name': name, 'sector': sector,
                     'shares': int(shares), 'price': float(price),
                     'amount': int(amount), 'factor': float(factor)})
        continue
    m = re.search(sell_pat, line)
    if m:
        code, name, sector, shares, price, pnl, days, reason = m.groups()
        sells.append({'code': code, 'name': name, 'sector': sector,
                     'shares': int(shares), 'price': float(price),
                     'pnl_pct': float(pnl), 'days': int(days), 'reason': reason})
        continue
    m = re.search(stop_pat, line)
    if m:
        code, name, sector, loss = m.groups()
        stop_losses.append({'code': code, 'name': name, 'sector': sector, 'loss': float(loss)})
        continue
    m = re.search(signal_pat, line)
    if m:
        code, name, sector, period = m.groups()
        signals.append({'code': code, 'name': name, 'sector': sector, 'period': int(period)})
        continue
    m = re.search(filter_pat, line)
    if m:
        bl, cd, gap, trend, vol = m.groups()
        filter_lines.append({'blacklist': int(bl), 'cooldown': int(cd),
                            'gap': int(gap), 'trend': int(trend), 'volume': int(vol)})

print(f"Total buys: {len(buys)} (v2: 1036)")
print(f"Total sells: {len(sells)} (v2: 1008)")
print(f"Total signals: {len(signals)} (v2: 1058)")
print(f"Total stop losses: {len(stop_losses)} (v2: 397)")
print()

# === Filter effectiveness ===
total_filtered = sum(f['blacklist'] + f['cooldown'] + f['gap'] + f['trend'] + f['volume']
                     for f in filter_lines)
print(f"=== 过滤器效果 ===")
print(f"过滤事件行数: {len(filter_lines)}")
if filter_lines:
    avg_bl = sum(f['blacklist'] for f in filter_lines) / len(filter_lines)
    avg_cd = sum(f['cooldown'] for f in filter_lines) / len(filter_lines)
    avg_gap = sum(f['gap'] for f in filter_lines) / len(filter_lines)
    avg_trend = sum(f['trend'] for f in filter_lines) / len(filter_lines)
    avg_vol = sum(f['volume'] for f in filter_lines) / len(filter_lines)
    print(f"  均黑名单: {avg_bl:.1f}  均冷却: {avg_cd:.1f}  均跳空: {avg_gap:.1f}  均趋势: {avg_trend:.1f}  均缩量: {avg_vol:.1f}")
    print(f"  趋势过滤占比: {sum(f['trend'] for f in filter_lines)/total_filtered*100:.0f}%")

# === By reason ===
print()
print("=" * 70)
print("=== 按卖出原因分析 ===")
reason_stats = defaultdict(lambda: {'count': 0, 'total_pnl': 0.0, 'total_days': 0})
for s in sells:
    if '大盘防御' in s['reason']: rkey = '大盘防御'
    elif '硬止损' in s['reason']: rkey = '硬止损'
    elif '到期' in s['reason']: rkey = '持有到期'
    elif '补卖' in s['reason']: rkey = '补卖(昨日跌停)'
    else: rkey = s['reason']
    reason_stats[rkey]['count'] += 1
    reason_stats[rkey]['total_pnl'] += s['pnl_pct']
    reason_stats[rkey]['total_days'] += s['days']

for reason, stats in sorted(reason_stats.items(), key=lambda x: x[1]['count'], reverse=True):
    cnt = stats['count']
    avg_pnl = stats['total_pnl'] / cnt if cnt > 0 else 0
    avg_days = stats['total_days'] / cnt if cnt > 0 else 0
    print(f"  {reason:20s}: {cnt:4d}笔, 均盈亏{avg_pnl:+7.1f}%, 均持有{avg_days:5.0f}天")

# === By sector ===
print()
print("=" * 70)
print("=== 按行业分析 ===")
sector_stats = defaultdict(lambda: {'c': 0, 'pnl': 0.0, 'days': 0, 'stops': 0, 'w': 0})
for s in sells:
    sec = s['sector']
    sector_stats[sec]['c'] += 1
    sector_stats[sec]['pnl'] += s['pnl_pct']
    sector_stats[sec]['days'] += s['days']
    if s['pnl_pct'] > 0: sector_stats[sec]['w'] += 1
for sl in stop_losses:
    sector_stats[sl['sector']]['stops'] += 1

for sec, st in sorted(sector_stats.items(), key=lambda x: x[1]['c'], reverse=True):
    c = st['c']; avg_p = st['pnl']/c if c else 0; avg_d = st['days']/c if c else 0
    wr = st['w']/c*100 if c else 0
    print(f"  {sec:12s}: {c:3d}笔, 均盈亏{avg_p:+6.1f}%, 胜率{wr:4.0f}%, 均持有{avg_d:4.0f}天, 止损{st['stops']}次")

# === Overall ===
print()
print("=" * 70)
print("=== 整体统计 (v3 vs v2) ===")
total_pnl = sum(s['pnl_pct'] for s in sells)
avg_pnl = total_pnl / len(sells) if sells else 0
wins = [s for s in sells if s['pnl_pct'] > 0]
losses = [s for s in sells if s['pnl_pct'] <= 0]
win_rate = len(wins)/len(sells)*100 if sells else 0
avg_win = sum(w['pnl_pct'] for w in wins)/len(wins) if wins else 0
avg_loss = sum(l['pnl_pct'] for l in losses)/len(losses) if losses else 0
avg_days = sum(s['days'] for s in sells)/len(sells) if sells else 0

print(f"  交易笔数: {len(sells):5d}  (v2: 1008)")
print(f"  胜率:     {win_rate:5.1f}% (v2: 36.5%)")
print(f"  平均盈利: {avg_win:+5.1f}% (v2: +72.0%)")
print(f"  平均亏损: {avg_loss:+5.1f}% (v2: -26.2%)")
pl_ratio = abs(avg_win/avg_loss) if avg_loss != 0 else 0
print(f"  盈亏比:   {pl_ratio:5.2f}  (v2: 2.75)")
print(f"  均持天数: {avg_days:5.1f}  (v2: 6.2)")
print(f"  总盈亏和: {total_pnl:+5.1f}%")

# === By holding days bucket ===
print()
print("=" * 70)
print("=== 按持有天数区间分析 ===")
for lo, hi, label in [(1,1,'1天'), (2,3,'2-3天'), (4,7,'4-7天'), (8,14,'8-14天'), (15,20,'15-20天'), (21,99,'21+天')]:
    bucket = [s for s in sells if lo <= s['days'] <= hi]
    if bucket:
        avg = sum(s['pnl_pct'] for s in bucket)/len(bucket)
        wr = len([s for s in bucket if s['pnl_pct'] > 0])/len(bucket)*100
        pct = len(bucket)/len(sells)*100
        print(f"  {label:10s}: {len(bucket):3d}笔({pct:4.0f}%), 均盈亏{avg:+6.1f}%, 胜率{wr:4.0f}%")

# === By specific days ===
print()
print("=" * 70)
print("=== 按具体持有天数分析 ===")
for days in sorted(set(s['days'] for s in sells)):
    day_sells = [s for s in sells if s['days'] == days]
    avg = sum(s['pnl_pct'] for s in day_sells) / len(day_sells)
    wins_day = len([s for s in day_sells if s['pnl_pct'] > 0])
    print(f"  持有{days:2d}天: {len(day_sells):3d}笔, 均盈亏{avg:+6.1f}%, 胜率{wins_day/len(day_sells)*100:4.0f}%")

# === Market filter analysis ===
print()
print("=" * 70)
print("=== 大盘过滤器分析 ===")
defense = [s for s in sells if '大盘防御' in s['reason']]
non_defense = [s for s in sells if '大盘防御' not in s['reason']]
if defense:
    da = sum(s['pnl_pct'] for s in defense)/len(defense)
    dw = len([s for s in defense if s['pnl_pct']>0])/len(defense)*100
    dd = sum(s['days'] for s in defense)/len(defense)
    print(f"  大盘防御清仓: {len(defense)}笔, 均盈亏{da:+.1f}%, 胜率{dw:.0f}%, 均持有{dd:.0f}天")
if non_defense:
    na = sum(s['pnl_pct'] for s in non_defense)/len(non_defense)
    nw = len([s for s in non_defense if s['pnl_pct']>0])/len(non_defense)*100
    nd = sum(s['days'] for s in non_defense)/len(non_defense)
    print(f"  正常到期/止损: {len(non_defense)}笔, 均盈亏{na:+.1f}%, 胜率{nw:.0f}%, 均持有{nd:.0f}天")

# === Alpha144 factor vs PnL ===
print()
print("=" * 70)
print("=== Alpha144因子与盈亏关系 ===")
matched = []
for i, s in enumerate(sells):
    if i < len(buys):
        b = buys[i]
        matched.append((b['factor'], s['pnl_pct'], s['code'], s['name'], s['sector'], s['days']))
if matched:
    matched.sort(key=lambda x: x[0])
    n = len(matched)
    for label, group in [
        ('低因子(T1)', matched[:n//3]),
        ('中因子(T2)', matched[n//3:2*n//3]),
        ('高因子(T3)', matched[2*n//3:])
    ]:
        if group:
            avg_f = sum(x[0] for x in group)/len(group)
            avg_p = sum(x[1] for x in group)/len(group)
            wr = len([x for x in group if x[1]>0])/len(group)*100
            print(f"  {label}: {len(group)}笔, 均因子{avg_f:.2e}, 均盈亏{avg_p:+.1f}%, 胜率{wr:.0f}%")

# === Price range ===
print()
print("=" * 70)
print("=== 买入价格区间分析 ===")
for lo, hi, label in [(0,5,'0-5元'),(5,15,'5-15元'),(15,30,'15-30元'),(30,100,'30-100元'),(100,9999,'100+元')]:
    bucket = [(b, matched[i]) for i, b in enumerate(buys)
              if lo <= b['price'] < hi and i < len(matched)]
    if bucket:
        avg_p = sum(x[1][1] for x in bucket)/len(bucket)
        wr = len([x for x in bucket if x[1][1]>0])/len(bucket)*100
        stops_b = sum(1 for x in bucket if '硬止损' in matched[buys.index(x[0])][0] if buys.index(x[0]) < len(matched))
        print(f"  {label:12s}: {len(bucket)}笔, 均盈亏{avg_p:+6.1f}%, 胜率{wr:4.0f}%")

# === Worst trades ===
print()
print("=" * 70)
print("=== 最大亏损 Top 15 ===")
for s in sorted(sells, key=lambda x: x['pnl_pct'])[:15]:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持有{s['days']:2d}天 {s['reason']}")

# === Best trades ===
print()
print("=" * 70)
print("=== 最大盈利 Top 15 ===")
for s in sorted(sells, key=lambda x: x['pnl_pct'], reverse=True)[:15]:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持有{s['days']:2d}天 {s['reason']}")

# === Stop loss gap analysis ===
print()
print("=" * 70)
print("=== 止损滑点分析 ===")
actual_stops = [s for s in sells if '硬止损' in s['reason']]
print(f"  止损触发: {len(stop_losses)}次, 实际执行: {len(actual_stops)}笔")
if actual_stops:
    avg = sum(s['pnl_pct'] for s in actual_stops)/len(actual_stops)
    print(f"  平均止损亏损: {avg:.1f}% (触发线: -18%)")
    print(f"  平均滑点: {abs(avg)-18:.1f}%")
    # Worst offenders
    print(f"  止亏损>50%的次数: {len([s for s in actual_stops if s['pnl_pct'] < -50])}")
    print(f"  止亏损>70%的次数: {len([s for s in actual_stops if s['pnl_pct'] < -70])}")

# === Rebuy analysis ===
print()
print("=" * 70)
print("=== 重复买入分析（同一股票被买入多次）===")
from collections import Counter
buy_counts = Counter(b['code'] for b in buys)
rebuy = {c: n for c, n in buy_counts.items() if n >= 3}
if rebuy:
    print(f"  买入>=3次的股票: {len(rebuy)}只")
    for code, cnt in sorted(rebuy.items(), key=lambda x: x[1], reverse=True)[:10]:
        name = buys[[b['code'] for b in buys].index(code)]['name']
        # Find PnL for this stock
        pnls = [s['pnl_pct'] for s in sells if s['code'] == code]
        avg_p = sum(pnls)/len(pnls) if pnls else 0
        stops = [s for s in actual_stops if s['code'] == code]
        print(f"    {code}({name}): {cnt}次买入, {len(pnls)}次卖出, 均盈亏{avg_p:+.1f}%, 止损{len(stops)}次")
else:
    print("  无重复买入>=3次的股票")

# === Final asset and date range ===
print()
print("=" * 70)
print("=== 回测时间与资产 ===")
asset_lines = [l for l in content.split('\n') if '资产=' in l and '摘要' in l]
if asset_lines:
    for l in asset_lines[:3]:
        print(f"  {l.strip()[:130]}")
    print("  ...")
    for l in asset_lines[-3:]:
        print(f"  {l.strip()[:130]}")
    import re as re2
    last = asset_lines[-1]
    m = re2.search(r'资产=(\d+)万', last)
    if m:
        final = int(m.group(1))
        ret = (final - 30) / 30 * 100
        print(f"\n  终点资产: {final}万 (起始30万), 总收益: {final-30:+d}万 ({ret:+.1f}%)")

# Date range
date_lines = [l for l in content.split('\n') if '202' in l and 'bar=' in l and '摘要' not in l and '资产' not in l]
if date_lines:
    first = date_lines[0].strip()[:80]
    last_d = date_lines[-1].strip()[:80]
    print(f"\n  起始: {first}")
    print(f"  结束: {last_d}")

# === Signal to trade ratio ===
print()
print("=" * 70)
print("=== 信号转化率 ===")
print(f"  信号总数: {len(signals)}")
print(f"  实际买入: {len(buys)}")
print(f"  转化率: {len(buys)/len(signals)*100:.1f}%" if signals else "N/A")

# === Empty periods (market defense) ===
print()
print("=" * 70)
print("=== 空仓期分析 ===")
empty_summaries = [l for l in content.split('\n') if '摘要' in l and '空仓' in l]
print(f"  空仓日: {len(empty_summaries)}")
total_bars = len([l for l in content.split('\n') if '摘要' in l])
if total_bars:
    print(f"  总交易日: {total_bars}, 空仓率: {len(empty_summaries)/total_bars*100:.1f}%")
