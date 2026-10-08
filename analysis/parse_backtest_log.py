#!/usr/bin/env python3
"""Parse QMT backtest log and generate trade analysis."""
import re
from collections import defaultdict

with open('C:/MyW/QMT-Expert/Log/log20260806-2', 'r', encoding='utf-8') as f:
    content = f.read()

buys = []
sells = []
stop_losses = []

buy_pattern = r">>> \[买入\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 金额(\d+) \| alpha144=([\d.e+\-]+)"
sell_pattern = r"<<< \[卖出\] (\S+)\(([^|]+)\|([^)]+)\) × (\d+)股 @ ([\d.]+) \| 盈亏([+\-][\d.]+)% \| 持有(\d+)天 \| (.+)"
stop_pattern = r"\[止损触发\] (\S+)\(([^|]+)\|([^)]+)\) 浮亏-([\d.]+)%"
signal_pattern = r"\[信号\] (\S+)\(([^|]+)\|([^)]+)\) 突破5日新高!"

all_signals = []

for line in content.split('\n'):
    m = re.search(buy_pattern, line)
    if m:
        code, name, sector, shares, price, amount, factor = m.groups()
        buys.append({
            'code': code, 'name': name, 'sector': sector,
            'shares': int(shares), 'price': float(price), 'amount': int(amount),
            'factor': float(factor)
        })
        continue

    m = re.search(sell_pattern, line)
    if m:
        code, name, sector, shares, price, pnl, days, reason = m.groups()
        sells.append({
            'code': code, 'name': name, 'sector': sector,
            'shares': int(shares), 'price': float(price), 'pnl_pct': float(pnl),
            'days': int(days), 'reason': reason
        })
        continue

    m = re.search(stop_pattern, line)
    if m:
        code, name, sector, loss = m.groups()
        stop_losses.append({
            'code': code, 'name': name, 'sector': sector, 'loss': float(loss)
        })
        continue

    m = re.search(signal_pattern, line)
    if m:
        code, name, sector = m.groups()
        all_signals.append({'code': code, 'name': name, 'sector': sector})

print(f"Total buys: {len(buys)}")
print(f"Total sells: {len(sells)}")
print(f"Total signals: {len(all_signals)}")
print(f"Total stop losses triggered: {len(stop_losses)}")
print()

# === By reason ===
print("=" * 70)
print("=== 按卖出原因分析 ===")
reason_stats = defaultdict(lambda: {'count': 0, 'total_pnl': 0.0, 'total_days': 0})
for s in sells:
    if '大盘防御' in s['reason']:
        rkey = '大盘防御'
    elif '硬止损' in s['reason']:
        rkey = '硬止损'
    elif '到期' in s['reason']:
        rkey = '持有到期'
    elif '补卖' in s['reason']:
        rkey = '补卖(昨日跌停)'
    else:
        rkey = s['reason']
    reason_stats[rkey]['count'] += 1
    reason_stats[rkey]['total_pnl'] += s['pnl_pct']
    reason_stats[rkey]['total_days'] += s['days']

for reason, stats in sorted(reason_stats.items(), key=lambda x: x[1]['count'], reverse=True):
    cnt = stats['count']
    avg_pnl = stats['total_pnl'] / cnt
    avg_days = stats['total_days'] / cnt
    print(f"  {reason:20s}: {cnt:4d}笔, 均盈亏{avg_pnl:+7.1f}%, 均持有{avg_days:5.0f}天")

# === By sector ===
print()
print("=" * 70)
print("=== 按行业分析 ===")
sector_stats = defaultdict(lambda: {'count': 0, 'total_pnl': 0.0, 'total_days': 0, 'stops': 0, 'wins': 0})
for s in sells:
    sec = s['sector']
    sector_stats[sec]['count'] += 1
    sector_stats[sec]['total_pnl'] += s['pnl_pct']
    sector_stats[sec]['total_days'] += s['days']
    if s['pnl_pct'] > 0:
        sector_stats[sec]['wins'] += 1
for sl in stop_losses:
    sector_stats[sl['sector']]['stops'] += 1

for sec, stats in sorted(sector_stats.items(), key=lambda x: x[1]['count'], reverse=True):
    cnt = stats['count']
    avg_pnl = stats['total_pnl'] / cnt if cnt > 0 else 0
    avg_days = stats['total_days'] / cnt if cnt > 0 else 0
    wr = stats['wins'] / cnt * 100 if cnt > 0 else 0
    print(f"  {sec:12s}: {cnt:3d}笔, 均盈亏{avg_pnl:+6.1f}%, 胜率{wr:4.0f}%, 均持有{avg_days:4.0f}天, 止损{stats['stops']}次")

