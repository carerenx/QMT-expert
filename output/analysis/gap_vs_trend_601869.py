# -*- coding: utf-8 -*-
"""
分析 长飞光纤(601869) 开盘缺口(高开/低开) 与 整日走势的关系

核心问题:
  高开 → 当天继续涨还是回落?
  低开 → 当天继续跌还是反弹?
  → 直接影响反T策略: 高开日做反T(先卖)是否安全?

数据来源: 腾讯K线(前复权)
"""
import sys, os, time, warnings
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings('ignore')

CODE = '601869'
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gap_analysis')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ============================================================
# 数据获取
# ============================================================
def fetch_data(code='601869', days=300):
    """从腾讯获取日线"""
    UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    tc = f'sh{code}'
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    r = requests.get(url, params={'param': f'{tc},day,,,{days},qfq'},
                     headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=15)
    data = r.json()
    raw = data.get('data', {}).get(tc, {}).get('qfqday', []) or \
          data.get('data', {}).get(tc, {}).get('day', [])
    klines = [{'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
                'high': float(k[3]), 'low': float(k[4]), 'volume': float(k[5])} for k in raw]
    df = pd.DataFrame(klines).sort_values('date').reset_index(drop=True)
    print(f'数据: {len(df)} 条, {df["date"].iloc[0]} ~ {df["date"].iloc[-1]}')
    return df


# ============================================================
# 特征计算
# ============================================================
def compute_features(df):
    """计算缺口 + 日内走势特征"""
    # 开盘缺口: (今开 - 昨收) / 昨收
    df['gap'] = (df['open'] - df['close'].shift(1)) / df['close'].shift(1)

    # 日内走势: (今收 - 今开) / 今开
    df['intraday'] = (df['close'] - df['open']) / df['open']

    # 日内最高相对开盘: (最高-开盘)/开盘
    df['intraday_high'] = (df['high'] - df['open']) / df['open']

    # 日内最低相对开盘: (最低-开盘)/开盘
    df['intraday_low'] = (df['low'] - df['open']) / df['open']

    # 日内振幅
    df['range'] = (df['high'] - df['low']) / df['open']

    # 收盘方向: 阳线/阴线
    df['is_green'] = df['close'] >= df['open']

    # 缺口分类
    def gap_type(g):
        if pd.isna(g): return 'N/A'
        if g > 0.02: return '大幅高开(>2%)'
        if g > 0.005: return '小幅高开(0.5-2%)'
        if g > -0.005: return '平开(-0.5~0.5%)'
        if g > -0.02: return '小幅低开(-2%~-0.5%)'
        return '大幅低开(<-2%)'
    df['gap_type'] = df['gap'].apply(gap_type)

    # 趋势强度(前5日涨跌幅)
    df['trend5'] = df['close'].pct_change(5)

    # 前日涨跌
    df['prev_day_up'] = df['close'].shift(1) > df['open'].shift(1)
    df['prev_gap'] = df['gap'].shift(1)

    return df.dropna()


