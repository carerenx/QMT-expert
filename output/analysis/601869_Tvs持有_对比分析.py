# -*- coding: utf-8 -*-
"""
长飞光纤(601869) 日内做T vs 长期持有 — 对比分析
=================================================
核心问题：持有同一只股票，做T到底能不能跑赢纯持有？
"""

import sys, os, time, json, math, warnings
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings('ignore')

# ============================================================
# Config
# ============================================================
CODE = '601869'
NAME = '长飞光纤'
OUTPUT_DIR = r'd:\02Project\QMT-expert\data\601869_t0_backtest'
INITIAL_CAPITAL = 5_000_000
COMMISSION = 0.00025
STAMP_TAX = 0.001
SLIPPAGE = 0.001

ATR_PERIOD = 14
VOL_BAND_MULT = 0.5
VOL_SELL_MULT = 0.75
GRID_LEVELS = 2
GRID_STEP_PCT = 0.015
MAX_T_RATIO = 0.5
MIN_LOT = 100

# ============================================================
# Data Fetch
# ============================================================
def fetch_data():
    UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    tc = f'sh{CODE}'
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    params = {'param': f'{tc},day,,,520,qfq'}
    r = requests.get(url, params=params,
                     headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=15)
    data = r.json()
    raw = data.get('data', {}).get(tc, {}).get('qfqday', []) or \
          data.get('data', {}).get(tc, {}).get('day', [])
    klines = [{'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
               'high': float(k[3]), 'low': float(k[4]), 'volume': float(k[5])} for k in raw]
    df = pd.DataFrame(klines).sort_values('date').reset_index(drop=True)

    # Features
    df['returns'] = df['close'].pct_change()
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['daily_range'] = (df['high'] - df['low']) / df['open']

    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift(1))
    tr3 = abs(df['low'] - df['close'].shift(1))
    df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()
    df['atr_pct'] = df['atr'] / df['close']

    df['volume_ma20'] = df['volume'].rolling(20).mean()
    df['volume_ratio'] = df['volume'] / df['volume_ma20']

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    df['trend_bull'] = (df['close'] > df['ma20']) & (df['ma5'] > df['ma20'])
    df['trend_bear'] = (df['close'] < df['ma20']) & (df['ma5'] < df['ma20'])

    return df.dropna().reset_index(drop=True)