# === Overall ===
print()
print("=" * 70)
print("=== 整体统计 ===")
total_pnl = sum(s['pnl_pct'] for s in sells)
avg_pnl = total_pnl / len(sells)
wins = [s for s in sells if s['pnl_pct'] > 0]
losses = [s for s in sells if s['pnl_pct'] <= 0]
win_rate = len(wins) / len(sells) * 100
avg_win = sum(w['pnl_pct'] for w in wins) / len(wins) if wins else 0
avg_loss = sum(l['pnl_pct'] for l in losses) / len(losses) if losses else 0
avg_days = sum(s['days'] for s in sells) / len(sells)

print(f"  交易笔数: {len(sells)}")
print(f"  胜率: {win_rate:.1f}% ({len(wins)}赢/{len(losses)}亏)")
print(f"  平均盈利: {avg_win:+.1f}%")
print(f"  平均亏损: {avg_loss:+.1f}%")
if avg_loss != 0:
    print(f"  盈亏比: {abs(avg_win/avg_loss):.2f}")
print(f"  平均持有天数: {avg_days:.1f}")
print(f"  总盈亏之和: {total_pnl:+.1f}%")

# === By holding days bucket ===
print()
print("=" * 70)
print("=== 按持有天数区间分析 ===")
for lo, hi, label in [(1, 1, '1天'), (2, 3, '2-3天'), (4, 7, '4-7天'),
                       (8, 14, '8-14天'), (15, 20, '15-20天'), (21, 99, '21+天')]:
    bucket = [s for s in sells if lo <= s['days'] <= hi]
    if bucket:
        avg = sum(s['pnl_pct'] for s in bucket) / len(bucket)
        wr = len([s for s in bucket if s['pnl_pct'] > 0]) / len(bucket) * 100
        print(f"  {label:10s}: {len(bucket):3d}笔, 均盈亏{avg:+6.1f}%, 胜率{wr:4.0f}%")

# === By specific days ===
print()
print("=" * 70)
print("=== 按具体持有天数分析 ===")
for days in sorted(set(s['days'] for s in sells)):
    day_sells = [s for s in sells if s['days'] == days]
    avg = sum(s['pnl_pct'] for s in day_sells) / len(day_sells)
    wins_day = len([s for s in day_sells if s['pnl_pct'] > 0])
    print(f"  持有{days:2d}天: {len(day_sells):3d}笔, 均盈亏{avg:+6.1f}%, 胜率{wins_day/len(day_sells)*100:4.0f}%")

# === Worst trades ===
print()
print("=" * 70)
print("=== 最大亏损 Top 15 ===")
worst = sorted(sells, key=lambda x: x['pnl_pct'])[:15]
for s in worst:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持有{s['days']:2d}天 原因:{s['reason']}")

# === Best trades ===
print()
print("=" * 70)
print("=== 最大盈利 Top 15 ===")
best = sorted(sells, key=lambda x: x['pnl_pct'], reverse=True)[:15]
for s in best:
    print(f"  {s['code']}({s['name']}|{s['sector']}) 盈亏{s['pnl_pct']:+6.1f}% 持有{s['days']:2d}天 原因:{s['reason']}")

# === Stop loss deep dive ===
print()
print("=" * 70)
print("=== 止损详细分析 ===")
for sl in stop_losses:
    print(f"  {sl['code']}({sl['name']}|{sl['sector']}) 浮亏-{sl['loss']:.1f}%")
# Check: how many stops actually executed? (some stops may have been defense liquidated before)
actual_stop_sells = [s for s in sells if '硬止损' in s['reason']]
print(f"\n  止损触发次数: {len(stop_losses)}")
print(f"  实际止损卖出: {len(actual_stop_sells)}笔")
if actual_stop_sells:
    avg_stop = sum(s['pnl_pct'] for s in actual_stop_sells) / len(actual_stop_sells)
    print(f"  止损平均亏损: {avg_stop:.1f}%")
    # Gap analysis: how much worse than -18% was the actual exit?
    for s in actual_stop_sells:
        excess = abs(s['pnl_pct']) - 18
        print(f"    {s['code']}({s['name']}) 止损@{s['pnl_pct']:.1f}%, 超出止损线{excess:.1f}%")

# === Market filter analysis ===
print()
print("=" * 70)
print("=== 大盘过滤器分析 ===")
defense_sells = [s for s in sells if '大盘防御' in s['reason']]
non_defense = [s for s in sells if '大盘防御' not in s['reason']]
if defense_sells:
    d_avg = sum(s['pnl_pct'] for s in defense_sells) / len(defense_sells)
    d_wr = len([s for s in defense_sells if s['pnl_pct'] > 0]) / len(defense_sells) * 100
    print(f"  大盘防御清仓: {len(defense_sells)}笔, 均盈亏{d_avg:+.1f}%, 胜率{d_wr:.0f}%, 均持有{sum(s['days'] for s in defense_sells)/len(defense_sells):.0f}天")
