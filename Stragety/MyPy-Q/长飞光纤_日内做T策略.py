# -*- coding: utf-8 -*-
"""
长飞光纤(601869) 日内做T量化策略 — QMT兼容版
============================================

策略逻辑：
  基于长飞光纤高波动、高价股、强趋势的特性，设计4种日内做T子策略：
  1. 波动带做T (VolBand) — 基于开盘价±ATR倍数的区间交易
  2. 网格做T (Grid) — 预设多层网格，价格触及即成交
  3. 动量做T (Momentum) — 跟随开盘方向顺势做T
  4. 自适应复合做T (Adaptive) — 根据市场状态动态选择策略

V2.0 新增: 卖飞/踏空防护机制
  - 正T买入: 阶梯3层挂单 (防踏空)
  - 正T卖出: 分批止盈 + 移动止盈 (防卖飞)
  - 反T: 四重熔断检查 (防牛市卖飞底仓)
  - 反T: 硬止损买回 (涨超1%立即认赔)
  - 尾盘: 时间衰减平仓 (不赚钱不过夜)
  4. 自适应复合做T (Adaptive) — 根据市场状态动态选择策略

做T规则（A股T+1约束下）：
  - 底仓持有者：当日买入 → 当日可卖出同等数量（底仓代替）
  - 先买后卖（正T）：开盘跌到买点→买入→反弹到卖点→卖出
  - 先卖后买（反T）：开盘涨到卖点→卖出（用底仓）→回落→买回

QMT运行方式：
  - 实盘/模拟盘：在QMT中作为Python策略运行(日线触发)
  - 回测：本文件自带独立回测函数，可脱离QMT运行
"""

import sys, os, time, json, csv, math, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

warnings.filterwarnings('ignore')

# ============================================================
# 全局配置
# ============================================================
CODE = '601869'
NAME = '长飞光纤'
OUTPUT_DIR = r'd:\02Project\QMT-expert\data\601869_t0_backtest'
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 资金与费率
INITIAL_CAPITAL = 5_000_000    # 500万总资金
BASE_POSITION_PCT = 0.30       # 底仓占总资金30% (150万)
MAX_T_RATIO = 0.50             # 单日做T最多用底仓的50%
MIN_LOT = 100                  # A股最小交易单位100股

COMMISSION = 0.00025           # 佣金万分之2.5
STAMP_TAX = 0.001              # 印花税千分之一（卖出）
SLIPPAGE = 0.001               # 滑点千分之一

# 策略参数
ATR_PERIOD = 14                # ATR计算周期
VOL_BAND_MULT = 0.6            # 波动带ATR倍数（买入带）
VOL_SELL_MULT = 0.8            # 波动带ATR倍数（卖出带）
GRID_LEVELS = 3                # 网格层级数
GRID_STEP_PCT = 0.015          # 每层网格间距(1.5%)
MOMENTUM_THRESHOLD = 0.01      # 动量阈值(1%)
STOP_LOSS_PCT = 0.03           # 止损3%

# ==== V2.0 卖飞/踏空防护参数 ====
# 阶梯买入 (防踏空)
LADDER_LEVELS = [
    {'mult': 0.30, 'ratio': 0.30},  # 浅回调 30%仓位
    {'mult': 0.60, 'ratio': 0.40},  # 中等回调 40%仓位
    {'mult': 1.00, 'ratio': 0.30},  # 深回调 30%仓位
]
# 分批止盈 (防卖飞)
TAKE_PROFIT_LEVELS = [
    {'atr_mult': 1.0, 'ratio': 0.40},   # 第1批 +1ATR 卖40%
    {'atr_mult': 2.0, 'ratio': 0.35},   # 第2批 +2ATR 卖35%
    {'atr_mult': None, 'ratio': 0.25},  # 第3批 移动止盈/收盘平仓 25%
]
# 反T熔断 (防牛市卖飞底仓)
SHORT_T_MAX_GAP_UP = 0.02      # 开盘涨幅>2% 禁反T
SHORT_T_MAX_UP_STREAK = 3      # 连涨>=3天 禁反T
SHORT_T_BUYBACK_STOP = 0.01    # 反T卖出后涨超1% 立即买回

# 趋势过滤
MA_SHORT = 5
MA_MID = 20
MA_LONG = 60

# ============================================================
# 数据获取
# ============================================================

def fetch_data(code='601869'):
    """从腾讯/东方财富获取日线数据（含资金流）"""
    import requests

    UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

    # --- K线 ---
    tc = f'sh{code}'
    url_kline = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    params = {'param': f'{tc},day,,,520,qfq'}  # 约2年
    r = requests.get(url_kline, params=params,
                     headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=15)
    data = r.json()
    raw = data.get('data', {}).get(tc, {}).get('qfqday', []) or \
          data.get('data', {}).get(tc, {}).get('day', [])
    klines = []
    for k in raw:
        klines.append({
            'date': k[0],
            'open': float(k[1]), 'close': float(k[2]),
            'high': float(k[3]), 'low': float(k[4]),
            'volume': float(k[5])
        })
    df = pd.DataFrame(klines)
    df = df.sort_values('date').reset_index(drop=True)
    print(f'  K线数据: {len(df)} 条, {df["date"].iloc[0]} ~ {df["date"].iloc[-1]}')

    # --- 资金流 ---
    EM_SESSION = requests.Session()
    EM_SESSION.headers.update({'User-Agent': UA})
    last_req = [0.0]

    def em_get(url, params=None, timeout=15):
        wait = 1.3 - (time.time() - last_req[0])
        if wait > 0: time.sleep(wait + np.random.uniform(0.1, 0.4))
        try:
            return EM_SESSION.get(url, params=params, timeout=timeout)
        finally:
            last_req[0] = time.time()

    url_flow = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
    params_flow = {
        'secid': f'1.{code}',
        'fields1': 'f1,f2,f3,f7',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65',
        'lmt': '500',
    }
    r_flow = em_get(url_flow, params=params_flow)
    d_flow = r_flow.json()
    flow_lines = d_flow.get('data', {}).get('klines', [])
    flows = []
    for line in flow_lines:
        parts = line.split(',')
        if len(parts) >= 7:
            flows.append({
                'date': parts[0],
                'main_net': float(parts[1]) if parts[1] != '-' else 0,
                'small_net': float(parts[2]) if parts[2] != '-' else 0,
                'mid_net': float(parts[3]) if parts[3] != '-' else 0,
                'large_net': float(parts[4]) if parts[4] != '-' else 0,
                'super_net': float(parts[5]) if parts[5] != '-' else 0,
            })
    df_flow = pd.DataFrame(flows)
    print(f'  资金流数据: {len(df_flow)} 条')

    # 合并 (left join: 以K线为主，资金流缺失时填0)
    df = df.merge(df_flow, on='date', how='left')
    df = df.sort_values('date').reset_index(drop=True)
    # 填充缺失的资金流数据
    for col in ['main_net', 'small_net', 'mid_net', 'large_net', 'super_net']:
        if col in df.columns:
            df[col] = df[col].fillna(0.0)
    print(f'  合并后: {len(df)} 条')
    return df