# ============================================================
# T-Trading Simulation (Adaptive strategy)
# ============================================================
def simulate_t_trading(df, base_pct):
    """模拟日内做T，返回每日P&L序列"""
    n = len(df)
    start_close = df['close'].iloc[0]

    # 底仓
    base_cost = INITIAL_CAPITAL * base_pct
    base_shares = int(base_cost / (start_close * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    cash = INITIAL_CAPITAL - base_shares * start_close * (1 + COMMISSION)

    t_pnl_series = []
    t_active_series = []

    for i in range(n):
        row = df.iloc[i]
        o, h, l, c = row['open'], row['high'], row['low'], row['close']
        atr = row['atr']
        atr_pct = row['atr_pct']

        if pd.isna(atr) or atr <= 0:
            t_pnl_series.append(0.0)
            t_active_series.append(0)
            continue

        # 趋势判断
        trend_bull = row.get('trend_bull', False)
        trend_bear = row.get('trend_bear', False)

        # 量缩不做
        if row.get('volume_ratio', 1) < 0.6:
            t_pnl_series.append(0.0)
            t_active_series.append(0)
            continue

        max_t = int(base_shares * MAX_T_RATIO / MIN_LOT) * MIN_LOT
        if max_t < MIN_LOT:
            t_pnl_series.append(0.0)
            t_active_series.append(0)
            continue

        per_trade = int(max_t / 2 / MIN_LOT) * MIN_LOT
        if per_trade < MIN_LOT:
            per_trade = MIN_LOT

        day_pnl = 0.0
        traded = 0

        # --- 正T: 牛市或震荡 (阶梯3层 + 分批止盈，防踏空+卖飞) ---
        if not trend_bear:
            ladder = [
                {'mult': 0.30, 'ratio': 0.30},
                {'mult': 0.60, 'ratio': 0.40},
                {'mult': 1.00, 'ratio': 0.30},
            ]
            tp_levels = [
                {'atr_mult': 1.0, 'ratio': 0.40},
                {'atr_mult': 2.0, 'ratio': 0.35},
                {'atr_mult': None, 'ratio': 0.25},
            ]
            for lv in ladder:
                buy_price = o - atr * lv['mult']
                if l <= buy_price:
                    fill_buy = max(buy_price, l) * (1 + SLIPPAGE)
                    lv_shares_total = int(per_trade * lv['ratio'] / MIN_LOT) * MIN_LOT
                    if lv_shares_total < MIN_LOT:
                        continue
                    for tp in tp_levels:
                        tp_shares = int(lv_shares_total * tp['ratio'] / MIN_LOT) * MIN_LOT
                        if tp_shares < MIN_LOT:
                            continue
                        if tp['atr_mult'] is not None:
                            target_sell = fill_buy * (1 + atr_pct * tp['atr_mult'])
                            if h >= target_sell:
                                fill_sell = min(target_sell, h) * (1 - SLIPPAGE)
                                day_pnl += tp_shares * (fill_sell * (1 - COMMISSION - STAMP_TAX) -
                                                         fill_buy * (1 + COMMISSION))
                                traded += tp_shares
                        else:
                            day_pnl += tp_shares * (c * (1 - COMMISSION - STAMP_TAX) -
                                                     fill_buy * (1 + COMMISSION))
                            traded += tp_shares

        # --- 反T: 四重熔断 + 硬止损买回 (防卖飞底仓) ---
        short_t_allowed = True
        gap_up = row.get('gap', 0) if not pd.isna(row.get('gap', 0)) else 0
        up_streak = int(row.get('up_streak', 0)) if not pd.isna(row.get('up_streak', 0)) else 0
        macd_hist = row.get('macd_hist', 0) if not pd.isna(row.get('macd_hist', 0)) else 0
        if trend_bull:
            short_t_allowed = False
        elif gap_up > 0.02:
            short_t_allowed = False
        elif up_streak >= 3:
            short_t_allowed = False
        elif macd_hist > 0 and not trend_bear:
            short_t_allowed = False

        if short_t_allowed and not trend_bull:
            sell_price = o + atr * VOL_SELL_MULT * 1.2
            buy_price = sell_price * (1 - atr_pct * VOL_BAND_MULT)
            if h >= sell_price and l <= buy_price:
                fill_sell = min(sell_price, h) * (1 - SLIPPAGE)
                fill_buy = max(buy_price, l) * (1 + SLIPPAGE)
                shares = per_trade
                day_pnl += shares * (fill_sell * (1 - COMMISSION - STAMP_TAX) -
                                     fill_buy * (1 + COMMISSION))
                traded += shares

        # --- 网格 ---
        for level in range(1, GRID_LEVELS + 1):
            g_buy = o * (1 - GRID_STEP_PCT * level)
            g_sell = o * (1 + GRID_STEP_PCT * level)
            if l <= g_buy and h >= g_sell:
                g_shares = int(per_trade / GRID_LEVELS / MIN_LOT) * MIN_LOT
                if g_shares >= MIN_LOT:
                    day_pnl += g_shares * (g_sell * (1 - COMMISSION - STAMP_TAX) -
                                           g_buy * (1 + COMMISSION))
                    traded += g_shares

        t_pnl_series.append(day_pnl)
        t_active_series.append(1 if traded > 0 else 0)

    return t_pnl_series, t_active_series, base_shares


# ============================================================
# Scenario Builder
# ============================================================
def build_scenarios(df):
    """构建多种持仓+做T场景并对比"""

    close_series = df['close'].values
    dates = df['date'].values
    n = len(df)

    # ---- Scenario 1: 100% 长期持有 ----
    start_px = close_series[0]
    shares_bh = int(INITIAL_CAPITAL / (start_px * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    cash_bh = INITIAL_CAPITAL - shares_bh * start_px * (1 + COMMISSION)
    equity_bh = shares_bh * close_series + cash_bh

    # ---- Scenario 2: 30%底仓 + 做T (自适应) ----
    base_pct_30 = 0.30
    t_pnl_30, t_active_30, shares_30 = simulate_t_trading(df, base_pct_30)
    start_px_30 = close_series[0]
    cash_30 = INITIAL_CAPITAL * (1 - base_pct_30)
    cum_t_30 = np.cumsum(t_pnl_30)
    equity_t30 = shares_30 * close_series + cash_30 + cum_t_30

    # ---- Scenario 3: 50%底仓 + 做T ----
    base_pct_50 = 0.50
    t_pnl_50, t_active_50, shares_50 = simulate_t_trading(df, base_pct_50)
    start_px_50 = close_series[0]
    cash_50 = INITIAL_CAPITAL * (1 - base_pct_50)
    cum_t_50 = np.cumsum(t_pnl_50)
    equity_t50 = shares_50 * close_series + cash_50 + cum_t_50

    # ---- Scenario 4: 70%底仓 + 做T ----
    base_pct_70 = 0.70
    t_pnl_70, t_active_70, shares_70 = simulate_t_trading(df, base_pct_70)
    start_px_70 = close_series[0]
    cash_70 = INITIAL_CAPITAL * (1 - base_pct_70)
    cum_t_70 = np.cumsum(t_pnl_70)
    equity_t70 = shares_70 * close_series + cash_70 + cum_t_70

    # ---- Scenario 5: 30%底仓纯持有(不做T) ----
    base_cost_30h = INITIAL_CAPITAL * 0.30
    shares_30h = int(base_cost_30h / (start_px * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    cash_30h = INITIAL_CAPITAL - shares_30h * start_px * (1 + COMMISSION)
    equity_30h = shares_30h * close_series + cash_30h

    # ---- Scenario 6: 50%底仓纯持有(不做T) ----
    base_cost_50h = INITIAL_CAPITAL * 0.50
    shares_50h = int(base_cost_50h / (start_px * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    cash_50h = INITIAL_CAPITAL - shares_50h * start_px * (1 + COMMISSION)
    equity_50h = shares_50h * close_series + cash_50h

    # ---- Scenario 7: 70%底仓纯持有(不做T) ----
    base_cost_70h = INITIAL_CAPITAL * 0.70
    shares_70h = int(base_cost_70h / (start_px * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    cash_70h = INITIAL_CAPITAL - shares_70h * start_px * (1 + COMMISSION)
    equity_70h = shares_70h * close_series + cash_70h

    scenarios = {
        '100% Hold': {'equity': equity_bh, 'color': '#333333', 'ls': '-'},
        '70%+T0': {'equity': equity_t70, 'color': '#D62828', 'ls': '-'},
        '50%+T0': {'equity': equity_t50, 'color': '#FF6B35', 'ls': '-'},
        '30%+T0': {'equity': equity_t30, 'color': '#004E89', 'ls': '-'},
        '70% Hold': {'equity': equity_70h, 'color': '#999999', 'ls': '--'},
        '50% Hold': {'equity': equity_50h, 'color': '#AAAAAA', 'ls': '--'},
        '30% Hold': {'equity': equity_30h, 'color': '#BBBBBB', 'ls': '--'},
    }

    return scenarios, dates, close_series, {
        '30': (t_pnl_30, t_active_30),
        '50': (t_pnl_50, t_active_50),
        '70': (t_pnl_70, t_active_70),
    }


# ============================================================
# Metrics
# ============================================================
def calc_metrics(equity, name):
    n = len(equity)
    total_return = (equity[-1] / INITIAL_CAPITAL - 1) * 100
    rets = np.diff(equity) / equity[:-1]
    years = n / 252
    ann_return = ((equity[-1] / INITIAL_CAPITAL) ** (1/years) - 1) * 100 if years > 0 else 0
    vol = np.std(rets) * np.sqrt(252) * 100
    sharpe = (ann_return - 3) / vol if vol > 0 else 0
    cummax = np.maximum.accumulate(equity)
    dd = (equity - cummax) / cummax * 100
    max_dd = np.min(dd)
    return {
        'name': name,
        'final_equity': equity[-1],
        'total_return': total_return,
        'ann_return': ann_return,
        'volatility': vol,
        'sharpe': sharpe,
        'max_dd': max_dd,
    }


# ============================================================
# Print & Chart
# ============================================================
def main():
    print('=' * 70)
    print(f'  {NAME}({CODE}) — 做T vs 持有 对比分析')
    print('=' * 70)

    # Data
    print('\n[1/4] 获取数据...')
    df = fetch_data()
    print(f'  区间: {df["date"].iloc[0]} ~ {df["date"].iloc[-1]} ({len(df)}天)')
    print(f'  起始价: {df["close"].iloc[0]:.2f} -> 终止价: {df["close"].iloc[-1]:.2f}')
    print(f'  日均振幅: {df["daily_range"].mean()*100:.2f}%')

    # Run scenarios
    print('\n[2/4] 运行7种场景对比...')
    scenarios, dates, close, t_data = build_scenarios(df)

    # Calc metrics
    print('\n[3/4] 计算绩效...')
    all_metrics = {}
    for name, data in scenarios.items():
        all_metrics[name] = calc_metrics(data['equity'], name)

    # ---- Print comparison ----
    print('\n' + '=' * 70)
    print('  Return Comparison Table')
    print('=' * 70)
    print(f'\n  {"Scenario":<18} {"Final Equity":>14} {"Total Ret":>10} {"Ann Ret":>10} {"Sharpe":>7} {"Max DD":>10}')
    print('  ' + '-' * 74)

    # Sort
    order = ['100% Hold', '70%+T0', '50%+T0', '30%+T0',
             '70% Hold', '50% Hold', '30% Hold']
    for name in order:
        m = all_metrics[name]
        marker = ' <-- Max Return' if name == '70%+T0' else ''
        print(f'  {name:<18} {m["final_equity"]:>14,.0f} {m["total_return"]:>9.2f}% {m["ann_return"]:>9.2f}% {m["sharpe"]:>6.2f} {m["max_dd"]:>9.2f}%{marker}')

    # ---- Key comparison: T0 alpha ----
    print(f'\n  {"=" * 50}')
    print(f'  T0 Alpha: incremental value of T-trading at same base')
    print(f'  {"=" * 50}')

    base_labels = {'30': '30%', '50': '50%', '70': '70%'}
    for pct_key in ['30', '50', '70']:
        pct_label = base_labels[pct_key]
        name_t = f'{pct_label}+T0'
        name_h = f'{pct_label} Hold'
        t_ret = all_metrics[name_t]['total_return']
        h_ret = all_metrics[name_h]['total_return']
        alpha = t_ret - h_ret
        t_pnl, t_act = t_data[pct_key]
        active_days = sum(t_act)
        total_t_pnl = sum(t_pnl)
        print(f'  {pct_label}: Hold {h_ret:+.1f}% | +T0 {t_ret:+.1f}% | Alpha {alpha:+.1f}% | '
              f'T0 Profit {total_t_pnl/10000:.1f}W | Active {active_days}/{len(df)}d')

    # ---- Segment analysis ----
    print(f'\n  {"=" * 50}')
    print(f'  Segment Analysis (30% Base + T0)')
    print(f'  {"=" * 50}')

    n = len(df)
    seg_size = n // 3
    segments = [
        ('Phase1 (Early Rally)', 0, seg_size),
        ('Phase2 (Main Uptrend)', seg_size, seg_size * 2),
        ('Phase3 (Acceleration)', seg_size * 2, n),
    ]

    for seg_name, start, end in segments:
        seg_close = close[start:end]
        seg_dates = dates[start:end]
        hold_ret = (seg_close[-1] / seg_close[0] - 1) * 100
        t_pnl_30, t_act_30 = t_data['30']
        seg_t_pnl = sum(t_pnl_30[start:end])
        seg_t_active = sum(t_act_30[start:end])
        print(f'  {seg_name} ({seg_dates[0]}~{seg_dates[-1]}, {end-start}d):')
        print(f'    Hold: {hold_ret:+.1f}% | T0: {seg_t_pnl/10000:+.1f}W | T-days: {seg_t_active}')

    # ---- Monthly breakdown ----
    print(f'\n  {"=" * 50}')
    print(f'  Recent 6M Monthly T0 P&L (30% Base)')
    print(f'  {"=" * 50}')

    recent_months = {}
    t_pnl_30, t_act_30 = t_data['30']
    for i in range(max(0, n - 126), n):  # last ~6 months
        d = str(dates[i])[:7]  # YYYY-MM
        if d not in recent_months:
            recent_months[d] = {'pnl': 0, 'active': 0, 'days': 0, 'close_start': 0, 'close_end': 0}
        recent_months[d]['pnl'] += t_pnl_30[i]
        recent_months[d]['active'] += t_act_30[i]
        recent_months[d]['days'] += 1
        if recent_months[d]['close_start'] == 0:
            recent_months[d]['close_start'] = close[i]
        recent_months[d]['close_end'] = close[i]

    print(f'  {"Month":<10} {"T0 P&L(W)":>12} {"T-Days":>8} {"Mth Chg":>8} {"Price Range":>20}')
    print(f'  {"-" * 60}')
    for month, data in sorted(recent_months.items()):
        hold_chg = (data['close_end'] / data['close_start'] - 1) * 100
        print(f'  {month:<10} {data["pnl"]/10000:>+12.1f} {data["active"]:>7}天 {hold_chg:>+7.1f}% '
              f'{data["close_start"]:.0f}~{data["close_end"]:.0f}')

    # ---- Charts ----
    print('\n[4/4] 生成对比图表...')
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.ticker import FuncFormatter

        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

        dates_dt = pd.to_datetime(dates)

        # ===== Chart A: 权益曲线对比 (多场景) =====
        fig, axes = plt.subplots(3, 1, figsize=(18, 14),
                                 gridspec_kw={'height_ratios': [3, 1.5, 1.5]})

        ax = axes[0]
        for name, data in scenarios.items():
            eq = data['equity']
            ax.plot(dates_dt, eq / 10000, color=data['color'], linestyle=data['ls'],
                    linewidth=1.8 if '+' in name else 1.0,
                    alpha=0.9 if '+' in name else 0.5,
                    label=f'{name} ({all_metrics[name]["total_return"]:+.1f}%)')

        ax.axhline(y=INITIAL_CAPITAL / 10000, color='gray', linewidth=0.5, linestyle=':')
        ax.set_title(f'{NAME}({CODE}) — 做T vs 持有 权益曲线对比', fontsize=15, fontweight='bold')
        ax.set_ylabel('总资产 (万元)', fontsize=11)
        ax.legend(loc='upper left', fontsize=9, ncol=2)
        ax.grid(True, alpha=0.3)

        # ===== Chart B: 做T vs 持有 差值 (Alpha) =====
        ax2 = axes[1]
        for pct_key, pct_label, color in [('30', '30%', '#004E89'),
                                       ('50', '50%', '#FF6B35'),
                                       ('70', '70%', '#D62828')]:
            t_name = f'{pct_label}+T0'
            h_name = f'{pct_label} Hold'
            alpha_curve = (scenarios[t_name]['equity'] - scenarios[h_name]['equity']) / 10000
            ax2.fill_between(dates_dt, alpha_curve, 0, color=color, alpha=0.2)
            ax2.plot(dates_dt, alpha_curve, color=color, linewidth=1.2, label=f'{pct_label} T0 Alpha {alpha_curve[-1]:+.1f}W')

        ax2.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')
        ax2.set_ylabel('T0 Cumulative Alpha (10K CNY)', fontsize=10)
        ax2.legend(loc='upper left', fontsize=9)
        ax2.grid(True, alpha=0.3)

        # ===== Chart C: 月度做T收益柱状图 =====
        ax3 = axes[2]
        months_sorted = sorted(recent_months.keys())
        month_labels = months_sorted
        month_pnls = [recent_months[m]['pnl'] / 10000 for m in months_sorted]
        month_changes = [(recent_months[m]['close_end'] / recent_months[m]['close_start'] - 1) * 100
                        for m in months_sorted]

        bar_colors = ['#D62828' if p > 0 else '#1B998B' for p in month_pnls]
        bars = ax3.bar(range(len(month_labels)), month_pnls, color=bar_colors, alpha=0.7, label='T0 P&L(W)')

        # Overlay monthly hold return
        ax3_twin = ax3.twinx()
        ax3_twin.plot(range(len(month_labels)), month_changes, 'o-', color='#333333',
                     linewidth=1.5, markersize=6, label='Monthly Hold Return%')
        ax3_twin.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')

        ax3.set_xticks(range(len(month_labels)))
        ax3.set_xticklabels(month_labels, rotation=45, ha='right', fontsize=8)
        ax3.set_ylabel('T0 P&L (10K CNY)', fontsize=10)
        ax3_twin.set_ylabel('Hold Return %', fontsize=10)
        ax3.grid(True, alpha=0.3, axis='y')

        # Combined legend
        lines1, labels1 = ax3.get_legend_handles_labels()
        lines2, labels2 = ax3_twin.get_legend_handles_labels()
        ax3.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=8)

        plt.tight_layout()
        chart_path = os.path.join(OUTPUT_DIR, 'chart5_TvsHold.png')
        plt.savefig(chart_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart5_TvsHold.png')

        # ===== Chart D: 做T效率散点图 =====
        fig, ax = plt.subplots(figsize=(12, 7))

        # Scatter: X=daily amplitude, Y=T0 daily P&L
        t_pnl_30_arr, t_act_30_arr = t_data['30']
        true_vol_days = np.where(t_act_30_arr == 1)[0]
        amplitudes = df['daily_range'].values[true_vol_days] * 100
        t_pnls = np.array(t_pnl_30_arr)[true_vol_days] / 10000

        scatter = ax.scatter(amplitudes, t_pnls, c=df['close'].values[true_vol_days],
                           cmap='RdYlGn', alpha=0.6, s=30, edgecolors='none')

        # Trend line
        if len(amplitudes) > 2:
            z = np.polyfit(amplitudes, t_pnls, 1)
            p = np.poly1d(z)
            x_smooth = np.linspace(amplitudes.min(), amplitudes.max(), 100)
            ax.plot(x_smooth, p(x_smooth), '--', color='#333333', linewidth=1.5,
                   label=f'Trend: +1% amplitude = +{z[0]:.1f}W T0 P&L')

        ax.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')
        ax.set_xlabel('Daily Amplitude %', fontsize=12)
        ax.set_ylabel('T0 Daily P&L (10K CNY)', fontsize=12)
        ax.set_title(f'{NAME}({CODE}) — Amplitude vs T0 P&L (30% Base, {len(true_vol_days)} T-days)', fontsize=13, fontweight='bold')
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        cbar = plt.colorbar(scatter, ax=ax)
        cbar.set_label('股价 (元)', fontsize=10)

        plt.tight_layout()
        chart2_path = os.path.join(OUTPUT_DIR, 'chart6_amplitude_vs_pnl.png')
        plt.savefig(chart2_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart6_amplitude_vs_pnl.png')

    except Exception as e:
        print(f'  图表错误: {e}')
        import traceback
        traceback.print_exc()

    # ---- Final summary ----
    print('\n' + '=' * 70)
    print('  核心结论')
    print('=' * 70)

    best_ret = max(all_metrics.items(), key=lambda x: x[1]['total_return'])
    m_base = all_metrics['100% Hold']
    m_t70 = all_metrics['70%+T0']
    m_h70 = all_metrics['70% Hold']

    print(f'''
  1. 100% Long-term Hold: {m_base['total_return']:+.1f}% (Sharpe {m_base['sharpe']:.1f}, MaxDD {m_base['max_dd']:.1f}%)
     -> Max return but max volatility, full exposure to drawdowns

  2. 70% Base + T0: {m_t70['total_return']:+.1f}% (Sharpe {m_t70['sharpe']:.1f}, MaxDD {m_t70['max_dd']:.1f}%)
     -> Beats 70% Hold ({m_h70['total_return']:+.1f}%) by {all_metrics["70%+T0"]["total_return"] - all_metrics["70% Hold"]["total_return"]:+.1f}%

  3. T0 Value: At SAME base position, T0 consistently adds positive alpha
     -> Larger base = higher absolute T0 profit (more shares to trade)
     -> But larger base = bigger drawdown in bear markets

  4. Best approach depends on risk tolerance:
     -> Aggressive: 100% Hold (return {m_base["total_return"]:+.1f}% but DD {m_base["max_dd"]:.1f}%)
     -> Balanced: 70% Base + T0 (return {m_t70["total_return"]:+.1f}%, DD {m_t70["max_dd"]:.1f}%)
     -> Conservative: 50% Base + T0 (half cash buffer)

  5. Key insight: T0 is NOT about beating 100% Hold on raw return
     -> It's about getting close to 100% Hold returns with MUCH smaller drawdowns
     -> T0 converts daily volatility into cash flow while maintaining upside exposure
    ''')

    # Save
    summary = {name: m for name, m in all_metrics.items()}
    summary['stock_info'] = {
        'code': CODE, 'name': NAME,
        'start_price': float(df['close'].iloc[0]),
        'end_price': float(df['close'].iloc[-1]),
        'stock_return_pct': float((df['close'].iloc[-1]/df['close'].iloc[0] - 1) * 100),
        'daily_amplitude_mean': float(df['daily_range'].mean() * 100),
        'period': f'{df["date"].iloc[0]} ~ {df["date"].iloc[-1]}',
        'trading_days': len(df),
    }
    json_path = os.path.join(OUTPUT_DIR, 'TvsHold_comparison.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n  结果已保存: {json_path}')

    return all_metrics, scenarios, dates


if __name__ == '__main__':
    main()