if non_defense:
    n_avg = sum(s['pnl_pct'] for s in non_defense) / len(non_defense)
    n_wr = len([s for s in non_defense if s['pnl_pct'] > 0]) / len(non_defense) * 100
    print(f"  正常到期/止损: {len(non_defense)}笔, 均盈亏{n_avg:+.1f}%, 胜率{n_wr:.0f}%, 均持有{sum(s['days'] for s in non_defense)/len(non_defense):.0f}天")

# === Factor and PnL ===
print()
print("=" * 70)
print("=== Alpha144因子与盈亏关系 ===")
# Match buys with sells approximately in chronological order
from collections import deque
buy_queue = deque(buys)
sell_with_factor = []
pending = {}  # code -> list of buy records

for s in sells:
    code = s['code']
    # Find matching buy - try pending first
    if code in pending and pending[code]:
        b = pending[code].pop(0)
        sell_with_factor.append((b['factor'], s['pnl_pct'], s['code'], s['name'], s['sector']))
    else:
        sell_with_factor.append((None, s['pnl_pct'], s['code'], s['name'], s['sector']))

# Actually let's just use buys in order
# FIFO matching
buy_idx = 0
matched = []
for s in sells:
    if buy_idx < len(buys):
        b = buys[buy_idx]
        matched.append((b['factor'], s['pnl_pct'], s['code'], s['name'], s['sector'], s['days'], s['reason']))
        buy_idx += 1
    else:
        matched.append((None, s['pnl_pct'], s['code'], s['name'], s['sector'], s['days'], s['reason']))

print("  FIFO匹配分析 (买入顺序 ≈ 卖出顺序):")
# Divide into factor terciles
valid = [(f, p) for f, p, *rest in matched if f is not None]
if valid:
    valid.sort(key=lambda x: x[0])
    n = len(valid)
    lo = valid[:n//3]
    mid = valid[n//3:2*n//3]
    hi = valid[2*n//3:]
    for label, group in [('低因子(T1)', lo), ('中因子(T2)', mid), ('高因子(T3)', hi)]:
        avg_f = sum(x[0] for x in group) / len(group)
        avg_p = sum(x[1] for x in group) / len(group)
        wr = len([x for x in group if x[1] > 0]) / len(group) * 100
        print(f"  {label}: {len(group)}笔, 均因子{avg_f:.2e}, 均盈亏{avg_p:+.1f}%, 胜率{wr:.0f}%")

    # Low vs high factor comparison
    print(f"\n  低因子(T1)均盈亏: {sum(x[1] for x in lo)/len(lo):+.1f}%")
    print(f"  高因子(T3)均盈亏: {sum(x[1] for x in hi)/len(hi):+.1f}%")

# === Price range analysis ===
print()
print("=" * 70)
print("=== 买入价格区间分析 ===")
for lo, hi, label in [(0, 5, '0-5元'), (5, 15, '5-15元'), (15, 30, '15-30元'), (30, 100, '30-100元'), (100, 9999, '100+元')]:
    bucket = [(b, m) for b, m in zip(buys, matched) if lo <= b['price'] < hi]
    if bucket:
        avg_p = sum(x[1][1] for x in bucket) / len(bucket)
        wr = len([x for x in bucket if x[1][1] > 0]) / len(bucket) * 100
        stops_bucket = sum(1 for x in bucket if '硬止损' in x[1][6])
        print(f"  {label:12s}: {len(bucket)}笔, 均盈亏{avg_p:+6.1f}%, 胜率{wr:4.0f}%, 止损{stops_bucket}次")

# === Final PnL summary ===
print()
print("=" * 70)
print("=== 资产曲线关键节点 ===")
asset_lines = [l for l in content.split('\n') if '资产=' in l and '摘要' in l]
for l in asset_lines[:5]:
    print(f"  {l.strip()[:120]}")
print("  ...")
for l in asset_lines[-5:]:
    print(f"  {l.strip()[:120]}")

# Extract final asset
import re as re2
if asset_lines:
    last = asset_lines[-1]
    m = re2.search(r'资产=(\d+)万', last)
    if m:
        final = int(m.group(1))
        print(f"\n  终点资产: {final}万 (起始30万)")
        print(f"  总收益: {final-30}万 ({(final-30)/30*100:+.1f}%)")

# Date range
print()
print("=" * 70)
print("=== 回测时间范围 ===")
date_lines = [l for l in content.split('\n') if 'bar=5' in l and '202' in l and '摘要' not in l]
if date_lines:
    print(f"  起始: {date_lines[0][:80]}")
    print(f"  结束: {date_lines[-1][:80]}")