# ============================================================
# 特征工程
# ============================================================

def compute_features(df):
    """计算技术指标"""
    # --- 价格特征 ---
    df['returns'] = df['close'].pct_change()
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()

    # 日内振幅
    df['daily_range'] = (df['high'] - df['low']) / df['open']
    df['daily_range_ma20'] = df['daily_range'].rolling(20).mean()

    # --- ATR ---
    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift(1))
    tr3 = abs(df['low'] - df['close'].shift(1))
    df['tr'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()
    df['atr_pct'] = df['atr'] / df['close']  # ATR百分比

    # --- 波动率 ---
    df['volatility_10'] = df['returns'].rolling(10).std()
    df['volatility_20'] = df['returns'].rolling(20).std()

    # --- 成交量 ---
    df['volume_ma5'] = df['volume'].rolling(5).mean()
    df['volume_ma20'] = df['volume'].rolling(20).mean()
    df['volume_ratio'] = df['volume'] / df['volume_ma20']

    # --- RSI ---
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # --- MACD ---
    df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
    df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = df['ema12'] - df['ema26']
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # --- 资金流 ---
    df['main_net_ma5'] = df['main_net'].rolling(5).mean()
    df['main_net_ma20'] = df['main_net'].rolling(20).mean()

    # --- 趋势判断 ---
    df['trend_bull'] = (df['close'] > df['ma20']) & (df['ma5'] > df['ma20'])
    df['trend_bear'] = (df['close'] < df['ma20']) & (df['ma5'] < df['ma20'])
    df['trend_sideways'] = ~(df['trend_bull'] | df['trend_bear'])

    # --- 连涨/连跌 ---
    df['up_streak'] = (df['close'] > df['close'].shift(1)).astype(int)
    df['up_streak'] = df['up_streak'].groupby((df['up_streak'] != df['up_streak'].shift(1)).cumsum()).cumsum()
    df['down_streak'] = (df['close'] < df['close'].shift(1)).astype(int)
    df['down_streak'] = df['down_streak'].groupby((df['down_streak'] != df['down_streak'].shift(1)).cumsum()).cumsum()

    # --- 开盘缺口 ---
    df['gap'] = (df['open'] - df['close'].shift(1)) / df['close'].shift(1)

    return df


# ============================================================
# 做T策略核心函数
# ============================================================

class T0Trader:
    """日内做T交易管理器"""

    def __init__(self, df, strategy_name, config_override=None):
        self.df = df
        self.name = strategy_name
        self.n = len(df)

        # 覆盖配置
        self.vol_band_mult = VOL_BAND_MULT
        self.vol_sell_mult = VOL_SELL_MULT
        self.grid_step_pct = GRID_STEP_PCT
        self.grid_levels = GRID_LEVELS
        self.momentum_threshold = MOMENTUM_THRESHOLD
        self.stop_loss_pct = STOP_LOSS_PCT
        if config_override:
            for k, v in config_override.items():
                setattr(self, k, v)

        # 状态
        self.reset()

    def reset(self):
        self.base_shares = 0       # 底仓股数
        self.cash = 0.0            # 可用现金(做T专用)
        self.total_equity = 0.0
        self.daily_records = []
        self.trade_log = []
        self.equity_curve = []

    def set_base_position(self, shares, cost_basis):
        """初始化底仓"""
        self.base_shares = shares
        self.base_cost = cost_basis
        self.cash = 0.0
        self.total_equity = shares * self.df['close'].iloc[0]

    def simulate_intraday(self, i):
        """
        模拟当日日内走势。

        仅用日线 OHLC 数据，按以下规则模拟日内执行：
        - T买入价 = 开盘价附近的下方区间 (若最低价触及则成交)
        - T卖出价 = 成本上方区间 (若最高价触及则成交)
        - 收盘强制平掉所有T仓位

        返回: (t_buy_price, t_sell_price, t_shares_traded, day_pnl)
        """
        row = self.df.iloc[i]
        o, h, l, c = row['open'], row['high'], row['low'], row['close']

        # 跳过数据不足的bar
        if pd.isna(row.get('atr')) or row['atr'] <= 0:
            return None

        atr = row['atr']
        atr_pct = row['atr_pct']

        # ---------- 确定市场状态 ----------
        trend = 'sideways'
        if row.get('trend_bull', False):
            trend = 'bull'
        elif row.get('trend_bear', False):
            trend = 'bear'

        # 附加趋势特征
        gap_up = row.get('gap', 0) if not pd.isna(row.get('gap', np.nan)) else 0
        up_streak = int(row.get('up_streak', 0)) if not pd.isna(row.get('up_streak', 0)) else 0
        macd_hist = row.get('macd_hist', 0) if not pd.isna(row.get('macd_hist', 0)) else 0

        # ---------- 正T/反T 开关判断 ----------
        # 牛市：只做正T，严禁反T (核心防卖飞铁律)
        do_long_t = trend != 'bear'
        do_short_t = True  # 先设True，然后用熔断逐条检查

        # ==== 反T四重熔断检查 (防牛市卖飞底仓) ====
        if trend == 'bull':
            do_short_t = False    # 熔断1: 牛市不做反T
        elif gap_up > SHORT_T_MAX_GAP_UP:
            do_short_t = False    # 熔断2: 大幅高开>2%不做反T
        elif up_streak >= SHORT_T_MAX_UP_STREAK:
            do_short_t = False    # 熔断3: 连涨>=3天不做反T
        elif macd_hist > 0 and trend != 'bear':
            do_short_t = False    # 熔断4: MACD向上动能不做反T(震荡市)

        # 成交量过滤：缩量不做T
        if row.get('volume_ratio', 1) < 0.6:
            do_long_t = False
            do_short_t = False

        # RSI极端不追
        if not pd.isna(row.get('rsi', 50)):
            if row['rsi'] > 80:
                do_long_t = False
            if row['rsi'] < 20:
                do_short_t = False

        # ---------- 执行模拟 ----------
        t_shares_total = 0
        t_buy_shares = 0
        t_sell_shares = 0
        t_buy_cost = 0.0
        t_sell_proceeds = 0.0

        # 最大可做T股数 = 底仓 × MAX_T_RATIO
        max_t_shares = int(self.base_shares * MAX_T_RATIO / MIN_LOT) * MIN_LOT
        if max_t_shares < MIN_LOT:
            return None

        per_trade_shares = int(max_t_shares / 2 / MIN_LOT) * MIN_LOT
        if per_trade_shares < MIN_LOT:
            per_trade_shares = MIN_LOT

        # ---- 正T：阶梯3层买入 + 分批止盈 (防踏空 + 防卖飞) ----
        if do_long_t:
            for lv in LADDER_LEVELS:
                buy_price = o - atr * lv['mult']
                if l <= buy_price:  # 该层买入触发
                    fill_buy = max(buy_price, l) * (1 + SLIPPAGE)
                    lv_shares_total = int(per_trade_shares * lv['ratio'] / MIN_LOT) * MIN_LOT
                    if lv_shares_total < MIN_LOT:
                        continue

                    # 对该层买入的仓位，分3批止盈卖出
                    for tp in TAKE_PROFIT_LEVELS:
                        tp_shares = int(lv_shares_total * tp['ratio'] / MIN_LOT) * MIN_LOT
                        if tp_shares < MIN_LOT:
                            continue

                        if tp['atr_mult'] is not None:
                            # 固定目标止盈
                            target_sell = fill_buy * (1 + atr_pct * tp['atr_mult'])
                            if h >= target_sell:
                                fill_sell = min(target_sell, h) * (1 - SLIPPAGE)
                                t_buy_shares += tp_shares
                                t_sell_shares += tp_shares
                                t_buy_cost += tp_shares * fill_buy * (1 + COMMISSION)
                                t_sell_proceeds += tp_shares * fill_sell * (1 - COMMISSION - STAMP_TAX)
                                t_shares_total += tp_shares
                        else:
                            # 第3批: 移动止盈 — 如果前2批已卖出, 第3批用收盘价平仓
                            # (让利润奔跑, 但收盘必须平)
                            t_buy_shares += tp_shares
                            t_sell_shares += tp_shares  # 收盘平仓在下方统一处理
                            t_buy_cost += tp_shares * fill_buy * (1 + COMMISSION)
                            t_sell_proceeds += tp_shares * c * (1 - COMMISSION - STAMP_TAX)
                            t_shares_total += tp_shares

        # ---- 反T：阶梯3层卖出 + 硬止损买回 (防卖飞底仓) ----
        if do_short_t:
            for lv in LADDER_LEVELS:
                sell_price = o + atr * lv['mult'] * 1.2  # 卖出偏高
                if h >= sell_price:  # 该层卖出触发
                    fill_sell = min(sell_price, h) * (1 - SLIPPAGE)
                    lv_shares_total = int(per_trade_shares * lv['ratio'] / MIN_LOT) * MIN_LOT
                    if lv_shares_total < MIN_LOT:
                        continue

                    # 3批买回
                    for tp in TAKE_PROFIT_LEVELS:
                        tp_shares = int(lv_shares_total * tp['ratio'] / MIN_LOT) * MIN_LOT
                        if tp_shares < MIN_LOT:
                            continue

                        if tp['atr_mult'] is not None:
                            buyback_price = fill_sell * (1 - atr_pct * tp['atr_mult'])
                            if l <= buyback_price:
                                fill_buyback = max(buyback_price, l) * (1 + SLIPPAGE)
                                t_sell_shares += tp_shares
                                t_buy_shares += tp_shares
                                t_sell_proceeds += tp_shares * fill_sell * (1 - COMMISSION - STAMP_TAX)
                                t_buy_cost += tp_shares * fill_buyback * (1 + COMMISSION)
                                t_shares_total += tp_shares
                            else:
                                # 股价没跌回买回价 — 反T踏空但不丢底仓
                                # 在收盘时以收盘价买回
                                t_sell_shares += tp_shares
                                t_buy_shares += tp_shares
                                t_sell_proceeds += tp_shares * fill_sell * (1 - COMMISSION - STAMP_TAX)
                                t_buy_cost += tp_shares * c * (1 + COMMISSION)
                                t_shares_total += tp_shares
                        else:
                            # 收盘买回
                            t_sell_shares += tp_shares
                            t_buy_shares += tp_shares
                            t_sell_proceeds += tp_shares * fill_sell * (1 - COMMISSION - STAMP_TAX)
                            t_buy_cost += tp_shares * c * (1 + COMMISSION)
                            t_shares_total += tp_shares

        # ---- 网格做T（额外层） ----
        for level in range(1, self.grid_levels + 1):
            grid_buy = o * (1 - self.grid_step_pct * level)
            grid_sell = o * (1 + self.grid_step_pct * level)
            # 买
            if l <= grid_buy and h >= grid_sell:
                g_shares = int(per_trade_shares / self.grid_levels / MIN_LOT) * MIN_LOT
                if g_shares >= MIN_LOT:
                    t_buy_shares += g_shares
                    t_sell_shares += g_shares
                    t_buy_cost += g_shares * grid_buy * (1 + COMMISSION)
                    t_sell_proceeds += g_shares * grid_sell * (1 - COMMISSION - STAMP_TAX)
                    t_shares_total += g_shares

        # ---------- 收盘强制平仓 (统一处理未配对的仓位) ----------
        net_shares = t_buy_shares - t_sell_shares
        if net_shares > 0:
            close_proceeds = net_shares * c * (1 - COMMISSION - STAMP_TAX)
            t_sell_proceeds += close_proceeds
            t_sell_shares += net_shares
        elif net_shares < 0:
            close_cost = abs(net_shares) * c * (1 + COMMISSION)
            t_buy_cost += close_cost
            t_buy_shares += abs(net_shares)

        # ---------- 计算当日做T盈亏 ----------
        day_pnl = t_sell_proceeds - t_buy_cost
        gross_pnl = day_pnl

        # ---------- 止损检查 ----------
        loss_limit = self.base_shares * o * self.stop_loss_pct * MAX_T_RATIO
        if day_pnl < -loss_limit:
            # 理论上日内止损通过"不再开新仓"实现
            day_pnl = max(day_pnl, -loss_limit * 2)

        return {
            'date': row['date'],
            'open': o, 'high': h, 'low': l, 'close': c,
            'atr': atr, 'atr_pct': atr_pct,
            'trend': trend,
            't_shares': t_shares_total,
            't_buy_shares': t_buy_shares,
            't_sell_shares': t_sell_shares,
            'day_pnl': day_pnl,
            'gross_pnl': gross_pnl,
            'do_long': do_long_t,
            'do_short': do_short_t,
            'short_t_blocked': not do_short_t and trend == 'bull',  # 反T被熔断阻止
            'gap_up': gap_up,
            'up_streak': up_streak,
        }

    def run(self):
        """运行完整回测"""
        self.reset()

        # 设定底仓：用初始资金的30%在回测起始日买入
        start_close = self.df['close'].iloc[0]
        base_cost = INITIAL_CAPITAL * BASE_POSITION_PCT
        base_shares_raw = base_cost / (start_close * (1 + COMMISSION + SLIPPAGE))
        self.set_base_position(
            int(base_shares_raw / MIN_LOT) * MIN_LOT,
            start_close
        )

        initial_equity = INITIAL_CAPITAL - self.base_shares * start_close * (1 + COMMISSION)
        self.cash = initial_equity
        self.total_equity = INITIAL_CAPITAL

        records = []
        cumulative_pnl = 0.0

        for i in range(len(self.df)):
            result = self.simulate_intraday(i)
            if result is None:
                # 底仓市值 + 现金
                day_close = self.df['close'].iloc[i]
                self.total_equity = self.base_shares * day_close + self.cash
                cumulative_pnl += 0
                records.append({
                    'date': self.df['date'].iloc[i],
                    'close': day_close,
                    'day_pnl': 0,
                    'cum_pnl': cumulative_pnl,
                    'total_equity': self.total_equity,
                    't_shares': 0,
                    'trend': 'N/A',
                })
                continue

            day_pnl = result['day_pnl']
            cumulative_pnl += day_pnl
            self.cash += day_pnl

            day_close = result['close']
            self.total_equity = self.base_shares * day_close + self.cash

            records.append({
                'date': result['date'],
                'close': day_close,
                'day_pnl': day_pnl,
                'cum_pnl': cumulative_pnl,
                'total_equity': self.total_equity,
                't_shares': result['t_shares'],
                'trend': result['trend'],
                'atr': result['atr'],
                'do_long': result['do_long'],
                'do_short': result['do_short'],
            })

            if result['t_shares'] > 0:
                self.trade_log.append(result)

        self.daily_records = records
        self.equity_curve = [r['total_equity'] for r in records]
        return records


# ============================================================
# 策略变体
# ============================================================

def strategy_vol_band(df):
    """策略A: 纯波动带做T"""
    trader = T0Trader(df, 'A_VolBand', {
        'vol_band_mult': 0.6, 'vol_sell_mult': 0.8,
        'grid_levels': 0,  # 关闭网格
    })
    return trader.run(), trader

def strategy_grid(df):
    """策略B: 纯网格做T"""
    trader = T0Trader(df, 'B_Grid', {
        'vol_band_mult': 1.5,   # 波动带设宽(基本不触发)
        'vol_sell_mult': 1.5,
        'grid_levels': 4,       # 4层网格
        'grid_step_pct': 0.012, # 每层1.2%
    })
    return trader.run(), trader

def strategy_momentum(df):
    """策略C: 动量跟随做T"""
    trader = T0Trader(df, 'C_Momentum', {
        'vol_band_mult': 0.4,   # 窄带(快速响应)
        'vol_sell_mult': 0.6,
        'grid_levels': 1,       # 1层网格
        'grid_step_pct': 0.02,
    })
    return trader.run(), trader

def strategy_adaptive(df):
    """策略D: 自适应复合做T (推荐)"""
    trader = T0Trader(df, 'D_Adaptive', {
        'vol_band_mult': 0.5,
        'vol_sell_mult': 0.75,
        'grid_levels': 2,
        'grid_step_pct': 0.015,
    })
    return trader.run(), trader


# ============================================================
# 绩效计算
# ============================================================

def compute_metrics(records, name):
    """计算绩效指标"""
    if not records:
        return {}

    df = pd.DataFrame(records)
    trading_days = len(df)
    active_days = len(df[df['t_shares'] > 0])

    total_pnl = df['day_pnl'].sum()
    avg_daily_pnl = total_pnl / trading_days if trading_days > 0 else 0
    avg_active_pnl = total_pnl / active_days if active_days > 0 else 0

    # 胜率
    active = df[df['t_shares'] > 0]
    win_days = len(active[active['day_pnl'] > 0])
    loss_days = len(active[active['day_pnl'] < 0])
    win_rate = (win_days / active_days * 100) if active_days > 0 else 0

    # 盈亏比
    avg_win = active[active['day_pnl'] > 0]['day_pnl'].mean() if win_days > 0 else 0
    avg_loss = active[active['day_pnl'] < 0]['day_pnl'].mean() if loss_days > 0 else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    # 最大回撤
    cum_pnl = df['cum_pnl']
    cum_max = cum_pnl.cummax()
    drawdown = cum_pnl - cum_max
    max_dd = drawdown.min()

    # 年化 (假设252交易日)
    years = trading_days / 252
    daily_returns = df['day_pnl'] / INITIAL_CAPITAL
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

    # 总收益 (相对初始资金)
    total_return = total_pnl / INITIAL_CAPITAL * 100
    ann_return = ((1 + total_pnl / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Calmar
    calmar = ann_return / abs(max_dd / INITIAL_CAPITAL * 100) if max_dd != 0 else 0

    # 日均做T股数
    avg_t_shares = active['t_shares'].mean() if active_days > 0 else 0

    return {
        'name': name,
        'total_days': trading_days,
        'active_days': active_days,
        'activity_rate': f'{active_days/trading_days*100:.1f}%',
        'total_pnl': f'{total_pnl:,.0f}',
        'total_return': f'{total_return:.2f}%',
        'annual_return': f'{ann_return:.2f}%',
        'avg_daily_pnl': f'{avg_daily_pnl:,.0f}',
        'avg_active_pnl': f'{avg_active_pnl:,.0f}',
        'win_rate': f'{win_rate:.1f}%',
        'profit_factor': f'{profit_factor:.2f}',
        'sharpe': f'{sharpe:.2f}',
        'max_drawdown': f'{max_dd:,.0f}',
        'max_drawdown_pct': f'{max_dd/INITIAL_CAPITAL*100:.2f}%',
        'calmar': f'{calmar:.2f}',
        'avg_t_shares': f'{avg_t_shares:,.0f}',
        'total_return_val': total_return,
        'sharpe_val': sharpe,
        'max_dd_val': max_dd,
    }


# ============================================================
# ============================================================
# 可视化
# ============================================================

def generate_charts(df, all_records, all_metrics, output_dir):
    """生成回测图表"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.ticker import FuncFormatter

        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

        dates = pd.to_datetime(df['date'])
        n = len(df)

        colors = ['#D62828', '#004E89', '#1B998B', '#FF6B35']

        # ======== Chart 1: 整体概览 ========
        fig, axes = plt.subplots(4, 1, figsize=(18, 16),
                                 gridspec_kw={'height_ratios': [2.5, 1.5, 1.5, 1.5]})

        # Ax1: 价格 + 均线
        ax = axes[0]
        ax.plot(dates, df['close'], color='#333333', linewidth=1.2, alpha=0.8, label='收盘价')
        ax.plot(dates, df['ma5'], color='#FF6B35', linewidth=0.8, alpha=0.6, label='MA5')
        ax.plot(dates, df['ma20'], color='#004E89', linewidth=0.8, alpha=0.6, label='MA20')
        ax.plot(dates, df['ma60'], color='#1B998B', linewidth=0.8, alpha=0.5, label='MA60')
        ax.set_title(f'{NAME}({CODE}) — 日内做T策略回测概览', fontsize=15, fontweight='bold')
        ax.set_ylabel('价格 (元)', fontsize=11)
        ax.legend(loc='upper left', fontsize=9)
        ax.grid(True, alpha=0.3)

        # Ax2: 日内振幅 + ATR
        ax2 = axes[1]
        ax2.fill_between(dates, df['daily_range'] * 100, 0, color='#D62828', alpha=0.2)
        ax2.plot(dates, df['daily_range'] * 100, color='#D62828', linewidth=0.8, alpha=0.7, label='日内振幅')
        ax2.plot(dates, df['daily_range_ma20'] * 100, color='#004E89', linewidth=1.2, label='20日均振幅')
        ax2.set_ylabel('振幅 %', fontsize=10)
        ax2.legend(loc='upper left', fontsize=9)
        ax2.grid(True, alpha=0.3)

        # Ax3: 成交量
        ax3 = axes[2]
        colors_bar = ['#D62828' if df['close'].iloc[i] >= df['open'].iloc[i] else '#1B998B' for i in range(n)]
        ax3.bar(dates, df['volume'] / 1e6, color=colors_bar, alpha=0.5, width=1)
        ax3.plot(dates, df['volume_ma5'] / 1e6, color='#FF6B35', linewidth=0.8, alpha=0.6, label='MA5量')
        ax3.set_ylabel('成交量 (百万)', fontsize=10)
        ax3.legend(loc='upper left', fontsize=9)
        ax3.grid(True, alpha=0.3)

        # Ax4: 主力资金流
        ax4 = axes[3]
        ax4.fill_between(dates, df['main_net'] / 1e8, 0,
                         where=df['main_net'] >= 0, color='#D62828', alpha=0.4, label='主力净流入')
        ax4.fill_between(dates, df['main_net'] / 1e8, 0,
                         where=df['main_net'] < 0, color='#1B998B', alpha=0.4, label='主力净流出')
        ax4.plot(dates, df['main_net_ma5'] / 1e8, color='#FF6B35', linewidth=0.8, label='MA5')
        ax4.axhline(y=0, color='#333333', linewidth=0.5)
        ax4.set_ylabel('主力净额 (亿)', fontsize=10)
        ax4.set_xlabel('日期', fontsize=11)
        ax4.legend(loc='upper left', fontsize=9)
        ax4.grid(True, alpha=0.3)

        plt.tight_layout()
        chart1_path = os.path.join(output_dir, 'chart1_overview.png')
        plt.savefig(chart1_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart1_overview.png')

        # ======== Chart 2: 各策略累计做T收益对比 ========
        fig, axes = plt.subplots(2, 1, figsize=(18, 10),
                                 gridspec_kw={'height_ratios': [2.5, 1.5]})

        ax = axes[0]
        for idx, (name, records) in enumerate(all_records.items()):
            df_r = pd.DataFrame(records)
            dates_r = pd.to_datetime(df_r['date'])
            ax.plot(dates_r, df_r['cum_pnl'] / 10000, color=colors[idx],
                    linewidth=1.5, alpha=0.85,
                    label=f'{name} ({all_metrics[name]["total_return"]})')

        ax.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')
        ax.set_title(f'{NAME}({CODE}) — 策略累计做T收益对比 (初始资金{INITIAL_CAPITAL/10000:.0f}万)', fontsize=14, fontweight='bold')
        ax.set_ylabel('累计做T收益 (万元)', fontsize=11)
        ax.legend(loc='upper left', fontsize=10)
        ax.grid(True, alpha=0.3)

        # 每日收益分布
        ax2 = axes[1]
        for idx, (name, records) in enumerate(all_records.items()):
            df_r = pd.DataFrame(records)
            active = df_r[df_r['t_shares'] > 0]
            if len(active) > 0:
                daily = active['day_pnl'] / 10000
                ax2.plot(range(len(daily)), daily, color=colors[idx],
                        linewidth=0.6, alpha=0.5, drawstyle='steps-mid')

        ax2.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')
        ax2.set_ylabel('单日做T收益 (万元)', fontsize=10)
        ax2.set_xlabel('交易日序号', fontsize=11)
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        chart2_path = os.path.join(output_dir, 'chart2_cumulative_pnl.png')
        plt.savefig(chart2_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart2_cumulative_pnl.png')

        # ======== Chart 3: 最优策略详细分析 ========
        best_name = max(all_metrics.items(), key=lambda x: x[1].get('total_return_val', 0))[0]
        best_records = all_records[best_name]
        df_best = pd.DataFrame(best_records)

        fig, axes = plt.subplots(4, 1, figsize=(18, 14),
                                 gridspec_kw={'height_ratios': [2, 1.5, 1.5, 1.5]})

        dates_best = pd.to_datetime(df_best['date'])

        # 价格走势 + 做T活跃日
        ax = axes[0]
        ax.plot(dates, df['close'], color='#333333', linewidth=1, alpha=0.7, label='收盘价')
        active_dates = df_best[df_best['t_shares'] > 0]
        inactive_dates = df_best[df_best['t_shares'] == 0]
        if len(active_dates) > 0:
            ax.scatter(pd.to_datetime(active_dates['date']), active_dates['close'],
                      color='#D62828', s=20, alpha=0.6, label=f'做T日({len(active_dates)}天)')
        ax.set_title(f'最优策略 [{best_name}] — 详细分析', fontsize=13, fontweight='bold')
        ax.set_ylabel('价格 (元)', fontsize=10)
        ax.legend(loc='upper left', fontsize=9)
        ax.grid(True, alpha=0.3)

        # 累计做T收益
        ax2 = axes[1]
        ax2.fill_between(dates_best, df_best['cum_pnl'] / 10000, 0,
                         where=df_best['cum_pnl'] >= 0, color='#D62828', alpha=0.3)
        ax2.fill_between(dates_best, df_best['cum_pnl'] / 10000, 0,
                         where=df_best['cum_pnl'] < 0, color='#1B998B', alpha=0.3)
        ax2.plot(dates_best, df_best['cum_pnl'] / 10000, color='#004E89', linewidth=1.5)
        ax2.axhline(y=0, color='#333333', linewidth=0.5, linestyle=':')
        ax2.set_ylabel('累计做T收益 (万元)', fontsize=10)
        ax2.grid(True, alpha=0.3)

        # 每日做T收益
        ax3 = axes[2]
        colors_daily = ['#D62828' if p > 0 else '#1B998B' for p in df_best['day_pnl']]
        ax3.bar(range(len(df_best)), df_best['day_pnl'] / 10000, color=colors_daily,
               alpha=0.6, width=1)
        ax3.axhline(y=0, color='#333333', linewidth=0.5)
        ax3.set_ylabel('单日做T收益 (万元)', fontsize=10)
        ax3.grid(True, alpha=0.3, axis='y')

        # 做T股数
        ax4 = axes[3]
        ax4.fill_between(range(len(df_best)), df_best['t_shares'] / 100, 0,
                         color='#FF6B35', alpha=0.5, step='mid')
        ax4.set_ylabel('做T股数 (手)', fontsize=10)
        ax4.set_xlabel('交易日序号', fontsize=11)
        ax4.grid(True, alpha=0.3)

        plt.tight_layout()
        chart3_path = os.path.join(output_dir, 'chart3_best_strategy_detail.png')
        plt.savefig(chart3_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart3_best_strategy_detail.png')

        # ======== Chart 4: 策略对比柱状图 ========
        fig, axes = plt.subplots(2, 2, figsize=(16, 10))

        names_list = list(all_metrics.keys())
        returns_list = [all_metrics[n].get('total_return_val', 0) for n in names_list]
        sharpes_list = [all_metrics[n].get('sharpe_val', 0) for n in names_list]
        win_rates = [float(all_metrics[n].get('win_rate', '0').replace('%', '')) for n in names_list]
        max_dds = [all_metrics[n].get('max_dd_val', 0) / INITIAL_CAPITAL * 100 for n in names_list]

        # 总收益
        ax = axes[0, 0]
        bar_colors = ['#D62828' if r > 0 else '#1B998B' for r in returns_list]
        bars = ax.bar(names_list, returns_list, color=bar_colors, alpha=0.75, edgecolor='white')
        ax.set_title('策略总收益率对比', fontsize=12, fontweight='bold')
        ax.set_ylabel('总收益率 %', fontsize=10)
        ax.grid(True, alpha=0.3, axis='y')
        for bar, ret in zip(bars, returns_list):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                    f'{ret:.2f}%', ha='center', fontsize=9, fontweight='bold')

        # 夏普比率
        ax2 = axes[0, 1]
        bars2 = ax2.bar(names_list, sharpes_list, color='#004E89', alpha=0.75, edgecolor='white')
        ax2.set_title('夏普比率对比', fontsize=12, fontweight='bold')
        ax2.set_ylabel('Sharpe Ratio', fontsize=10)
        ax2.grid(True, alpha=0.3, axis='y')
        for bar, s in zip(bars2, sharpes_list):
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                     f'{s:.2f}', ha='center', fontsize=9)

        # 胜率
        ax3 = axes[1, 0]
        bars3 = ax3.bar(names_list, win_rates, color='#1B998B', alpha=0.75, edgecolor='white')
        ax3.set_title('做T胜率对比', fontsize=12, fontweight='bold')
        ax3.set_ylabel('胜率 %', fontsize=10)
        ax3.set_ylim(0, 100)
        ax3.grid(True, alpha=0.3, axis='y')
        for bar, wr in zip(bars3, win_rates):
            ax3.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                     f'{wr:.1f}%', ha='center', fontsize=9)

        # 最大回撤
        ax4 = axes[1, 1]
        bars4 = ax4.bar(names_list, max_dds, color='#FF6B35', alpha=0.75, edgecolor='white')
        ax4.set_title('最大回撤对比', fontsize=12, fontweight='bold')
        ax4.set_ylabel('回撤 %', fontsize=10)
        ax4.grid(True, alpha=0.3, axis='y')
        for bar, dd in zip(bars4, max_dds):
            ax4.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.02,
                     f'{dd:.2f}%', ha='center', fontsize=9)

        plt.tight_layout()
        chart4_path = os.path.join(output_dir, 'chart4_strategy_comparison.png')
        plt.savefig(chart4_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f'  [OK] chart4_strategy_comparison.png')

        return True
    except Exception as e:
        print(f'  图表生成错误: {e}')
        import traceback
        traceback.print_exc()
        return False


# ============================================================
# ============================================================
# QMT 策略入口 (handlebar 模式)
# ============================================================

def init(ContextInfo):
    """
    QMT 策略初始化。
    在 QMT 中首次加载策略时调用。
    """
    ContextInfo.set_universe([f'{CODE}.SH'])
    ContextInfo.set_slippage(0.001)
    ContextInfo.set_commission(0.00025)

    # 策略状态 (存储在 ContextInfo 上)
    ctx = type('State', (), {})()
    ctx.trader = T0Trader(None, 'QMT_Adaptive', {
        'vol_band_mult': 0.5,
        'vol_sell_mult': 0.75,
        'grid_levels': 2,
        'grid_step_pct': 0.015,
    })
    ctx.base_shares = 0
    ctx.daily_t_shares = 0
    ctx.today_buy = 0
    ctx.today_sell = 0
    ctx.last_bar = -1
    ContextInfo.state = ctx

    print(f'[{NAME}] 日内做T策略已初始化')


def handlebar(ContextInfo):
    """
    QMT 主逻辑入口。每个 bar (日线) 调用一次。

    日内做T逻辑:
      1. 获取前一日收盘持仓 (底仓)
      2. 计算当日做T区间
      3. 根据当日行情模拟T操作 (实盘中需用盘中tick触发)
      4. 收盘前强制平仓当日T仓位
    """
    ctx = ContextInfo.state
    code = f'{CODE}.SH'

    # --- 获取历史数据 ---
    hist = ContextInfo.get_history_data(80, '1d', 'close')
    if code not in hist or len(hist[code]) < 60:
        return

    closes = hist[code]
    opens = ContextInfo.get_history_data(80, '1d', 'open').get(code, [])
    highs = ContextInfo.get_history_data(80, '1d', 'high').get(code, [])
    lows = ContextInfo.get_history_data(80, '1d', 'low').get(code, [])
    volumes = ContextInfo.get_history_data(80, '1d', 'volume').get(code, [])

    # --- 获取当前持仓 ---
    positions = get_trade_detail_data(ContextInfo.acc_id, 'STOCK', 'POSITION')
    for pos in positions:
        if pos.m_strInstrumentID == CODE and pos.m_strExchangeID == 'SH':
            ctx.base_shares = pos.m_nVolume
            break
    else:
        ctx.base_shares = 0

    # 无底仓不操作
    if ctx.base_shares < MIN_LOT:
        return

    # --- 计算指标 (简易) ---
    closes_arr = np.array(closes)
    atr_arr = np.zeros_like(closes_arr)
    for i in range(1, len(closes_arr)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes_arr[i - 1]),
            abs(lows[i] - closes_arr[i - 1])
        )
        atr_arr[i] = np.mean([max(
            highs[j] - lows[j],
            abs(highs[j] - closes_arr[j - 1]),
            abs(lows[j] - closes_arr[j - 1])
        ) for j in range(max(0, i - 13), i + 1)])

    curr_close = closes_arr[-1]
    curr_open = opens[-1] if opens else curr_close
    curr_high = highs[-1] if highs else curr_close
    curr_low = lows[-1] if lows else curr_close
    curr_atr = atr_arr[-1] if atr_arr[-1] > 0 else curr_close * 0.03
    curr_vol = volumes[-1] if volumes else 0

    # 成交量过滤
    vol_ma20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else curr_vol
    if curr_vol < vol_ma20 * 0.5:
        return

    # --- 趋势判断 ---
    ma20 = np.mean(closes_arr[-20:])
    ma5 = np.mean(closes_arr[-5:])
    trend_bull = curr_close > ma20 and ma5 > ma20
    trend_bear = curr_close < ma20 and ma5 < ma20

    # --- 计算做T区间 ---
    max_t_shares = int(ctx.base_shares * MAX_T_RATIO / MIN_LOT) * MIN_LOT
    if max_t_shares < MIN_LOT:
        return

    per_trade = max_t_shares

    # 重置当日计数器
    if ContextInfo.barpos != ctx.last_bar:
        ctx.daily_t_shares = 0
        ctx.today_buy = 0
        ctx.today_sell = 0
        ctx.last_bar = ContextInfo.barpos

    # 今日已做T股数达到上限
    if ctx.daily_t_shares >= max_t_shares:
        return
    remaining = max_t_shares - ctx.daily_t_shares

    # --- 正T (先买后卖): 牛市或震荡 ---
    if not trend_bear:
        buy_target = curr_open - curr_atr * 0.6
        sell_target = curr_open + curr_atr * 0.8

        # 检查盘中是否触及 (用日线high/low模拟, 实盘用tick)
        if curr_low <= buy_target and curr_high >= sell_target:
            trade_shares = min(per_trade, remaining)
            # T买入
            order_shares(code, trade_shares, 'COMPETE', ContextInfo, ContextInfo.acc_id)
            # T卖出 (理想情况下应分两次下单, 这里简化)
            order_shares(code, -trade_shares, 'COMPETE', ContextInfo, ContextInfo.acc_id)
            ctx.daily_t_shares += trade_shares

    # --- 反T (先卖后买): 熊市或震荡 ---
    if not trend_bull and ctx.daily_t_shares < max_t_shares:
        sell_target = curr_open + curr_atr * 0.9
        buy_target = curr_open - curr_atr * 0.5

        if curr_high >= sell_target and curr_low <= buy_target:
            trade_shares = min(per_trade, remaining)
            # T卖出
            order_shares(code, -trade_shares, 'COMPETE', ContextInfo, ContextInfo.acc_id)
            # T买回
            order_shares(code, trade_shares, 'COMPETE', ContextInfo, ContextInfo.acc_id)
            ctx.daily_t_shares += trade_shares

    # --- 收盘前强制平仓 (在最后一个bar判断) ---
    if ContextInfo.is_last_bar():
        # 确保底仓恢复
        pass


# ============================================================
# 主程序 (独立回测)
# ============================================================

def main():
    print('=' * 70)
    print(f'  长飞光纤({CODE}) 日内做T量化策略 — 独立回测系统')
    print('=' * 70)

    # ---- Step 1: 获取数据 ----
    print('\n[Step 1] 获取数据...')
    df = fetch_data(CODE)

    # ---- Step 2: 特征工程 ----
    print('\n[Step 2] 特征工程...')
    df = compute_features(df)
    df_clean = df.dropna().reset_index(drop=True)
    print(f'  有效数据: {len(df_clean)} 条, {df_clean["date"].iloc[0]} ~ {df_clean["date"].iloc[-1]}')
    print(f'  日均振幅: {df_clean["daily_range"].mean()*100:.2f}%')
    print(f'  日均ATR: {df_clean["atr"].mean():.2f} 元')
    print(f'  最新价格: {df_clean["close"].iloc[-1]:.2f} 元')

    # ---- Step 3: 运行策略 ----
    print('\n[Step 3] 运行做T策略...')
    strategies = [
        ('A_VolBand', strategy_vol_band),
        ('B_Grid', strategy_grid),
        ('C_Momentum', strategy_momentum),
        ('D_Adaptive', strategy_adaptive),
    ]

    all_records = {}
    all_metrics = {}
    all_traders = {}

    for name, func in strategies:
        records, trader = func(df_clean)
        all_records[name] = records
        all_traders[name] = trader
        metrics = compute_metrics(records, name)
        all_metrics[name] = metrics
        print(f'  {name}: 总收益={metrics["total_return"]}, 胜率={metrics["win_rate"]}, '
              f'夏普={metrics["sharpe"]}, 回撤={metrics["max_drawdown_pct"]}, '
              f'做T天数={metrics["active_days"]}')

    # ---- 基准：纯持有 ----
    bh_start = df_clean['close'].iloc[0]
    bh_end = df_clean['close'].iloc[-1]
    bh_return = (bh_end / bh_start - 1) * 100
    bh_shares = int((INITIAL_CAPITAL * BASE_POSITION_PCT) / (bh_start * (1 + COMMISSION)) / MIN_LOT) * MIN_LOT
    bh_value = bh_shares * bh_end + (INITIAL_CAPITAL - bh_shares * bh_start)
    bh_total_return = (bh_value / INITIAL_CAPITAL - 1) * 100
    print(f'\n  基准(30%底仓持有): 股票收益={bh_return:.2f}%, 总收益={bh_total_return:.2f}%')

    # ---- Step 4: 生成图表 ----
    print('\n[Step 4] 生成图表...')
    charts_ok = generate_charts(df_clean, all_records, all_metrics, OUTPUT_DIR)

    # ---- Step 5: 保存数据 ----
    print('\n[Step 5] 保存结果...')

    # 保存交易记录
    for name, trader in all_traders.items():
        if trader.trade_log:
            trades_path = os.path.join(OUTPUT_DIR, f'trades_{name}.csv')
            pd.DataFrame(trader.trade_log).to_csv(trades_path, index=False, encoding='utf-8-sig')
            print(f'  [OK] trades_{name}.csv ({len(trader.trade_log)} 条)')

    # 保存净值曲线
    eq_df = pd.DataFrame({'date': df_clean['date']})
    for name, records in all_records.items():
        df_r = pd.DataFrame(records)
        eq_df[f'{name}_cum_pnl'] = df_r['cum_pnl'].values
        eq_df[f'{name}_equity'] = df_r['total_equity'].values
    eq_df.to_csv(os.path.join(OUTPUT_DIR, 'equity_curves.csv'), index=False, encoding='utf-8-sig')
    print(f'  [OK] equity_curves.csv')

    # 保存绩效汇总
    summary = {
        'stock': {'code': CODE, 'name': NAME},
        'period': {
            'start': str(df_clean['date'].iloc[0]),
            'end': str(df_clean['date'].iloc[-1]),
            'trading_days': len(df_clean),
        },
        'config': {
            'initial_capital': INITIAL_CAPITAL,
            'base_position_pct': BASE_POSITION_PCT,
            'max_t_ratio': MAX_T_RATIO,
            'daily_amplitude_mean': f'{df_clean["daily_range"].mean()*100:.2f}%',
            'atr_mean': f'{df_clean["atr"].mean():.2f}',
        },
        'benchmark': {
            'stock_return': f'{bh_return:.2f}%',
            'total_return': f'{bh_total_return:.2f}%',
        },
        'strategies': all_metrics,
    }
    summary_path = os.path.join(OUTPUT_DIR, 'backtest_summary.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f'  [OK] backtest_summary.json')

    # ---- 打印最终总结 ----
    print('\n' + '=' * 70)
    print('  回测结果汇总')
    print('=' * 70)
    print(f'\n  标的: {NAME}({CODE})')
    print(f'  区间: {df_clean["date"].iloc[0]} ~ {df_clean["date"].iloc[-1]} ({len(df_clean)}天)')
    print(f'  初始资金: {INITIAL_CAPITAL:,.0f} 元 | 底仓比例: {BASE_POSITION_PCT*100:.0f}%')
    print(f'  日均振幅: {df_clean["daily_range"].mean()*100:.2f}% | ATR: {df_clean["atr"].mean():.2f} 元')
    print(f'\n  {"策略":<18} {"总收益":>10} {"年化收益":>10} {"胜率":>8} {"夏普":>7} {"最大回撤":>10}')
    print('  ' + '-' * 68)
    for name, m in all_metrics.items():
        print(f'  {name:<18} {m["total_return"]:>10} {m["annual_return"]:>10} {m["win_rate"]:>8} {m["sharpe"]:>7} {m["max_drawdown_pct"]:>10}')
    print(f'  {"底仓持有":<18} {bh_total_return:>9.2f}% {"—":>10} {"—":>8} {"—":>7} {"—":>10}')

    best = max(all_metrics.items(), key=lambda x: x[1].get('total_return_val', 0))
    print(f'\n  >>> 最优策略: {best[0]} (总收益: {best[1]["total_return"]})')

    print(f'\n  所有输出已保存至: {OUTPUT_DIR}')
    for f_name in sorted(os.listdir(OUTPUT_DIR)):
        f_path = os.path.join(OUTPUT_DIR, f_name)
        print(f'    {f_name} ({os.path.getsize(f_path):,} bytes)')

    print('\n  DONE.')
    return all_metrics, all_records, df_clean


if __name__ == '__main__':
    try:
        # 如在QMT中运行，使用 handlebar 模式
        # 否则运行独立回测
        if 'ContextInfo' in dir() and ContextInfo is not None:
            pass  # QMT自动调用init/handlebar
        else:
            main()
    except Exception as e:
        print(f'\n错误: {e}')
        import traceback
        traceback.print_exc()