# ============================================================
# 分析
# ============================================================
def analyze(df):
    """核心分析"""
    print(f'\n{"="*65}')
    print(f'  长飞光纤({CODE}) 开盘缺口 vs 日内走势分析')
    print(f'  区间: {df["date"].iloc[0]} ~ {df["date"].iloc[-1]} ({len(df)}天)')
    print(f'{"="*65}')

    # ================================================================
    # 1. 缺口分布
    # ================================================================
    print(f'\n[1] 缺口类型分布')
    gap_counts = df['gap_type'].value_counts()
    for k in ['大幅高开(>2%)','小幅高开(0.5-2%)','平开(-0.5~0.5%)','小幅低开(-2%~-0.5%)','大幅低开(<-2%)']:
        c = gap_counts.get(k, 0)
        print(f'  {k:<20} {c:>4}天 ({c/len(df)*100:5.1f}%)')

    # ================================================================
    # 2. ★ 核心: 缺口 vs 收盘方向
    # ================================================================
    print(f'\n[2] 缺口 vs 收盘方向 (最重要的分析)')
    print(f'  {"缺口类型":<20} {"总天数":>5} {"收阳":>5} {"收阳率":>8} {"收阴":>5} {"收阴率":>8} {"平均日内涨跌":>10}')
    print(f'  {"─"*20} {"─"*5} {"─"*5} {"─"*8} {"─"*5} {"─"*8} {"─"*10}')

    for k in ['大幅高开(>2%)','小幅高开(0.5-2%)','平开(-0.5~0.5%)','小幅低开(-2%~-0.5%)','大幅低开(<-2%)']:
        sub = df[df['gap_type'] == k]
        if len(sub) == 0: continue
        green = len(sub[sub['is_green']])
        red   = len(sub) - green
        avg_id = sub['intraday'].mean() * 100
        print(f'  {k:<20} {len(sub):>5} {green:>5} {green/len(sub)*100:>7.1f}% {red:>5} {red/len(sub)*100:>7.1f}% {avg_id:>+9.2f}%')

    # 相关性
    gap_intraday_corr = df['gap'].corr(df['intraday'])
    print(f'\n  缺口(gap) vs 日内走势(intraday) 相关系数: {gap_intraday_corr:.3f}')
    if abs(gap_intraday_corr) > 0.3:
        direction = '同向' if gap_intraday_corr > 0 else '反向'
        print(f'  → {direction}关系较强(|r|>{0.3})')

    # ================================================================
    # 3. 高开后的日内走势细节
    # ================================================================
    print(f'\n[3] 高开日的日内细节')
    gap_up = df[df['gap'] > 0.005]
    if len(gap_up) > 0:
        print(f'  高开日(>0.5%): {len(gap_up)}天')
        print(f'    收盘收阳: {len(gap_up[gap_up["is_green"]])}天 ({len(gap_up[gap_up["is_green"]])/len(gap_up)*100:.1f}%)')
        print(f'    收盘收阴: {len(gap_up[~gap_up["is_green"]])}天 ({len(gap_up[~gap_up["is_green"]])/len(gap_up)*100:.1f}%)')
        print(f'    平均日内涨跌: {gap_up["intraday"].mean()*100:+.2f}%')
        # ★ 关键: 高开后盘中最高能冲多少, 最低跌多少
        print(f'    盘中平均最高(相对开盘): +{gap_up["intraday_high"].mean()*100:.1f}%')
        print(f'    盘中平均最低(相对开盘): {gap_up["intraday_low"].mean()*100:.1f}%')
        # 高开后冲高回落的概率
        gap_up_peaked = len(gap_up[(gap_up['intraday_high'] > 0.02) & (~gap_up['is_green'])])
        print(f'    冲高>2%后收阴: {gap_up_peaked}天 ({gap_up_peaked/len(gap_up)*100:.1f}%) ← 反T最佳场景')

    # ================================================================
    # 4. 低开后的日内走势细节
    # ================================================================
    print(f'\n[4] 低开日的日内细节')
    gap_down = df[df['gap'] < -0.005]
    if len(gap_down) > 0:
        print(f'  低开日(<-0.5%): {len(gap_down)}天')
        print(f'    收盘收阳: {len(gap_down[gap_down["is_green"]])}天 ({len(gap_down[gap_down["is_green"]])/len(gap_down)*100:.1f}%)')
        print(f'    收盘收阴: {len(gap_down[~gap_down["is_green"]])}天 ({len(gap_down[~gap_down["is_green"]])/len(gap_down)*100:.1f}%)')
        print(f'    平均日内涨跌: {gap_down["intraday"].mean()*100:+.2f}%')
        print(f'    盘中平均最高(相对开盘): +{gap_down["intraday_high"].mean()*100:.1f}%')
        print(f'    盘中平均最低(相对开盘): {gap_down["intraday_low"].mean()*100:.1f}%')
        # 低开后翻红的概率
        gap_down_reverse = len(gap_down[gap_down['is_green']])
        print(f'    低开后收阳(翻红): {gap_down_reverse}天 ({gap_down_reverse/len(gap_down)*100:.1f}%) ← 正T最佳场景')

    # ================================================================
    # 5. 趋势环境下的缺口效应
    # ================================================================
    print(f'\n[5] 趋势环境下的缺口效应')
    df['trend5_dir'] = pd.cut(df['trend5'], bins=[-1, -0.05, 0.05, 1],
                               labels=['前5日下跌', '前5日横盘', '前5日上涨'])

    for td in ['前5日上涨', '前5日横盘', '前5日下跌']:
        sub = df[df['trend5_dir'] == td]
        if len(sub) == 0: continue
        gap_up_s = sub[sub['gap'] > 0.005]
        gap_dn_s = sub[sub['gap'] < -0.005]
        print(f'\n  [{td}] ({len(sub)}天)')
        if len(gap_up_s) > 0:
            green_r = len(gap_up_s[gap_up_s['is_green']]) / len(gap_up_s) * 100
            print(f'    高开后收阳率: {green_r:.0f}% ({len(gap_up_s)}天)  平均日内{gap_up_s["intraday"].mean()*100:+.2f}%')
        if len(gap_dn_s) > 0:
            green_r = len(gap_dn_s[gap_dn_s['is_green']]) / len(gap_dn_s) * 100
            print(f'    低开后收阳率: {green_r:.0f}% ({len(gap_dn_s)}天)  平均日内{gap_dn_s["intraday"].mean()*100:+.2f}%')

    # ================================================================
    # 6. ★ 对反T策略的直接启示
    # ================================================================
    print(f'\n{"="*65}')
    print(f'  [6] 对反T策略的启示')
    print(f'{"="*65}')

    # 高开日: 反T先卖, 等回落再买
    gap_up_2pct = df[(df['gap'] > 0.005) & (df['gap'] <= 0.02)]
    gap_up_big  = df[df['gap'] > 0.02]

    if len(gap_up_2pct) > 0:
        # 高开后回落的概率 → 反T成功
        avg_pullback = gap_up_2pct['intraday_high'].mean() - gap_up_2pct['intraday'].mean()
        print(f'\n  小幅高开(0.5-2%): {len(gap_up_2pct)}天')
        print(f'    反T可能获利(日内收阴或盘中回落>1%): '
              f'{len(gap_up_2pct[(~gap_up_2pct["is_green"]) | (gap_up_2pct["intraday_high"] - gap_up_2pct["intraday"] > 0.01)])}天')
        print(f'    反T风险(高开后一路涨不回头, 收阳+无显著回落): '
              f'{len(gap_up_2pct[gap_up_2pct["is_green"] & (gap_up_2pct["intraday_high"] - gap_up_2pct["intraday"] < 0.005)])}天')

    if len(gap_up_big) > 0:
        print(f'\n  大幅高开(>2%): {len(gap_up_big)}天')
        print(f'    收阳率: {len(gap_up_big[gap_up_big["is_green"]])/len(gap_up_big)*100:.1f}%')
        print(f'    盘中平均回落: {(gap_up_big["intraday_high"]-gap_up_big["intraday"]).mean()*100:.1f}%')

    # 低开日: 反T做不了(先卖不划算), 但正T可用
    gap_dn = df[df['gap'] < -0.005]
    if len(gap_dn) > 0:
        print(f'\n  低开日(<-0.5%): {len(gap_dn)}天')
        print(f'    低开后收阳(翻红): {len(gap_dn[gap_dn["is_green"]])}天 ({len(gap_dn[gap_dn["is_green"]])/len(gap_dn)*100:.1f}%) ← 正T机会')
        print(f'    低开后继续跌(收阴): {len(gap_dn[~gap_dn["is_green"]])}天 ({len(gap_dn[~gap_dn["is_green"]])/len(gap_dn)*100:.1f}%)')

    # ================================================================
    # 7. 按缺口大小的详细分段
    # ================================================================
    print(f'\n[7] 按缺口大小详细分段')
    bins = [-1, -0.03, -0.02, -0.01, -0.005, 0, 0.005, 0.01, 0.02, 0.03, 1]
    labels = ['<-3%','-3~-2%','-2~-1%','-1~-0.5%','-0.5~0%','0~0.5%','0.5~1%','1~2%','2~3%','>3%']
    df['gap_bin'] = pd.cut(df['gap'], bins=bins, labels=labels)

    print(f'  {"缺口区间":<12} {"天数":>5} {"收阳率":>8} {"均日内%":>9} {"均最高%":>8} {"均最低%":>8}')
    print(f'  {"─"*12} {"─"*5} {"─"*8} {"─"*9} {"─"*8} {"─"*8}')
    for lb in labels:
        sub = df[df['gap_bin'] == lb]
        if len(sub) == 0: continue
        gr = len(sub[sub['is_green']]) / len(sub) * 100
        print(f'  {lb:<12} {len(sub):>5} {gr:>7.1f}% {sub["intraday"].mean()*100:>+8.2f}% '
              f'{sub["intraday_high"].mean()*100:>+7.2f}% {sub["intraday_low"].mean()*100:>+7.2f}%')

    return df


if __name__ == '__main__':
    print(f'获取 {CODE} 近一年日线数据...')
    df = fetch_data(CODE, days=280)
    df = compute_features(df)
    result = analyze(df)

    # 保存
    out = os.path.join(OUTPUT_DIR, f'{CODE}_gap_intraday_analysis.csv')
    result.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'\n详细数据: {out}')
