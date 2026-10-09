# -*- coding: utf-8 -*-
"""
core/config.py — 策略参数常量 + 时间工具函数
=============================================
所有可调参数集中管理。工具函数纯计算, 无副作用。
"""
import time as _time

from matplotlib.pylab import cond

# ============================================================================
# 标的 & 账户
# ============================================================================
ACCOUNT    = '8890145315'
STOCK_CODE     = '601869'
STOCK_NAME     = '长飞光纤'
STOCK_QMT      = f'{STOCK_CODE}.SH'
STRATEGY_NAME  = 'Daily trading buy and sell'

TRADE_LOT_SIZE = 100
MIN_LOT        = 100

TRADE_LOT_SIZE_MOM = 100
# ============================================================================
# 技术指标参数
# ============================================================================
ATR_PERIOD = 14

# ============================================================================
# 反T (short) 参数
# ============================================================================
SELL_TRIGGER_BASE_BEAR      = 0.40
SELL_TRIGGER_BASE_SIDEWAYS  = 0.55
SELL_TRIGGER_BASE_WEAK_BULL = 0.65

# 反T触发价缩放系数: 作用于"涨幅"部分 (curr_atr_pct × sell_mult)。
# <1.0 下调触发价, 使阈值更易触发; >1.0 上调。0.50 = 涨幅整体下调 50%。
SELL_TRIGGER_SCALE = 0.68

DYNAMIC_MULT_MIN = 0.20
DYNAMIC_MULT_MAX = 1.50

DAILY_RANGE_CAP_ENABLED = True
DAILY_RANGE_CAP_MULT    = 0.80

PULLBACK_PCT        = 0.0010
BUYBACK_TRIGGER_MULT = 0.15
BOUNCE_PCT          = 0.0010
BUYBACK_TIGHTEN_MULT = 0.60

VOLUME_FILTER_RATIO = 0.4
RSI_OVERBOUGHT      = 75
STRONG_BULL_RSI     = 70
STRONG_BULL_STREAK  = 5

EMERGENCY_BUYBACK = False
EMERGENCY_BUYBACK_PCT = 0.03
STOP_LOSS_PCT         = 0.015

FORCE_CLOSE_TIME   = '14:57:00'
ENABLE_FORCE_CLOSE = False

# ============================================================================
# 正T (long) 参数
# ============================================================================
BUY_TRIGGER_PCT   = 0.030
BUY_TRIGGER_TRAIL = 0.020
SELLBACK_RISE_PCT = 0.012

# ============================================================================
# 锁仓参数
# ============================================================================
LOCK_PRICE_RATIO  = 0.015       # 价格超过开盘价1.5% cond1
LOCK_MOMENTUM_PCT = 0.010       # 价格浮动1.0% cond2
LOCK_DRAWDOWN_PCT = 0.008
LOCK_LOOKBACK_SEC = 300         # 5分钟内的价格
LOCK_COOLDOWN_SEC = 60         # 锁仓60s

# ============================================================================
# 仓位管理
# ============================================================================
MAX_POSITION_LOTS = 5
MIN_POSITION_LOTS = 0
MAX_DAILY_TRADES  = 5

# ============================================================================
# MiniQMT 连接
# ============================================================================
MINIQMT_PATH = 'C:/QMT/userdata_mini'
SESSION_ID   = 0

# ============================================================================
# 数据 & 费率
# ============================================================================
HIST_DATA_LEN = 80
COMMISSION    = 0.00025
STAMP_TAX     = 0.001

# ============================================================================
# 状态机常量
# ============================================================================
STATE_IDLE       = 'IDLE'
STATE_SPIKING    = 'SPIKING'
STATE_SOLD       = 'SOLD'
STATE_DIPPING    = 'DIPPING'
STATE_DONE       = 'DONE'
STATE_FORCED     = 'FORCED'
STATE_BT_DIPPING = 'BT_DIPPING'
STATE_BT_BOUGHT  = 'BT_BOUGHT'
STATE_BT_SPIKING = 'BT_SPIKING'


# ============================================================================
# 时间工具函数 (纯函数, 无副作用)
# ============================================================================

def now_hms():
    """返回当前 HH:MM:SS"""
    return _time.strftime('%H:%M:%S')


def ts_prefix():
    """返回 [HH:MM:SS] 日志前缀"""
    return _time.strftime('[%H:%M:%S]')


def is_market_open(now_str=None):
    """判断是否在A股连续竞价时段 (9:30-11:30, 13:00-15:00)"""
    if now_str is None:
        now_str = now_hms()
    return ('09:30:00' <= now_str <= '11:30:00') or ('13:00:00' <= now_str <= '15:00:00')


def time_to_open(now_str):
    """计算距离开盘的剩余时间, 返回可读字符串"""
    h, m, s = int(now_str[:2]), int(now_str[3:5]), int(now_str[6:8])
    now_secs = h * 3600 + m * 60 + s

    if now_str < '09:30:00':
        target = 9 * 3600 + 30 * 60
    elif '11:30:00' < now_str < '13:00:00':
        target = 13 * 3600
    else:
        return '--'

    remain = target - now_secs
    if remain > 3600:
        return f'{remain // 3600}时{(remain % 3600) // 60}分'
    elif remain > 60:
        return f'{remain // 60}分{remain % 60}秒'
    else:
        return f'{remain}秒'
