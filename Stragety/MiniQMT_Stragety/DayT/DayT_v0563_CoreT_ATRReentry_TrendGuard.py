# -*- coding: utf-8 -*-
"""v0563 CoreT — v0562 plus CaptureT_v4's daily trend guard over the base position.

v0562 is the intraday core: reverse-T / forward-T, ATR re-entry, daily reset, no
stop-loss, no end-of-day force close.  Its whole risk model lives *inside* the
session — and that is exactly the gap CaptureT_v4 was built to close.

CaptureT_v4 showed, on the same symbol and the same window, that the size of the
lever is not the intraday T at all:

  * 601869 fell 579.71 (06-24) → 262 (08-03), a −54.8% drawdown.  An 800-share
    base lost about 250,000 yuan in that move.
  * v1's full-year intraday edge was 12,701.68 yuan.  One daily overlay that
    flattened the base for 26 sessions was worth +88,331.57 versus buy-and-hold.

So this file fuses that overlay — CaptureT_v4's *outer* layer, unchanged in
mechanism — onto the v0562 intraday core.  The two layers run at deliberately
different frequencies and on different data:

  ┌─ 外层：日线风控开关 ───────────────────────────── 每天一次 ─┐
  │  数据：日线 OHLC（严格早于今日）                            │
  │  决定：今天底仓该不该在（risk_on ∈ {True, False}）          │
  │  动作：清仓（RISK-OFF sell）/ 回场（RISK-RESTORE buy）      │
  ├─ 内层：v0562 日内做T ─────────────────────────── 每分钟一次 ─┤
  │  数据：当日分钟线 / tick                                    │
  │  决定：反T、正T 的触发                                      │
  │  动作：REV-T / FWD-T 开平仓                                 │
  └─────────────────────────────────────────────────────────────┘

外层是主：risk_on = False 的交易日，内层整天不启动（没有底仓可卖）。

── 开关本身（与 CaptureT_v4 完全一致）──
三个量，全部只用「截至昨日」的日线：

  * ATR(14)  = 近 14 日 (high − low) 均值           ← 波动尺度
  * peak     = 历史最高**收盘价**                    ← 跟踪止盈的锚
  * MA20     = 近 20 日收盘均值                      ← 回场的门槛

状态机（两个状态）：

  risk_on = True  → 昨收 < peak − K_ATR × ATR  → 清仓，risk_on = False
  risk_on = False → 昨收 > MA20                → 买回，risk_on = True

**出场用 ATR 跟踪止损，进场用均线**。这是不对称设计，有意为之：出场要快，
跌破就认；进场要慢，必须站上均线才算企稳，避免下跌中途反复接刀。

── 为什么这样能塞进「每日重置」架构 ──
本系列每个交易日重建策略实例，任何跨日状态都会丢。v4 的解法是：**把开关
状态定义成日线序列的纯函数，不存，每天早上从日线重放出来。** 同一个循环
（`_update_risk_switch`）在每天开盘前跑一次，得到的 risk_on 就是今天该用的值。

── 无未来函数 ──
`load_daily_snapshot` 在回放框架里自带断言，保证返回的最后一根日线**严格早于
当日**；信号在开盘前算完，订单在当日第一笔可交易 tick 成交；每个交易日的开关
只落地一次（`risk_switch_done` 标记）。

── 风控腿不进 ExecutionBook ──
`ExecutionBook` 随每日重置一起重建，而风控的一条腿可能跨好几天（今天清仓、
下周才回场）。昨天的 RISK-OFF 记录今天已不存在，RISK-RESTORE 会被判成「平掉
不存在的腿」直接抛错。风控的盈亏本来就体现在账户权益里，不需要也不应该由 T
台账记账。

Kept verbatim from v0562: compute_signal (ATR / open-anchored sell trigger /
do_short gate / range cap), confirmed_short_reversal, calculate_atr_reentry, the
REV-T ladder IDLE→SPIKING→SOLD→DIPPING and the FWD-T ladder
IDLE→BT_DIPPING→BT_BOUGHT→BT_SPIKING, whole-lot sizing via calculate_t_shares,
and the daily-reset lifecycle.
"""
SHORT_NEW_ENTRY_CUTOFF = '14:20:00'
SHORT_CONFIRM_MIN_BARS = 2
SHORT_CONFIRM_MIN_EXTENSION_PCT = 0.0015
SHORT_CONFIRM_MIN_PULLBACK_PCT = 0.0020
# 第二轮及以后反T上行系数的独立缩放；1=原值，0.8=卖出距离缩短20%，须大于0。
# 不叠乘首轮系数；不改变正T买入阈值或已成交交易的退出目标。
REENTRY_UP_UNITS_SCALE = 0.80

# ── 外层：日线趋势风控开关（融合自 CaptureT_v4_TrendGuarded）──
# 关键设计：开关状态是**日线序列的纯函数**，不需要跨日保存。
# 1.0 = 打开外层；0 值/False 只用于研究对照（此时退化为纯 v0562）。
RISK_LAYER_ENABLED = True
RISK_ATR_PERIOD = 14      # 教科书默认
RISK_K_ATR = 3.0          # 教科书默认；LAB 扫过 1.0~6.0，K>=2.0 整段为正
RISK_MA_REENTRY = 20      # 回场条件：收盘重新站上 MA20
# peak 的语义。两种读法回答的是不同的问题，默认沿用 CaptureT_v4 原版：
#   'MARKET'  —— peak = 重放窗口内收盘价的运行最高值，**永不重置**。
#                读作「这个市场离它自己的高点有多远」：一个 regime 锚。
#                必须是不可衰减的 —— 一旦改成滚动窗口，下跌时 peak 会跟着
#                价格下移，止损线永远追不上，止损就再也不会触发。
#   'REENTRY' —— peak 在每次「空仓 → 回场」时重置为回场当日的收盘价。
#                读作「当前这手持仓的高水位」：清仓后再回场是一个新仓位，
#                它的回撤应该从自己的成本算起，而不是从上一个仓位的顶算起。
#                与内层 ATR re-entry 的「用真实成交价重锚」是同一个思路。
# 触发差异：崩盘那一次两边完全相同（那时仓位是崩盘前开的，高水位就是当时的顶）；
# 只有「回场之后」的行为不同 —— MARKET 会因 stale peak 与前高之间反复翻仓，
# REENTRY 不会，但代价是离场要等更深的回撤。见 analysis/timing_lab_peak_mode_20260921/。
#   'ROLLING' —— peak = 近 `RISK_PEAK_WINDOW` 个交易日收盘价的最高值（滚动窗口）。
#                读作「最近一个月的高点」，是一个**短期**锚。它把止损从
#                「离历史顶多远」改成「最近有没有快速下跌」：因为窗口内的最高值
#                会随价格一起下移，只有比窗口更快的下跌才会触发。
#                代价很明确 —— 阴跌（比窗口慢的下跌）它一路跟着走，永远不触发。
RISK_PEAK_MODE = 'MARKET'
# 仅 'ROLLING' 使用。20 个交易日 ≈ 一个自然月。
RISK_PEAK_WINDOW = 20
RISK_PEAK_MODES = ('MARKET', 'REENTRY', 'ROLLING')
RISK_DAILY_LOOKBACK = 280  # 取多少根日线来重放开关（须覆盖任意长度的清仓期）
# 风控回场时买回多少股；必须与研究口径下的底仓一致。回放脚本按初始持仓覆盖。
BASE_TARGET_SHARES = 800

import math
import os
import sys
import time as _time
import traceback as _traceback
from datetime import datetime

import numpy as np
import pandas as pd

# Permit the documented ``python Stragety/.../DayT_v0563_CoreT_ATRReentry_TrendGuard.py`` command.
_STRATEGY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(_STRATEGY_ROOT))
for _module_root in (_REPOSITORY_ROOT, _STRATEGY_ROOT):
    if _module_root not in sys.path:
        sys.path.insert(0, _module_root)

from core import config as cfg
from core.signals import compute_signal
from core.t_position_size import calculate_t_shares
from core.execution_book import ExecutionBook
from core.atr_reentry import calculate_atr_reentry
from core.peak_mode import parse_peak_mode
from Stragety.MiniQMT_Stragety.DayT.infra.logger import (
    FileLogger, set_logger, get_logger, _log, _log_file_only,
)
from Stragety.MiniQMT_Stragety.DayT.infra.connector import (
    MiniQMTConnector, MockContextInfo,
    get_trade_detail_data, order_shares, set_global_conn,
)

ACCOUNT = cfg.ACCOUNT
TRADE_LOT_SIZE = cfg.TRADE_LOT_SIZE
STATE_IDLE = cfg.STATE_IDLE; STATE_SPIKING = cfg.STATE_SPIKING
STATE_SOLD = cfg.STATE_SOLD; STATE_DIPPING = cfg.STATE_DIPPING
STATE_DONE = cfg.STATE_DONE; STATE_FORCED = cfg.STATE_FORCED
STATE_BT_DIPPING = cfg.STATE_BT_DIPPING; STATE_BT_BOUGHT = cfg.STATE_BT_BOUGHT
STATE_BT_SPIKING = cfg.STATE_BT_SPIKING

# 成交判定
FILL_TIMEOUT_SEC = 8.0   # 等待满额成交的超时秒数
TERMINAL_ORDER_STATUSES = (53, 54, 56, 57)

# 风控开关的两条腿**不是 T 腿**，不进 ExecutionBook（理由见文件头）。
RISK_LABELS = ('RISK-OFF sell', 'RISK-RESTORE buy')

# 新开一笔做T的目标金额；高价股不足一手时仍按一手，不是硬性资金上限。
T_TARGET_VALUE = 40000.0
# 最多使用未被其他正T占用的可卖底仓的此比例；不足一手但有整手时按一手。
T_POSITION_FRACTION = 0.40
# 单笔股数覆盖，键为完整代码，例如 {'600000.SH': 100}。
SYMBOL_LOT_OVERRIDES = {}


def confirmed_short_reversal(trigger, peak, price, armed_bars,
                             minimum_bars=SHORT_CONFIRM_MIN_BARS,
                             minimum_extension_pct=SHORT_CONFIRM_MIN_EXTENSION_PCT,
                             minimum_pullback_pct=SHORT_CONFIRM_MIN_PULLBACK_PCT):
    """Require time, excess extension and a material peak reversal."""
    if trigger <= 0 or peak <= 0 or price <= 0:
        return False
    if armed_bars < minimum_bars:
        return False
    extension = peak / trigger - 1.0
    pullback = (peak - price) / peak
    return extension >= minimum_extension_pct and pullback >= minimum_pullback_pct


def calculate_execution_capacity(base_can_use, available_cash, price,
                                 max_daily_trades, lot_size=TRADE_LOT_SIZE):
    """Calculate executable REV/FWD lots; zero lots are never enabled."""
    sellable_shares = max(0, int(base_can_use or 0))
    sellable_lots = sellable_shares // lot_size
    cash_lots = 0
    if price and price > 0:
        cash_lots = int(float(available_cash or 0) /
                        (float(price) * lot_size * 1.01))
    short_lots = min(sellable_lots, max_daily_trades)
    long_lots = min(cash_lots, sellable_lots, max_daily_trades)
    can_short = short_lots >= 1
    can_long = long_lots >= 1

    short_reason = '' if can_short else 'sellable {} sh < {} sh'.format(
        sellable_shares, lot_size)
    long_reasons = []
    if cash_lots < 1:
        long_reasons.append('insufficient cash for 1 lot')
    if sellable_lots < 1:
        long_reasons.append(
            'T+1: sellable base shares {} sh < {} sh'.format(
                sellable_shares, lot_size))
    return {
        'short_lots': short_lots,
        'long_lots': long_lots,
        'cash_lots': cash_lots,
        'sellable_lots': sellable_lots,
        'can_short': can_short,
        'can_long': can_long,
        'short_reason': short_reason,
        'long_reason': '; '.join(long_reasons),
    }


def scale_reentry_signal(signal):
    """Scale the next-cycle sell distance from its original units, never cumulatively."""
    if not signal or signal.get('trigger_base') != 'CLOSE_FILL_ATR':
        return signal
    scale = float(REENTRY_UP_UNITS_SCALE)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('REENTRY_UP_UNITS_SCALE must be finite and greater than zero')
    result = signal['reentry']
    raw = result.setdefault('unscaled_up_units', result['up_units'])
    units = raw * scale
    base, atr_pct = result['base'], result['atr_pct']
    trigger = round(math.ceil((base + max(base * atr_pct * units, 0.01)) /
                              0.01 - 1e-9) * 0.01, 2)
    result.update(up_units=units, up_units_scale=scale, sell_trigger=trigger)
    signal.update(sell_trigger=trigger, sell_trigger_raw=trigger,
                  atr_pct=atr_pct, trigger_units=units,
                  trigger_pct=atr_pct * units, sell_mult=units,
                  sell_mult_base=units)
    return signal


def replay_risk_switch(highs, lows, closes, peak_mode=None):
    """把「ATR 跟踪止损 + MA20 回场」开关从日线序列里**重放**出来。

    这是整个外层的全部状态：纯函数，同样的日线序列永远给同样的答案，
    所以不需要跨日保存任何东西。入参必须是**严格早于今日**的日线。
    返回 None 表示样本不足或含非有限值（调用方按 fail-open 处理）。

    `peak_mode` 默认取模块常量 `RISK_PEAK_MODE`；三种语义的差别见该常量处的说明。
    """
    kind, window = parse_peak_mode(
        RISK_PEAK_MODE if peak_mode is None else peak_mode, RISK_PEAK_WINDOW)
    need = max(RISK_ATR_PERIOD, RISK_MA_REENTRY) + 1
    try:
        high = np.asarray(highs, dtype=float)
        low = np.asarray(lows, dtype=float)
        close = np.asarray(closes, dtype=float)
    except (TypeError, ValueError):
        return None
    n = len(close)
    if (n < need or len(high) != n or len(low) != n or
            not (np.isfinite(high).all() and np.isfinite(low).all() and
                 np.isfinite(close).all())):
        return None
    # A non-positive price makes peak/MA20 meaningless, so treat it as missing
    # data rather than replaying a switch off a corrupt bar.
    if np.any(close <= 0) or np.any(high <= 0) or np.any(low <= 0):
        return None

    # 逐步重放：ATR / MA20 都只看当日及之前；peak 的口径由 kind 决定。
    risk_on, peak = True, float(close[0])
    reason = 'INIT 满仓'
    for i in range(n):
        if kind == 'ROLLING':
            # 滚动窗口最高收盘：窗口随价格下移，只有比窗口更快的下跌才触发。
            peak = float(close[max(0, i - window + 1):i + 1].max())
        else:
            peak = max(peak, float(close[i]))
        if i < max(RISK_ATR_PERIOD, RISK_MA_REENTRY):
            continue
        atr = float((high[i - RISK_ATR_PERIOD + 1:i + 1] -
                     low[i - RISK_ATR_PERIOD + 1:i + 1]).mean())
        ma = float(close[i - RISK_MA_REENTRY + 1:i + 1].mean())
        if risk_on:
            if atr > 0 and close[i] < peak - RISK_K_ATR * atr:
                risk_on = False
                reason = ('RISK-OFF Y{:.2f} < peak Y{:.2f} - {:.1f}xATR '
                          'Y{:.2f}'.format(close[i], peak, RISK_K_ATR,
                                           peak - RISK_K_ATR * atr))
        elif close[i] > ma:
            risk_on = True
            if kind == 'REENTRY':
                # 新仓位从它自己的入场价开始记高水位，不继承上一手的顶。
                peak = float(close[i])
            reason = 'RE-ENTRY Y{:.2f} > MA20 Y{:.2f}'.format(close[i], ma)

    return {
        'risk_on': risk_on, 'reason': reason, 'peak': peak,
        'peak_mode': peak_mode or RISK_PEAK_MODE, 'peak_window': window,
        'atr': float((high[-RISK_ATR_PERIOD:] - low[-RISK_ATR_PERIOD:]).mean()),
        'ma20': float(close[-RISK_MA_REENTRY:].mean()),
        'daily_close': float(close[-1]),
    }


def resolve_signal_open(now_hms, tick_open, latest_complete_close):
    """Choose the signal open price and describe its source for logging."""
    after_hours = now_hms < '09:30:00' or now_hms >= '15:00:00'
    latest_close = float(latest_complete_close or 0.0)
    if after_hours and latest_close > 0:
        return latest_close, 'AFTER-HOURS latest_complete_close'
    tick_price = float(tick_open or 0.0)
    if tick_price > 0:
        return tick_price, 'TICK_OPEN'
    return latest_close, 'LATEST_COMPLETE_CLOSE fallback'


def format_signal_base_source(source):
    """Return a concise, operator-facing label for the signal base price."""
    source_labels = {
        'AFTER-HOURS latest_complete_close':
            'latest complete close; after-hours',
        'TICK_OPEN': 'today open tick',
        'LATEST_COMPLETE_CLOSE fallback': 'latest complete close; tick-open fallback',
    }
    return source_labels.get(source, str(source or 'unknown'))


class StrategyRunner:
    """One symbol, one position, one state machine. No stop-loss, no carry."""

    def __init__(self, portfolio, stock_qmt, stock_name=''):
        self.portfolio = portfolio
        self.stock_qmt = stock_qmt
        self.stock_code = stock_qmt.split('.')[0]
        self.stock_name = stock_name or stock_qmt
        self.trade_lot = SYMBOL_LOT_OVERRIDES.get(
            stock_qmt, 200 if self.stock_code.startswith(('688', '689')) else 100)
        self.version = 'v0563'
        self.conn = SymbolConnector(portfolio.conn, stock_qmt)
        self.ctx = MockContextInfo(self.conn)
        self.st = self.ctx.st
        self.dry_run = portfolio.dry_run
        self._running = True
        self._last_heartbeat = 0.0
        self.total_t_days = 0
        self.total_pnl = 0.0
        self.execution_book = ExecutionBook()
        self._execution_price = None
        self._submitted_order_id = None
        self._last_buyback_price = 0.0
        self._last_cycle_gross = 0.0
        self._last_executed_order = None

    # ═══ 日志 ═══

    def _log(self, message):
        _log('[{}]{}'.format(self.stock_code, message))

    def _file_log(self, message):
        _log_file_only('[{}]{}'.format(self.stock_code, message))

    def has_open_legs(self):
        return (any(self.execution_book.legs.values()) or
                bool(self.st.get('short_legs') or self.st.get('long_legs')) or
                self.st.get('fstate') in (STATE_SOLD, STATE_DIPPING,
                                          STATE_BT_BOUGHT, STATE_BT_SPIKING))

    # ═══ 状态 ═══

    def _init_state(self):
        self.st.update({
            'daily_signal': None, 'base_shares': 0, 'base_can_use': 0,
            'base_cost': 0.0, 'entry_price': 0.0, 'fstate': STATE_IDLE,
            'peak_price': 0.0, 'dip_price': 0.0,
            'sell_fill_price': 0.0, 'buyback_target': 0.0,
            'buyback_target_pct': 0.0, 'day_pnl': 0.0,
            'total_t_days': self.total_t_days, 'total_pnl': self.total_pnl,
            'trade_date': '', 'initialized': False,
            'init_attempts': 0, 'last_init_time': 0.0,
            'state_enter_time': '', 'sell_elapsed_bars': 0,
            'locked': False, 'lock_reason': '', 'lock_since': '',
            'short_arm_bars': 0, 'short_arm_trigger': 0.0,
            'next_t_cycle': 0, 'short_legs': [], 'long_legs': [],
            'reentry_pending': None, 'reentry_history': None,
            'bt_dip_price': 0.0, 'bt_buy_trigger': 0.0,
            'bt_buy_fill_price': 0.0, 'bt_sellback_target': 0.0,
            'bt_max_trail': 0.0, 'bt_sell_peak_price': 0.0,
            'do_short': False, 'do_long': False,
            'short_reason': '', 'long_reason': '',
            'short_signal_allowed': False, 'short_signal_reason': '',
            'avail_cash': 0.0, 'pos_value': 0.0, 'pos_pct': 0.0,
            'short_lots': 0, 'long_lots': 0,
            'trade_count_short': 0, 'trade_count_long': 0,
            '_market_open_logged': False,
            # ── 外层：日线风控开关 ──
            'risk_on': True, 'risk_ready': False, 'risk_reason': '',
            'risk_switch_done': '', 'risk_atr': 0.0, 'risk_peak': 0.0,
            'risk_ma20': 0.0, 'risk_daily_close': 0.0, 'risk_asof': '',
            'risk_liquidate_shares': 0, 'risk_restore_shares': 0,
        })

    def _lock_all_trading(self, reason):
        """Fail closed when the daily-data initialization path is unhealthy."""
        self.st['daily_signal'] = {}
        self.st['do_short'] = False
        self.st['do_long'] = False
        self.st['initialized'] = False
        self.st['locked'] = True
        self.st['lock_reason'] = reason
        self.st['lock_since'] = cfg.now_hms()
        self._log('[TRADE-LOCK] {}; all trading disabled'.format(reason))

    def _new_leg_block_reason(self):
        if self.portfolio.order_uncertain:
            return 'account order outcome uncertain'
        if not self.st.get('risk_on', True):
            return 'RISK-OFF 底仓已由日线风控清空: {}'.format(
                self.st.get('risk_reason', ''))
        if self.st.get('reentry_pending'):
            return 'next-T awaiting confirmed closing price / valid ATR history'
        if self.st.get('locked', False):
            return self.st.get('lock_reason', 'strategy locked')
        return ''

    # ═══ 外层：日线趋势风控开关（融合自 CaptureT_v4）═══

    def _load_risk_daily(self, today, tick_data, last_close):
        """取**严格早于今日**的日线。回放里 load_daily_snapshot 自带这个断言。"""
        self.conn.refresh_daily_cache()
        try:
            snapshot = self.conn.load_daily_snapshot(
                RISK_DAILY_LOOKBACK, today=today, tick_last_close=last_close,
                tick_time=tick_data.get('timetag') or tick_data.get('time'),
                retries=3, retry_delay=1.0)
        except Exception as error:
            self._log('[RISK-DAILY] {}'.format(error))
            return None
        if not snapshot:
            return None
        frame = snapshot.get('adjusted')
        if frame is None:
            frame = snapshot.get('raw')
        need = max(RISK_ATR_PERIOD, RISK_MA_REENTRY) + 5
        if frame is None or len(frame) < need:
            return None
        return frame

    def _risk_unavailable(self, why):
        """Fail **open**: a missing daily feed must not liquidate the base."""
        self.st.update({'risk_ready': False, 'risk_on': True,
                        'risk_reason': 'DATA-UNAVAILABLE fail-open ({})'.format(why)})
        self._log('[RISK] 日线不可用（{}）；risk_on=True，今日不加保护'.format(why))

    def _update_risk_switch(self, today, tick_data, last_close):
        """开盘前跑一次：从日线**重放**出今天的开关状态。

        无未来函数：frame 的最后一根严格早于今日，不含今日。
        无跨日状态：整条开关历史每天早上由日线重新推出来。
        """
        st = self.st
        if not RISK_LAYER_ENABLED:
            st.update({'risk_ready': True, 'risk_on': True,
                       'risk_reason': 'LAYER-DISABLED (research control)'})
            return
        frame = self._load_risk_daily(today, tick_data, last_close)
        if frame is None:
            self._risk_unavailable('daily snapshot rejected')
            return
        result = replay_risk_switch(
            frame['high'].tolist(), frame['low'].tolist(),
            frame['close'].tolist(), peak_mode=RISK_PEAK_MODE)
        if result is None:
            self._risk_unavailable('only {} usable daily bars'.format(len(frame)))
            return
        st.update({'risk_ready': True, 'risk_on': result['risk_on'],
                   'risk_reason': result['reason'], 'risk_atr': result['atr'],
                   'risk_peak': result['peak'], 'risk_ma20': result['ma20'],
                   'risk_peak_mode': result['peak_mode'],
                   'risk_daily_close': result['daily_close'],
                   'risk_asof': str(frame.index[-1])})

    def _apply_risk_switch(self, price):
        """把开关状态落到仓位上，本日只调用一次。返回 True 表示已下达风控动作。"""
        st = self.st
        if st.get('locked', False):
            # 初始化不健康时交易已整体关闭，风控腿也不能例外。
            self._log('[RISK] 策略已锁仓（{}），不执行风控动作'.format(
                st.get('lock_reason', '')))
            return False
        risk_on = bool(st.get('risk_on', True))
        holding = int(st.get('base_shares', 0)) > 0
        if not risk_on and holding:
            self._refresh_position()
            can_use = int(st.get('base_can_use', 0))
            if can_use >= self.trade_lot:
                self._log('[RISK] {} → 清仓卖出 {} 股'.format(
                    st.get('risk_reason', ''), can_use))
                self._submit_order(-can_use, price, 'RISK-OFF sell')
                st['risk_liquidate_shares'] = can_use
            else:
                self._log('[RISK] risk-off 但可卖 {} 股 < 1 手，无法清仓'.format(
                    can_use))
            return True
        if risk_on and not holding:
            shares = int(BASE_TARGET_SHARES)
            if shares >= self.trade_lot:
                self._log('[RISK] {} → 回场买回 {} 股'.format(
                    st.get('risk_reason', ''), shares))
                self._submit_order(shares, price, 'RISK-RESTORE buy')
                st['risk_restore_shares'] = shares
            else:
                self._log('[RISK] risk-on 但 BASE_TARGET_SHARES={} 不足一手，'
                          '不回场'.format(shares))
            return True
        return False

    # ═══ 每日初始化 ═══

    def _daily_init(self):
        """Daily reset: fresh state, fresh signal, fresh capacity. Nothing carries."""
        today = datetime.now().strftime('%Y%m%d')
        if (self.st.get('trade_date', '') == today and
                self.st.get('initialized', False)):
            self._refresh_position()
            return
        self._init_state()
        self.st['trade_date'] = today

        tick_data = self.ctx.get_full_tick([self.stock_qmt]).get(self.stock_qmt, {})
        today_open = float(tick_data.get('open', 0) or 0)
        curr_price_now = float(tick_data.get('lastPrice', 0) or 0)
        last_close = float(tick_data.get('lastClose', 0) or 0)

        # 外层先算：今天底仓该不该在。风控开关与内层信号用的是同一个
        # 「严格早于今日」的日线窗口，只是取的根数更长（要覆盖整段清仓期）。
        self._update_risk_switch(today, tick_data, last_close)

        self.conn.refresh_daily_cache()
        snapshot = self.conn.load_daily_snapshot(
            cfg.HIST_DATA_LEN, today=today, tick_last_close=last_close,
            tick_time=tick_data.get('timetag') or tick_data.get('time'),
            retries=3, retry_delay=1.0)
        if snapshot is None:
            self._refresh_position()
            self._lock_all_trading('daily data unavailable or stale')
            return
        hist = snapshot['adjusted']
        if len(hist) < 60:
            self._lock_all_trading(
                'complete daily bars {} < 60'.format(len(hist)))
            return
        self.st['reentry_history'] = hist.copy()

        self._refresh_position()
        if self.st.get('entry_price', 0) == 0.0:
            self.st['entry_price'] = self.st.get('base_cost', 0.0)
        base_shares = self.st.get('base_shares', 0)
        base_can_use = self.st.get('base_can_use', 0)

        opens_list = hist['open'].astype(float).tolist()
        highs_list = hist['high'].astype(float).tolist()
        lows_list = hist['low'].astype(float).tolist()
        closes_list = hist['close'].astype(float).tolist()
        volume_list = hist['volume'].astype(float).tolist()
        latest_complete_close = float(snapshot['raw'].iloc[-1]['close'])
        signal_open, signal_open_source = resolve_signal_open(
            cfg.now_hms(), today_open, latest_complete_close)

        # compute_signal is kept whole: it IS the trigger-price formula
        # (open-anchored, ATR-scaled, range-capped) and the do_short gate.
        signal = compute_signal(
            opens_list, highs_list, lows_list, closes_list, volume_list,
            yesterday_close=last_close, today_open=signal_open)
        if signal is None:
            self._lock_all_trading('compute_signal returned None')
            return

        signal['open_price_source'] = signal_open_source
        previous10_volumes = volume_list[-11:-1] if len(volume_list) >= 11 else []
        volume_avg10 = (sum(previous10_volumes) / len(previous10_volumes)
                        if previous10_volumes else 0.0)
        signal['volume_avg10'] = volume_avg10
        signal['volume_ratio10'] = (signal['volume_current'] / volume_avg10
                                    if volume_avg10 > 0 else None)
        signal['volume_baseline_count10'] = len(previous10_volumes)

        open_price = signal['open_price']
        account = get_trade_detail_data(ACCOUNT, 'STOCK', 'ACCOUNT')
        avail_cash = account[0].m_dAvailable if account else 0.0
        if curr_price_now <= 0:
            curr_price_now = open_price
        total_asset = account[0].m_dBalance if account else 0.0
        pos_value = base_shares * curr_price_now
        pos_pct = pos_value / total_asset * 100 if total_asset > 0 else 0.0
        capacity = calculate_execution_capacity(
            base_can_use, avail_cash, curr_price_now,
            cfg.MAX_DAILY_TRADES, self.trade_lot)

        risk_on = bool(self.st.get('risk_on', True))
        signal['short_signal_allowed'] = signal['do_short']
        signal['short_signal_reason'] = signal.get('blocked_reason', '')
        signal['risk_on'] = risk_on
        # 外层为主：risk_off 的交易日，内层两条腿都不启动。
        do_short = signal['do_short'] and capacity['can_short'] and risk_on
        if not risk_on:
            short_reason = 'RISK-OFF (daily trend guard): ' + self.st.get(
                'risk_reason', '')
        elif not signal['do_short']:
            short_reason = signal.get('blocked_reason', 'signal blocked')
        elif not capacity['can_short']:
            short_reason = capacity['short_reason']
        else:
            short_reason = ''
        do_long = capacity['can_long'] and risk_on
        long_reason = capacity['long_reason'] if not do_long else ''
        if not risk_on:
            long_reason = 'RISK-OFF (daily trend guard)'

        buy_trigger_floor = round(open_price * (1.0 - cfg.BUY_TRIGGER_PCT), 2)
        buy_trigger_trail = round(curr_price_now * (1.0 - cfg.BUY_TRIGGER_TRAIL), 2)
        buy_trigger = max(buy_trigger_floor, buy_trigger_trail)

        signal['do_short'] = do_short
        signal['short_reason'] = short_reason
        signal['buy_trigger'] = buy_trigger
        signal['buy_trigger_floor'] = buy_trigger_floor
        signal['buy_trigger_trail'] = buy_trigger_trail
        signal['buy_trigger_max_trail'] = buy_trigger_trail
        signal['sellback_target_hint'] = round(
            buy_trigger * (1.0 + cfg.SELLBACK_RISE_PCT), 2)

        self.st['daily_signal'] = signal
        self.st['do_short'] = do_short
        self.st['do_long'] = do_long
        self.st['long_reason'] = long_reason
        self.st['short_lots'] = capacity['short_lots']
        self.st['long_lots'] = capacity['long_lots']
        self.st['pos_value'] = pos_value
        self.st['pos_pct'] = pos_pct
        self.st['avail_cash'] = avail_cash
        self.st['trade_count_short'] = 0
        self.st['trade_count_long'] = 0
        self.st['fstate'] = STATE_IDLE
        for key in ('peak_price', 'dip_price', 'sell_fill_price',
                    'buyback_target', 'buyback_target_pct', 'bt_dip_price',
                    'bt_buy_trigger', 'bt_buy_fill_price', 'bt_sellback_target',
                    'bt_sell_peak_price'):
            self.st[key] = 0.0
        self.st['bt_max_trail'] = buy_trigger_trail
        self.st['day_pnl'] = 0.0
        self.st['state_enter_time'] = cfg.now_hms()
        self.st['sell_elapsed_bars'] = 0
        self.st['locked'] = False
        self.st['lock_reason'] = ''
        self.st['lock_since'] = ''
        self.st['_market_open_logged'] = False
        self.st['initialized'] = True

    def _refresh_position(self):
        positions = get_trade_detail_data(ACCOUNT, 'STOCK', 'POSITION')
        found = False
        for pos in positions:
            if pos.m_strInstrumentID == self.stock_code:
                self.st['base_shares'] = pos.m_nVolume
                self.st['base_can_use'] = getattr(
                    pos, 'm_nCanUseVolume', pos.m_nVolume)
                self.st['base_cost'] = pos.m_dOpenPrice
                found = True
                break
        if not found:
            self.st['base_shares'] = 0
            self.st['base_can_use'] = 0
            self.st['base_cost'] = 0.0

    def _refresh_capacity(self):
        """Re-derive do_short/do_long from the live account after a cycle closes."""
        st = self.st
        signal = st.get('daily_signal') or {}
        if not signal:
            return
        self._refresh_position()
        price = self._cur_price()
        cash = max(0.0, self._available_cash())
        reserved = self._leg_shares(st.get('long_legs', []))
        free = max(0, st.get('base_can_use', 0) - reserved)
        cap = calculate_execution_capacity(
            free, cash, price, cfg.MAX_DAILY_TRADES, self.trade_lot)
        risk_on = bool(st.get('risk_on', True))
        allowed = bool(signal.get('short_signal_allowed',
                                  signal.get('do_short', False)))
        short = (risk_on and allowed and cap['can_short'] and
                 st.get('trade_count_short', 0) < cfg.MAX_DAILY_TRADES)
        long = (risk_on and cap['can_long'] and
                st.get('trade_count_long', 0) < cfg.MAX_DAILY_TRADES)
        changed = (short, long) != (st.get('do_short'), st.get('do_long'))
        st['do_short'] = short
        st['do_long'] = long
        st['long_reason'] = cap['long_reason']
        st['avail_cash'] = cash
        reason = (signal.get('short_signal_reason', '')
                  if not allowed else cap['short_reason'])
        if not risk_on:
            reason = 'RISK-OFF (daily trend guard)'
        signal['do_short'] = short
        signal['short_reason'] = reason
        if changed:
            self._log('[CAPACITY] sellable={} reserved={} free={} cash=Y{:.2f} '
                      'REV={} FWD={} | {} {}'.format(
                          st.get('base_can_use', 0), reserved, free, cash,
                          short, long, reason, cap['long_reason']))

    # ═══ 价格 / 数量工具 ═══

    def _cur_price(self):
        tick = self.ctx.get_full_tick([self.stock_qmt])
        price = tick.get(self.stock_qmt, {}).get('lastPrice', 0)
        if price <= 0:
            price = self.st.get('daily_signal', {}).get('open_price', 0)
        return price

    def _available_cash(self):
        account = get_trade_detail_data(ACCOUNT, 'STOCK', 'ACCOUNT')
        cash = account[0].m_dAvailable if account else 0.0
        return max(0.0, cash - self.portfolio.reserved_cash(
            exclude=self.stock_qmt))

    def _paired_long_capacity(self, price):
        """Capacity for a same-day buy leg backed by still-sellable old shares."""
        self._refresh_position()
        reserved = self._leg_shares(self.st.get('long_legs', []))
        sellable = max(0, int(self.st.get('base_can_use', 0) or 0))
        pairing_shares = max(0, sellable - reserved)
        capacity = calculate_execution_capacity(
            pairing_shares, self._available_cash(), price, 1, self.trade_lot)
        capacity['reserved_long_shares'] = reserved
        capacity['pairing_shares'] = pairing_shares
        if pairing_shares < self.trade_lot:
            capacity['long_reason'] = (
                'T+1 sellable base shares pairing capacity {} sh '
                '(sellable {} - reserved {}) < {} sh'
                .format(pairing_shares, sellable, reserved, self.trade_lot))
        return capacity

    def _clamp_sell_shares(self, planned):
        """卖出前检查可卖仓位: 实际可卖 = min(计划, base_can_use)。"""
        self._refresh_position()
        can_use = self.st.get('base_can_use', 0)
        return int(min(planned, can_use))

    def _clamp_buy_shares(self, planned, price):
        """买入前检查现金: 实际可买 = min(计划, 现金可买股数)。"""
        if price <= 0:
            price = self._cur_price()
        avail = self._available_cash()
        if price <= 0:
            return 0
        max_by_cash = int(avail / (price * 1.001))
        return int(min(planned, max_by_cash))

    def _leg_shares(self, legs):
        return sum(s for _, s in legs)

    def _leg_avg_price(self, legs):
        sh = self._leg_shares(legs)
        return sum(p * s for p, s in legs) / sh if sh > 0 else 0.0

    def _new_t_shares(self, price, side):
        capacity = self._paired_long_capacity(price)
        cash = None
        if side == 'BUY':
            # 新开正T不能占用任何股票已卖待买回的资金（包括本股票）。
            own_reserve = (self.portfolio.reserved_cash(exclude='') -
                           self.portfolio.reserved_cash(exclude=self.stock_qmt))
            cash = max(0.0, self._available_cash() - own_reserve)
        return calculate_t_shares(
            float(price), capacity['pairing_shares'], self.trade_lot,
            T_TARGET_VALUE, T_POSITION_FRACTION, cash)

    def _buyback_limit_price(self, fallback_price):
        """Use ask1 directly; fall back to the trigger price if ask1 is absent."""
        tick = self.ctx.get_full_tick([self.stock_qmt]).get(self.stock_qmt, {})
        ask_prices = tick.get('askPrice', []) or []
        ask1 = float(ask_prices[0]) if len(ask_prices) > 0 and ask_prices[0] else 0.0
        base_price = ask1 if ask1 > 0 else float(fallback_price or 0.0)
        if base_price <= 0:
            return 0.0
        return round(base_price, 2)

    def _submit_buyback_order(self, shares, fallback_price, label):
        limit_price = self._buyback_limit_price(fallback_price)
        if limit_price <= 0:
            self._log('[{} SKIP] FIX buyback price unavailable'.format(label))
            return 'SKIP', 0
        self._last_buyback_price = limit_price
        self._log('[FIX-BUYBACK] trigger Y{:.2f} -> ask1/fallback limit Y{:.2f}'.format(
            fallback_price, limit_price))
        status, delta = self._submit_order(shares, limit_price, label, style='FIX')
        if delta:
            self._last_buyback_price = self._execution_price
        return status, delta

    # ═══ 下单 ═══

    def _submit_order(self, shares, price, label, style='COMPETE'):
        """下单 + 等待成交。shares>0 买入, <0 卖出。

        返回 (status, actual_delta):
          status: 'FILLED' 满额 | 'PARTIAL' 部分 | 'TIMEOUT' 未成交 | 'SKIP' 无可用
          actual_delta: 实际成交股数(带符号, 买正卖负)
        """
        self._last_executed_order = None
        side = 'SELL' if shares < 0 else 'BUY'
        is_new_leg = label in ('REV-T sell', 'FWD-T buy')
        planned = self._new_t_shares(price, side) if is_new_leg else abs(shares)
        if side == 'SELL':
            actual = self._clamp_sell_shares(planned)
        else:
            actual = self._clamp_buy_shares(planned, price)
        # 「RISK-RESTORE buy」也必须是整手：它按 BASE_TARGET_SHARES 下单，
        # 而现金钳制会把股数截成零股（例如 177 股），A股买入零股会被柜台拒单。
        # 与 v57 的处置一致。
        if is_new_leg or label == 'RISK-RESTORE buy':
            actual = actual // self.trade_lot * self.trade_lot
        if is_new_leg:
            self._log('[T-SIZE] {} | price=Y{:.2f} target=Y{:.0f} '
                      'base-fraction={:.0f}% | {} units x {} sh = {} sh (~Y{:.0f})'.format(
                          label, price, T_TARGET_VALUE, T_POSITION_FRACTION * 100,
                          actual // self.trade_lot, self.trade_lot, actual,
                          price * actual))
        if actual < self.trade_lot:
            self._log('[{} SKIP] {} 不足: planned {} actual {}'.format(
                label, '可卖' if side == 'SELL' else '现金', planned, actual))
            return 'SKIP', 0
        if self.dry_run:
            self._log('[SIGNAL-ORDER] {} {} shares={} price={}; no order submitted'.format(
                label, side, actual, price))
            return 'SKIP', 0
        signed = -actual if side == 'SELL' else actual
        price_str = 'MKT' if price <= 0 else 'Y{:.2f}'.format(price)
        self._log('[ORDER-{}] {} × {} sh'.format(label, price_str, actual))
        if self.portfolio.order_uncertain:
            self._log('[ORDER-BLOCKED] account has unresolved order')
            return 'SKIP', 0
        order_id = order_shares(
            self.stock_qmt, signed, style, price, self.ctx, ACCOUNT)
        if order_id is None or str(order_id) in ('', '0', '-1'):
            self._log('[ORDER-REJECTED] no valid order id')
            self.portfolio.order_uncertain = True
            raise RuntimeError(
                'submission outcome unknown; inspect broker before resuming')
        if str(order_id) in self.portfolio.own_order_ids:
            self._log('[ORDER-ID-REUSED] broker returned existing order={}'.format(
                order_id))
            self.portfolio.order_uncertain = True
            raise RuntimeError(
                'duplicate broker order id {}; inspect broker before resuming'.format(
                    order_id))
        self.portfolio.own_order_ids.add(str(order_id))
        self._submitted_order_id = order_id
        status, delta = self._wait_for_fill(signed, label, price)
        if delta:
            self._last_executed_order = dict(order_id=order_id, shares=abs(delta))
        return status, delta

    def _wait_for_fill(self, expected_shares_delta, label, trade_price,
                       timeout_sec=FILL_TIMEOUT_SEC):
        """Use this order's broker execution, never account position differences."""
        wanted = abs(expected_shares_delta)
        sign = 1 if expected_shares_delta > 0 else -1
        order_id = self._submitted_order_id
        deadline = _time.monotonic() + timeout_sec
        cancelled = False
        while True:
            try:
                order = self.conn.trader.query_stock_order(
                    self.conn._account_obj, order_id)
                if order is not None:
                    ids = (str(getattr(order, 'order_id', '')),
                           str(getattr(order, 'order_sysid', '')))
                    if order.stock_code != self.stock_qmt or str(order_id) not in ids:
                        raise ValueError('order identity mismatch')
                    volume = int(order.traded_volume or 0)
                    actual_price = float(order.traded_price or 0)
                    # xtquant.xtconstant: PART_CANCEL=53 CANCELED=54 SUCCEEDED=56 JUNK=57.
                    terminal = int(order.order_status) in TERMINAL_ORDER_STATUSES
                    if volume > wanted:
                        raise ValueError('execution quantity exceeds submitted quantity')
                    if terminal and volume == 0:
                        return 'TIMEOUT', 0
                    if ((volume == wanted or terminal) and volume > 0 and
                            math.isfinite(actual_price) and actual_price > 0):
                        self._execution_price = actual_price
                        if label in RISK_LABELS:
                            # 风控腿跨日，T 台账按日重建，记不了也不该记。
                            # 它的盈亏已经在账户现金/持仓里体现了。
                            gross, completed, cycle = 0.0, False, 0.0
                        else:
                            gross, completed, cycle = self.execution_book.record(
                                order_id, label, sign * volume, actual_price)
                        self.total_pnl += gross
                        self.st['day_pnl'] = self.st.get('day_pnl', 0) + gross
                        self.total_t_days += int(completed)
                        self._last_cycle_gross = cycle
                        if completed:
                            self._log('[CYCLE-CLOSED] {} gross=Y{:.2f} '
                                      'fees=NOT_INCLUDED'.format(label, cycle))
                        self.portfolio.own_order_ids.update(
                            value for value in ids if value)
                        self._refresh_position()
                        self._log('[EXECUTION] order={} {} qty={} avg=Y{:.4f} '
                                  'reference=Y{:.4f} realized-gross=Y{:.2f} '
                                  'total-gross=Y{:.2f} fees=NOT_INCLUDED'.format(
                                      order_id, label, volume, actual_price,
                                      trade_price, gross, self.total_pnl))
                        return ('FILLED' if volume == wanted else 'PARTIAL'), sign * volume
            except Exception as error:
                self._log('[EXECUTION-WAIT] order={} {}'.format(order_id, error))
            if _time.monotonic() >= deadline:
                if not cancelled:
                    self.conn.cancel_order(order_id)
                    cancelled = True
                    deadline = _time.monotonic() + timeout_sec
                else:
                    self.portfolio.order_uncertain = True
                    self._log('[ORDER-UNCERTAIN] order={}; account orders paused'.format(
                        order_id))
                    raise RuntimeError('unresolved broker order ' + str(order_id))
            _time.sleep(0.5)

    # ═══ 信号 ═══

    def _rev_sell_trigger(self):
        return (self.st.get('daily_signal') or {}).get('sell_trigger', 999999)

    def _update_fwd_buy_trigger(self, price):
        st = self.st
        signal = st.get('daily_signal') or {}
        if (st.get('fstate') != STATE_IDLE or
                signal.get('trigger_base') == 'CLOSE_FILL_ATR' or
                not math.isfinite(price) or price <= 0):
            return
        current_trail = round(price * (1.0 - cfg.BUY_TRIGGER_TRAIL), 2)
        max_trail = max(st.get('bt_max_trail', 0), current_trail)
        floor = signal.get('buy_trigger_floor', 0)
        st['bt_max_trail'] = max_trail
        signal['buy_trigger_trail'] = current_trail
        signal['buy_trigger_max_trail'] = max_trail
        signal['buy_trigger'] = max(floor, max_trail)
        signal['sellback_target_hint'] = round(
            signal['buy_trigger'] * (1.0 + cfg.SELLBACK_RISE_PCT), 2)

    def _recalc_open_trigger(self, tick_data, price):
        """First valid post-open tick: re-anchor the trigger on TODAY's open.

        Recomputes compute_signal's own formula; the pre-open pass may have used
        the previous close when the open tick was not yet available.
        """
        sig = self.st.get('daily_signal') or {}
        open_now = float(tick_data.get('open', 0) or 0)
        open_old = float(sig.get('open_price', 0) or 0)
        if open_now <= 0 or (open_old > 0 and abs(open_now - open_old) < 0.005):
            return
        atr_pct = float(sig.get('atr_pct', 0) or 0)
        units = float(sig.get('sell_mult', 0.40)) * cfg.SELL_TRIGGER_SCALE
        raw = open_now * (1.0 + atr_pct * units)
        range_cap = open_now * (1.0 + float(sig.get('daily_range_ma10', 0.0) or 0.0)
                                * cfg.DAILY_RANGE_CAP_MULT)
        capped = bool(cfg.DAILY_RANGE_CAP_ENABLED and raw > range_cap)
        old_trig = sig.get('sell_trigger', 0)
        sig['sell_trigger'] = round(range_cap if capped else raw, 2)
        sig['sell_trigger_raw'] = round(raw, 2)
        sig['range_capped'] = capped
        sig['open_price'] = open_now
        sig['buy_trigger_floor'] = round(
            open_now * (1.0 - cfg.BUY_TRIGGER_PCT), 2)
        self._update_fwd_buy_trigger(price)
        sig['sellback_target_hint'] = round(
            sig['buy_trigger'] * (1.0 + cfg.SELLBACK_RISE_PCT), 2)
        self._log('[SELL-TRIG RECALC] open Y{:.2f}→Y{:.2f} trig Y{:.2f}→Y{:.2f} '
                  '(units {:.3f} ATR {:.1f}%)'.format(
                      open_old, open_now, old_trig, sig['sell_trigger'],
                      units, atr_pct * 100))

    def _print_risk_brief(self):
        st = self.st
        if not st.get('risk_ready'):
            self._log('[RISK] {} -- 外层未生效'.format(
                st.get('risk_reason', 'not ready')))
            return
        self._log('[RISK] {} | peak-mode {} | {} | close Y{:.2f} peak Y{:.2f} '
                  'ATR14 Y{:.2f} stop Y{:.2f} | MA20 Y{:.2f} | replayed through {}'.format(
                      'RISK-ON 持底仓' if st.get('risk_on') else 'RISK-OFF 空仓',
                      st.get('risk_peak_mode', RISK_PEAK_MODE),
                      st.get('risk_reason', ''),
                      st.get('risk_daily_close', 0), st.get('risk_peak', 0),
                      st.get('risk_atr', 0),
                      st.get('risk_peak', 0) - RISK_K_ATR * st.get('risk_atr', 0),
                      st.get('risk_ma20', 0), st.get('risk_asof', '')))

    def _print_daily_brief(self, signal):
        if not cfg.is_market_open(cfg.now_hms()):
            self._log('[NON-TRADING PREVIEW] indicative plans only; no orders '
                      'outside trading hours; next trading day recalculates '
                      'from its opening price')
        self._print_risk_brief()
        trend = signal.get('trend', '?')
        trend_labels = {'strong_bull': 'STRONG-BULL', 'weak_bull': 'WEAK-BULL',
                        'bull': 'ADAPTIVE-BULL', 'sideways': 'ADAPTIVE-SIDEWAYS',
                        'bear': 'ADAPTIVE-BEAR'}
        open_p = signal.get('open_price', 0)
        open_source = format_signal_base_source(signal.get('open_price_source'))
        atr_pct = signal.get('atr_pct', 0) * 100
        rsi_v = signal.get('rsi', 0)
        vol_r = signal.get('vol_ratio')
        sell_mult = signal.get('sell_mult', 0)
        volume_valid = signal.get('volume_valid', False)
        vol_display = ('{:.2f}'.format(vol_r)
                       if volume_valid and vol_r is not None else 'N/A')
        sell_trig = signal.get('sell_trigger', 0)
        range_capped = signal.get('range_capped', False)
        do_short = signal.get('do_short', False)
        base_shares = self.st.get('base_shares', 0)
        base_can_use = self.st.get('base_can_use', 0)
        pos_pct = self.st.get('pos_pct', 0)
        avail_cash = self.st.get('avail_cash', 0)
        do_long = self.st.get('do_long', False)
        curr_price = self.ctx.get_full_tick(
            [self.stock_qmt]).get(self.stock_qmt, {}).get('lastPrice', 0)
        if curr_price <= 0:
            curr_price = open_p
        pos_value = base_shares * curr_price
        trend_cn = trend_labels.get(trend, trend)

        self._log('[SIGNAL] {} | SignalBase Y{:.2f} (source: {}) | ATR {:.1f}% | '
                  'RSI {:.0f} | Vol_ratio {} | Mult {:.2f} | base-trigger Y{:.2f}{} {}'.format(
                      trend_cn, open_p, open_source, atr_pct, rsi_v, vol_display,
                      sell_mult, sell_trig,
                      '(range-capped)' if range_capped else '',
                      '[REV-T blocked:{}]'.format(
                          signal.get('short_reason') or
                          signal.get('blocked_reason', 'unknown'))
                      if not do_short else ''))
        vol_last = signal.get('volume_current', 0)
        vol10_ratio = signal.get('volume_ratio10')
        vol20_ratio = signal.get('vol_ratio')
        self._log('[VOLUME] last {:.0f} | prev10_avg {:.0f} (last/10d {}) | '
                  'prev20_avg {:.0f} (last/20d {}) | n10={} n20={} | {}'.format(
                      vol_last, signal.get('volume_avg10', 0),
                      '{:.2f}x'.format(vol10_ratio) if vol10_ratio is not None else 'N/A',
                      signal.get('volume_avg20', 0),
                      '{:.2f}x'.format(vol20_ratio) if vol20_ratio is not None else 'N/A',
                      signal.get('volume_baseline_count10', 0),
                      signal.get('volume_baseline_count', 0),
                      'VALID' if volume_valid else 'INVALID-neutral'))

        bits = ['Position:{} sh Y{:,.0f}({:.0f}%)'.format(base_shares, pos_value, pos_pct),
                'Cash:Y{:,.0f}'.format(avail_cash),
                'Sellable:{} lots({} sh)'.format(
                    base_can_use // self.trade_lot, base_can_use)]
        self._log('[ACCOUNT] {}'.format(' | '.join(bits)))

        planned_short = self._new_t_shares(curr_price, 'SELL')
        planned_long = self._new_t_shares(curr_price, 'BUY')
        remaining_short = max(0, cfg.MAX_DAILY_TRADES -
                              self.st.get('trade_count_short', 0))
        remaining_long = max(0, cfg.MAX_DAILY_TRADES -
                             self.st.get('trade_count_long', 0))

        atr_fraction = float(signal.get('atr_pct', 0.0) or 0.0)
        rev_buyback_pct = atr_fraction * cfg.BUYBACK_TRIGGER_MULT
        execution_trig = self._rev_sell_trigger()
        rev_buyback_plan = round(execution_trig * (1.0 - rev_buyback_pct), 2)
        sell_raw = float(signal.get('sell_trigger_raw', sell_trig) or sell_trig)
        if signal.get('trigger_base') == 'CLOSE_FILL_ATR':
            reentry = signal['reentry']
            open_p = reentry['base']
            open_source = 'confirmed closing fill; next-cycle base'
            atr_fraction = float(reentry['atr_pct'])
            sell_formula = ('base-trigger Y{sell:.2f} = close-fill Y{base:.4f}*'
                            '(1+ATR {atr:.4f}%*(reentry_units {raw:.6f}*scale {scale:.3f}))'
                            ' [ceil cent; min distance Y0.01]').format(
                                sell=sell_trig, base=reentry['base'],
                                atr=reentry['atr_pct'] * 100,
                                raw=reentry.get('unscaled_up_units',
                                                reentry['up_units']),
                                scale=reentry.get('up_units_scale', 1.0))
        else:
            sell_formula = ('base-trigger Y{sell:.2f} = open Y{open:.2f}*'
                            '(1+ATR {atr:.2f}%*mult {mult:.2f}*scale {scale:.2f})').format(
                                sell=sell_trig, open=open_p, atr=atr_fraction * 100,
                                mult=sell_mult, scale=cfg.SELL_TRIGGER_SCALE)
        if range_capped:
            range_cap = open_p * (
                1.0 + float(signal.get('daily_range_ma10', 0.0) or 0.0) *
                cfg.DAILY_RANGE_CAP_MULT)
            sell_formula += ' | raw Y{:.2f}, range-cap Y{:.2f}'.format(
                sell_raw, range_cap)
        rev_plan = ('{} | buyback Y{buyback:.2f} = '
                    'Y{sell:.2f}*(1-{atr:.2f}%*{mult:.2f})').format(
                        sell_formula, sell=execution_trig,
                        buyback=rev_buyback_plan, atr=atr_fraction * 100,
                        mult=cfg.BUYBACK_TRIGGER_MULT)
        if do_short:
            self._log('[REV-T] {} plan {} sh / remaining {} entries | {}'.format(
                'ENABLED' if cfg.is_market_open(cfg.now_hms())
                else 'NON-TRADING PREVIEW', planned_short, remaining_short, rev_plan))
        else:
            reason = signal.get('short_reason', signal.get('blocked_reason', 'unknown'))
            self._log('[REV-T] BLOCKED {} | {}'.format(reason, rev_plan))

        buy_trig = signal.get('buy_trigger', 0)
        fwd_plan = ('plan buy Y{buy:.2f} = max(floor Y{floor:.2f}, max-trail '
                    'Y{max_trail:.2f}); current-trail Y{trail:.2f} | plan sell '
                    'Y{sell:.2f} = Y{buy:.2f}*(1+{rise:.1f}%) | '
                    'planned value~Y{lot:,.0f}').format(
                        buy=buy_trig,
                        floor=signal.get('buy_trigger_floor', 0),
                        trail=signal.get('buy_trigger_trail', 0),
                        max_trail=signal.get('buy_trigger_max_trail', 0),
                        sell=signal.get('sellback_target_hint', 0),
                        rise=cfg.SELLBACK_RISE_PCT * 100,
                        lot=buy_trig * planned_long)
        if do_long:
            self._log('[FWD-T] {} plan {} sh / remaining {} entries | {}'.format(
                'ENABLED' if cfg.is_market_open(cfg.now_hms())
                else 'NON-TRADING PREVIEW', planned_long, remaining_long, fwd_plan))
        else:
            self._log('[FWD-T] BLOCKED {} | {}'.format(
                self.st.get('long_reason', 'unknown'), fwd_plan))

        if self.total_t_days > 0:
            self._log('[CUM] {} trades gross~Y{:,.0f}'.format(
                self.total_t_days, self.total_pnl))

    # ═══ 状态机 ═══

    def _handle_idle(self, price):
        st = self.st; signal = st.get('daily_signal', {})
        if self._new_leg_block_reason():
            return
        if st.get('do_short', False):
            if cfg.now_hms() >= SHORT_NEW_ENTRY_CUTOFF:
                return
            trigger = self._rev_sell_trigger()
            if price >= trigger:
                can_use = st.get('base_can_use', st['base_shares'])
                if can_use < self.trade_lot:
                    return
                tc = st.get('trade_count_short', 0)
                if tc >= cfg.MAX_DAILY_TRADES or st.get('locked', False):
                    return
                st['trade_count_short'] = tc + 1
                st['fstate'] = STATE_SPIKING
                st['peak_price'] = price
                st['short_arm_bars'] = 0
                st['short_arm_trigger'] = trigger
                st['state_enter_time'] = cfg.now_hms()
                self._log('[REV-T spike #{}/{}] Y{:.2f} >= Y{:.2f}'.format(
                    tc + 1, cfg.MAX_DAILY_TRADES, price, trigger))
                return
        if st.get('do_long', False):
            buy_trigger = signal.get('buy_trigger', 0)
            if price <= buy_trigger:
                tc = st.get('trade_count_long', 0)
                if tc >= cfg.MAX_DAILY_TRADES:
                    return
                st['trade_count_long'] = tc + 1
                st['fstate'] = STATE_BT_DIPPING
                st['bt_dip_price'] = price
                st['bt_buy_trigger'] = buy_trigger
                st['state_enter_time'] = cfg.now_hms()
                self._log('[FWD-T dip #{}/{}] Y{:.2f} <= Y{:.2f}(-{:.2f}%)'.format(
                    tc + 1, cfg.MAX_DAILY_TRADES, price, buy_trigger,
                    (buy_trigger - price) / buy_trigger * 100))

    def _handle_spiking(self, price):
        st = self.st
        st['short_arm_bars'] = st.get('short_arm_bars', 0) + 1
        if price > st['peak_price']:
            st['peak_price'] = price
        peak = st['peak_price']
        pullback = (peak - price) / peak if peak > 0 else 0
        trigger = st.get('short_arm_trigger', self._rev_sell_trigger())
        if confirmed_short_reversal(trigger, peak, price, st['short_arm_bars']):
            block_reason = self._new_leg_block_reason()
            if block_reason:
                self._log('[REV-T ARM CANCELED] {}'.format(block_reason))
                st['trade_count_short'] = max(
                    0, st.get('trade_count_short', 0) - 1)
                st['fstate'] = STATE_IDLE
                st['peak_price'] = 0.0
                return
            self._log('[REV-T sell trig] peak Y{:.2f} pullback {:.2f}% → Y{:.2f}'.format(
                peak, pullback * 100, price))
            atr_pct = st['daily_signal']['atr_pct']
            buyback_pct = atr_pct * cfg.BUYBACK_TRIGGER_MULT
            st['buyback_target'] = round(price * (1.0 - buyback_pct), 2)
            st['buyback_target_pct'] = buyback_pct * 100
            st['sell_elapsed_bars'] = 0
            st['state_enter_time'] = cfg.now_hms()
            status, delta = self._submit_order(-self.trade_lot, price, 'REV-T sell')
            if delta:
                price = self._execution_price
            if status in ('SKIP', 'TIMEOUT'):
                if status == 'TIMEOUT':
                    self._log('[REV-T sell TIMEOUT] 未成交, 回 IDLE')
                st['trade_count_short'] = max(
                    0, st.get('trade_count_short', 0) - 1)
                st['fstate'] = STATE_IDLE
                return
            actual_sold = -delta
            st['buyback_target'] = round(price * (1.0 - buyback_pct), 2)
            st['sell_fill_price'] = price
            st['short_legs'].append((price, actual_sold))
            if status == 'PARTIAL':
                self._log('[REV-T sell PARTIAL] 实际卖出 {} sh'.format(actual_sold))
            st['fstate'] = STATE_SOLD

    def _handle_sold(self, price):
        st = self.st
        sp = st['sell_fill_price']; bt = st['buyback_target']
        tightened_bt = bt
        if st['sell_elapsed_bars'] > 30 and price > sp * 0.995:
            tightened_bt = sp * (1.0 - st['daily_signal']['atr_pct'] *
                                 cfg.BUYBACK_TRIGGER_MULT * cfg.BUYBACK_TIGHTEN_MULT)
            tightened_bt = round(max(tightened_bt, bt), 2)
        # 原买回触发价优先：一旦触及，继续沿用探底回升买回流程。
        if price <= tightened_bt:
            st['fstate'] = STATE_DIPPING
            st['dip_price'] = price
            st['state_enter_time'] = cfg.now_hms()
            self._log('[Buyback trig {}] Y{:.2f}(-{:.2f}%)'.format(
                '(tightened)' if tightened_bt > bt else '', price,
                (sp - price) / sp * 100))

    def _handle_dipping(self, price):
        st = self.st
        if price < st['dip_price']:
            st['dip_price'] = price
        dip = st['dip_price'] or price
        bounce = (price - dip) / dip if dip > 0 else 0
        if bounce >= cfg.BOUNCE_PCT:
            legs = st['short_legs'] or [(st['sell_fill_price'], self.trade_lot)]
            total_shares = self._leg_shares(legs)
            self._log('[REV-T buyback trig] low Y{:.2f} bounce {:.2f}% → Y{:.2f}'.format(
                dip, bounce * 100, price))
            bought = self._do_buyback(price, 'NORMAL')
            if bought >= total_shares and total_shares > 0:
                buyback_price = getattr(self, '_last_buyback_price', price)
                self._log('[REV-T done] buyback Y{:.2f} x {}sh gross~Y{:,.0f}'.format(
                    buyback_price, bought, self._last_cycle_gross))

    def _do_buyback(self, price, reason=''):
        st = self.st
        # 一次性买回全部未平仓反T腿 (按实际股数)
        legs = st['short_legs'] or [(st.get('sell_fill_price', price), self.trade_lot)]
        shares = self._leg_shares(legs)
        if shares <= 0:
            return 0
        status, delta = self._submit_buyback_order(
            shares, price, 'REV-T buyback({})'.format(reason))
        if delta:
            price = self._execution_price
        bought = delta if delta > 0 else 0
        if bought <= 0:
            self._log('[Buyback {}-FAIL] 未成交, 保持 SOLD 继续监控'.format(reason))
            st['fstate'] = STATE_SOLD
            return 0
        if bought >= shares:
            st['short_legs'] = []
            st['fstate'] = STATE_DONE
            self._recalculate_next_t_triggers('REV-T')
            self._try_resume()
            return bought
        # 部分买回: 保留未买回部分继续监控
        remaining = shares - bought
        st['short_legs'] = list(self.execution_book.legs.get('SHORT', []))
        st['fstate'] = STATE_SOLD
        self._log('[Buyback PARTIAL] 已买回 {} sh, 剩余 {} sh 继续监控'.format(
            bought, remaining))
        return bought

    def _handle_bt_dipping(self, price):
        st = self.st
        if price < st.get('bt_dip_price', price):
            st['bt_dip_price'] = price
        dip = st.get('bt_dip_price', price) or price
        bounce = (price - dip) / dip if dip > 0 else 0
        if bounce >= cfg.BOUNCE_PCT:
            block_reason = self._new_leg_block_reason()
            if block_reason:
                self._log('[FWD-T ARM CANCELED] {}'.format(block_reason))
                st['trade_count_long'] = max(
                    0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                st['bt_dip_price'] = 0.0
                return
            capacity = self._paired_long_capacity(price)
            if not capacity['can_long']:
                self._log('[FWD-T BLOCKED] {}'.format(capacity['long_reason']))
                st['trade_count_long'] = max(
                    0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                return
            self._log('[FWD-T buy trig] low Y{:.2f} bounce {:.2f}% → Y{:.2f}'.format(
                dip, bounce * 100, price))
            status, delta = self._submit_order(self.trade_lot, price, 'FWD-T buy')
            if delta:
                price = self._execution_price
            if status in ('SKIP', 'TIMEOUT'):
                st['trade_count_long'] = max(
                    0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                return
            st['fstate'] = STATE_BT_BOUGHT
            st['bt_buy_fill_price'] = price
            st['long_legs'].append((price, delta))
            avg_bp = self._leg_avg_price(st['long_legs'])
            st['bt_sellback_target'] = round(
                avg_bp * (1.0 + cfg.SELLBACK_RISE_PCT), 2)
            if status == 'PARTIAL':
                self._log('[FWD-T buy PARTIAL] 实际买入 {} sh'.format(delta))

    def _handle_bt_bought(self, price):
        """Hold until the sellback target. There is no stop-loss."""
        st = self.st
        target = st.get('bt_sellback_target', 999999)
        if price >= target:
            st['fstate'] = STATE_BT_SPIKING
            st['bt_sell_peak_price'] = price
            avg_bp = self._leg_avg_price(st['long_legs']) or st.get(
                'bt_buy_fill_price', 0)
            self._log('[FWD-T sellback watch] +{:.2f}% → Y{:.2f}'.format(
                (price - avg_bp) / avg_bp * 100 if avg_bp > 0 else 0, price))

    def _handle_bt_spiking(self, price):
        st = self.st
        if price > st.get('bt_sell_peak_price', price):
            st['bt_sell_peak_price'] = price
        peak = st.get('bt_sell_peak_price', price)
        pullback = (peak - price) / peak if peak > 0 else 0
        if pullback >= cfg.PULLBACK_PCT:
            # 多腿毛利 = Σ(卖价 - 各腿买价) × 各腿股数
            legs = st['long_legs'] or [(st.get('bt_buy_fill_price', price), self.trade_lot)]
            total_shares = self._leg_shares(legs)
            gross = sum((price - p) * s for p, s in legs)
            self._log('[FWD-T sell trig] peak Y{:.2f} pullback {:.2f}% → Y{:.2f} '
                      'gross~Y{:,.0f}'.format(peak, pullback * 100, price, gross))
            status, delta = self._submit_order(-total_shares, price, 'FWD-T sell')
            if delta:
                price = self._execution_price
            if status in ('SKIP', 'TIMEOUT'):
                self._log('[FWD-T sell FAIL] 未成交, 回 BT_BOUGHT')
                st['fstate'] = STATE_BT_BOUGHT
                return
            sold = -delta
            gross = sum((price - p) * n for p, n in legs)
            if sold >= total_shares:
                st['long_legs'] = []
                st['fstate'] = STATE_DONE
                self._recalculate_next_t_triggers('FWD-T')
                self._try_resume()
            else:
                remaining = total_shares - sold
                st['long_legs'] = list(self.execution_book.legs.get('LONG', []))
                st['fstate'] = STATE_BT_BOUGHT
                self._log('[FWD-T sell PARTIAL] 已卖 {} sh, 剩余 {} sh'.format(
                    sold, remaining))

    # ═══ 周期收尾 ═══

    def _recalculate_next_t_triggers(self, completed_by):
        """Re-anchor entries only after a complete closing leg is confirmed."""
        st = self.st
        closing_order = getattr(self, '_last_executed_order', None)
        st['reentry_pending'] = dict(closing_order or {}, completed_by=completed_by)
        return self._retry_atr_reentry()

    def _retry_atr_reentry(self):
        """Re-anchor the next cycle on the confirmed closing fill, or stay blocked."""
        pending = self.st.get('reentry_pending')
        if not pending:
            return False
        now = _time.monotonic()
        if now < pending.get('retry_at', 0):
            return False
        pending['retry_at'] = now + 5.0
        try:
            order_id = pending.get('order_id')
            if order_id is None:
                raise ValueError('closing order id unavailable')
            order = self.conn.trader.query_stock_order(self.conn._account_obj, order_id)
            if order is None or getattr(order, 'stock_code', '') != self.stock_qmt:
                raise ValueError('closing order not returned for this symbol')
            ids = (str(getattr(order, 'order_id', '')),
                   str(getattr(order, 'order_sysid', '')))
            if str(order_id) not in ids:
                raise ValueError('closing order id mismatch')
            price = float(getattr(order, 'traded_price', 0) or 0)
            quantity = int(getattr(order, 'traded_volume', 0) or 0)
            if not math.isfinite(price) or price <= 0 or quantity < pending.get('shares', 1):
                raise ValueError('closing execution price/volume not yet confirmed')
            history = self.st.get('reentry_history')
            if history is None:
                raise ValueError('completed daily history unavailable')
            result = calculate_atr_reentry(price, *[
                history[field].tolist() for field in ('open', 'high', 'low', 'close')])
            if result is None:
                raise ValueError('ATR history invalid or insufficient')
        except Exception as error:
            self._log('[NEXT-T WAIT] {}; new entries paused'.format(error))
            return False

        signal = self.st.get('daily_signal') or {}
        signal['sell_trigger'] = result['sell_trigger']
        signal['sell_trigger_raw'] = result['sell_trigger']
        signal['buy_trigger'] = result['buy_trigger']
        signal['buy_trigger_floor'] = result['buy_trigger']
        signal['buy_trigger_trail'] = result['buy_trigger']
        signal['buy_trigger_max_trail'] = result['buy_trigger']
        signal['sellback_target_hint'] = round(
            result['buy_trigger'] * (1 + cfg.SELLBACK_RISE_PCT), 2)
        signal['trigger_base'] = 'CLOSE_FILL_ATR'
        signal['trigger_base_price'] = price
        signal['reentry'] = result
        scale_reentry_signal(signal)
        self.st['daily_signal'] = signal
        self.st['bt_max_trail'] = result['buy_trigger']
        self.st['next_t_cycle'] = self.st.get('next_t_cycle', 0) + 1
        self.st['reentry_pending'] = None
        self._log('[NEXT-T #{}] {} | closing order={} avg=Y{:.4f} | '
                  'ATR={:.2f}% Q={:.2f} samples={} | '
                  'REV=base*(1+ATR*{:.4f}) -> Y{:.2f} | '
                  'FWD=base*(1-ATR*{:.4f}) -> Y{:.2f} (outward tick rounding)'.format(
                      self.st['next_t_cycle'], pending['completed_by'], order_id, price,
                      result['atr_pct'] * 100, result['quantile'],
                      result['sample_count'], result['up_units'],
                      result['sell_trigger'], result['down_units'],
                      result['buy_trigger']))
        self._log('[REENTRY-UNIT-FORMULA] raw {:.6f} * scale {:.3f} = {:.6f}'.format(
            result['unscaled_up_units'], result['up_units_scale'], result['up_units']))
        return True

    def _try_resume(self):
        """Re-arm after a completed cycle, re-deriving capacity from the account."""
        st = self.st
        self._refresh_capacity()
        tc_s = st.get('trade_count_short', 0)
        tc_l = st.get('trade_count_long', 0)
        can_s = st.get('do_short', False) and tc_s < cfg.MAX_DAILY_TRADES
        can_l = st.get('do_long', False) and tc_l < cfg.MAX_DAILY_TRADES
        if not (can_s or can_l):
            return
        block = self._new_leg_block_reason()
        if block:
            self._log('[RESUME BLOCKED] {}'.format(block))
            return
        st['fstate'] = STATE_IDLE
        st['peak_price'] = 0.0
        st['dip_price'] = 0.0
        st['sell_fill_price'] = 0.0
        st['buyback_target'] = 0.0
        st['short_legs'] = []
        st['long_legs'] = []
        st['state_enter_time'] = cfg.now_hms()
        self._log('[RESUME] -> IDLE (REV-T {}/{}, FWD-T {}/{})'.format(
            tc_s, cfg.MAX_DAILY_TRADES, tc_l, cfg.MAX_DAILY_TRADES))

    # ═══ 主循环 ═══

    def run(self):
        self._init_state()
        self._log('[START] {} {}'.format(
            self.version, 'SIGNAL' if self.dry_run else 'LIVE'))
        try:
            self._daily_init()
            signal = self.st.get('daily_signal')
            if signal:
                self._print_daily_brief(signal)
        except Exception as e:
            self._lock_all_trading('daily init exception: {}'.format(e))
            self._log('[ERROR] init failed: {}'.format(e))
            _traceback.print_exc()

        try:
            while self._running:
                now = cfg.now_hms()
                now_ts = _time.time()
                today = datetime.now().strftime('%Y%m%d')

                if not cfg.is_market_open(now):
                    if (not self.st.get('initialized', False) or
                            self.st.get('trade_date', '') != today):
                        if now_ts - self.st.get('last_init_time', 0.0) >= 60.0:
                            self.st['last_init_time'] = now_ts
                            self.st['init_attempts'] = self.st.get(
                                'init_attempts', 0) + 1
                            try:
                                self._daily_init()
                                signal = self.st.get('daily_signal')
                                if signal:
                                    self._print_daily_brief(signal)
                            except Exception as e:
                                self._lock_all_trading(
                                    'daily init exception: {}'.format(e))
                                self._log('[ERROR] init failed: {}'.format(e))
                    if now_ts - self._last_heartbeat >= 300:
                        self._last_heartbeat = now_ts
                        if now < '09:30:00':
                            self._file_log('[WAIT {}] to open {}'.format(
                                now, cfg.time_to_open(now)))
                        elif now > '15:00:00':
                            self._file_log('[CLOSE {}]'.format(now))
                        elif '11:30:00' < now < '13:00:00':
                            self._file_log('[LUNCH {}]'.format(now))
                    (yield 10); continue

                tick = self.ctx.get_full_tick([self.stock_qmt])
                if self.stock_qmt not in tick:
                    (yield 1); continue
                tick_data = tick[self.stock_qmt]
                price = tick_data.get('lastPrice', 0)
                if not math.isfinite(price) or price <= 0:
                    (yield 1); continue

                self._update_fwd_buy_trigger(price)
                # A pending re-anchor blocks new legs until the closing fill is
                # confirmed; retry it every iteration so a cycle can restart.
                self._retry_atr_reentry()
                if not self.st.get('_market_open_logged', False):
                    self.st['_market_open_logged'] = True
                    self._recalc_open_trigger(tick_data, price)

                # ── 外层优先：日线风控开关每个交易日只落地一次 ──
                # 需要清仓/回场时先处理；空仓日整天不做T。
                if self.st.get('risk_switch_done') != self.st.get('trade_date'):
                    self.st['risk_switch_done'] = self.st.get('trade_date')
                    self._apply_risk_switch(price)
                    if self.st.get('risk_on', True):
                        self._refresh_capacity()
                if not self.st.get('risk_on', True):
                    if now_ts - self._last_heartbeat >= 300:
                        self._last_heartbeat = now_ts
                        self._file_log('[RISK-OFF] Y{:.2f} 空仓日，内层不启动 | {}'.format(
                            price, self.st.get('risk_reason', '')))
                    (yield 5); continue

                fstate = self.st.get('fstate', STATE_IDLE)
                signal = self.st.get('daily_signal')
                do_short = self.st.get('do_short', False)
                do_long = self.st.get('do_long', False)
                if fstate == STATE_IDLE and (not signal or (not do_short and not do_long)):
                    if now_ts - self._last_heartbeat >= 300:
                        self._last_heartbeat = now_ts
                        self._file_log('[STANDBY] Y{:.2f} no trade direction'.format(
                            price))
                    (yield 5); continue

                if fstate == STATE_IDLE:
                    self._handle_idle(price)
                elif fstate == STATE_SPIKING:
                    self._handle_spiking(price)
                elif fstate == STATE_SOLD:
                    self._handle_sold(price)
                elif fstate == STATE_DIPPING:
                    self._handle_dipping(price)
                elif fstate == STATE_BT_DIPPING:
                    self._handle_bt_dipping(price)
                elif fstate == STATE_BT_BOUGHT:
                    self._handle_bt_bought(price)
                elif fstate == STATE_BT_SPIKING:
                    self._handle_bt_spiking(price)

                if self.st['fstate'] in (STATE_SOLD, STATE_DIPPING):
                    self.st['sell_elapsed_bars'] = self.st.get(
                        'sell_elapsed_bars', 0) + 1
                if (self.st.get('fstate') in (STATE_DONE, STATE_FORCED) and
                        now < '14:57:00'):
                    # Once the re-anchor resolves and capacity returns, re-arm.
                    self._try_resume()
                if now_ts - self._last_heartbeat >= 60:
                    self._last_heartbeat = now_ts
                    self._heartbeat(price)
                (yield 0.5)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self._log('[ERROR] {}'.format(e))
            _traceback.print_exc()
        finally:
            if self.st.get('fstate', '') in (STATE_SOLD, STATE_DIPPING):
                self._log('[WARN] position not bought back; leg left open')
            self._log('[STOP] {} v0563 cum {} days gross~Y{:,.0f}'.format(
                self.stock_name, self.total_t_days, self.total_pnl))

    def _heartbeat(self, price):
        fs = self.st['fstate']; sig = self.st.get('daily_signal', {})
        if fs in (STATE_DONE, STATE_FORCED):
            self._file_log('[HB] {} Y{:.2f} REV-T {}/{} FWD-T {}/{} cum {} trades~Y{:,.0f}'.format(
                fs, price, self.st.get('trade_count_short', 0), cfg.MAX_DAILY_TRADES,
                self.st.get('trade_count_long', 0), cfg.MAX_DAILY_TRADES,
                self.total_t_days, self.total_pnl))
            return
        if fs == STATE_IDLE:
            parts = []
            if self.st.get('do_short'):
                st_trig = self._rev_sell_trigger()
                if price >= st_trig:
                    parts.append('REV-T: exceeded Y{:.2f} by Y{:.2f}'.format(
                        st_trig, price - st_trig))
                else:
                    parts.append('REV-T: needs +Y{:.2f} to Y{:.2f}'.format(
                        st_trig - price, st_trig))
            else:
                parts.append('REV-T: off ({})'.format(
                    sig.get('short_reason', 'not executable')))
            if self.st.get('do_long'):
                bt_dyn = sig.get('buy_trigger', 0)
                trail_detail = ('floor Y{:.2f} current-trail Y{:.2f} '
                                'max-trail Y{:.2f} effective Y{:.2f}').format(
                                    sig.get('buy_trigger_floor', 0),
                                    sig.get('buy_trigger_trail', 0),
                                    sig.get('buy_trigger_max_trail', 0),
                                    bt_dyn)
                if price <= bt_dyn:
                    parts.append('FWD-T: threshold reached Y{:.2f} ({})'.format(
                        bt_dyn, trail_detail))
                else:
                    parts.append('FWD-T: needs -Y{:.2f} to Y{:.2f} ({})'.format(
                        price - bt_dyn, bt_dyn, trail_detail))
            else:
                parts.append('FWD-T: off ({})'.format(
                    self.st.get('long_reason', 'not executable')))
            if self.st.get('locked'):
                parts.append('LOCKED')
            self._file_log('[HB] {} Y{:.2f} {}'.format(
                fs, price, ' | '.join(parts)))
        elif fs == STATE_SPIKING:
            peak = self.st.get('peak_price', 0)
            pb = (peak - price) / peak * 100 if peak > 0 else 0
            self._file_log('[HB] {} Y{:.2f} peak Y{:.2f} pullback {:.2f}%'.format(
                fs, price, peak, pb))
        elif fs in (STATE_SOLD, STATE_DIPPING):
            sp = self.st.get('sell_fill_price', 0)
            bt = self.st.get('buyback_target', 0)
            if sp > 0:
                self._file_log('[HB] {} Y{:.2f} sell Y{:.2f} {:+.1f}% buyback Y{:.2f}'.format(
                    fs, price, sp, (price - sp) / sp * 100, bt))
        elif fs == STATE_BT_DIPPING:
            dip = self.st.get('bt_dip_price', price)
            bounce = (price - dip) / dip * 100 if dip > 0 else 0
            self._file_log('[HB] {} Y{:.2f} dip Y{:.2f} bounce {:.2f}%'.format(
                fs, price, dip, bounce))
        elif fs == STATE_BT_BOUGHT:
            bp = self.st.get('bt_buy_fill_price', 0)
            target = self.st.get('bt_sellback_target', 0)
            if bp > 0:
                self._file_log('[HB] {} Y{:.2f} buy Y{:.2f} {:+.1f}% sellback Y{:.2f}'.format(
                    fs, price, bp, (price - bp) / bp * 100, target))
        elif fs == STATE_BT_SPIKING:
            peak = self.st.get('bt_sell_peak_price', price)
            pb = (peak - price) / peak * 100 if peak > 0 else 0
            self._file_log('[HB] {} Y{:.2f} peak Y{:.2f} pullback {:.2f}%'.format(
                fs, price, peak, pb))
        else:
            self._file_log('[HB] {} Y{:.2f}'.format(fs, price))


class SymbolConnector:
    """Share account/transport, but isolate each symbol's daily cache."""
    def __init__(self, shared, stock_qmt):
        self.shared = shared
        self.stock_qmt = stock_qmt
        self.refresh_daily_cache()

    def __getattr__(self, name):
        return getattr(self.shared, name)

    def refresh_daily_cache(self):
        self._daily_data_cache = None
        self._daily_raw_cache = None
        self._daily_snapshot_meta = None

    def load_daily_snapshot(self, *args, **kwargs):
        kwargs['stock_code'] = self.stock_qmt
        return MiniQMTConnector.load_daily_snapshot(self, *args, **kwargs)


class PortfolioRunner:
    """One account, one symbol. No checkpoint, no reconciliation, no watchdog."""
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.conn = MiniQMTConnector()
        self.runners = {}
        self.order_uncertain = False
        self.own_order_ids = set()
        self._symbol_log_cache = {}

    def log_symbol_throttled(self, code, message, interval=5.0, file_only=False):
        reason = message.split(' | ', 1)[0]
        key = (code, reason)
        now = _time.monotonic()
        if now - self._symbol_log_cache.get(key, float('-inf')) < interval:
            return
        self._symbol_log_cache[key] = now
        output = '[{}] {}'.format(code.split('.')[0], message)
        if file_only:
            _log_file_only(output)
        else:
            _log(output)

    def reserved_cash(self, exclude):
        """Cash earmarked for open REV-T buybacks; never spent by a new FWD-T buy."""
        reserve = 0.0
        for code, runner in self.runners.items():
            if code == exclude:
                continue
            reserve += sum(price * shares
                           for price, shares in runner.st.get('short_legs', []))
        return reserve * 1.01

    def _prepare_trading_day(self):
        """Roll the runner into today. Open legs are left open, never reconciled."""
        today = datetime.now().strftime('%Y%m%d')
        for runner in list(self.runners.values()):
            if (runner.st.get('initialized') and
                    runner.st.get('trade_date') == today):
                continue
            if runner.has_open_legs():
                _log('[DAILY-RESET] {} discarding open legs {} from {}'.format(
                    runner.stock_qmt, runner.st.get('fstate'),
                    runner.st.get('trade_date')))
            runner._init_state()
            runner.execution_book = ExecutionBook()
            runner._daily_init()

    def save_checkpoint(self, force=False, settled=False):
        """Retained for interface parity; this build keeps no state across days."""
        return None

    def run(self):
        set_global_conn(self.conn, self.dry_run)
        if not self.conn.connect_data():
            _log('[PORTFOLIO-ERROR] market connection failed')
            return
        if not self.conn.connect_trade():
            _log('[PORTFOLIO-ERROR] account connection failed')
            return
        _log('[PORTFOLIO-START] v0563 CoreT + daily trend guard; mode={}'.format(
            'SIGNAL' if self.dry_run else 'LIVE'))
        runner = StrategyRunner(self, cfg.STOCK_QMT, cfg.STOCK_NAME)
        self.runners[cfg.STOCK_QMT] = runner
        gen = runner.run()
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        finally:
            gen.close()
            self.conn.disconnect()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='v0563 CoreT: reverse-T / forward-T with ATR re-entry plus '
                    "CaptureT_v4's daily ATR trailing-stop guard on the base")
    parser.add_argument('--mode', default='signal', choices=['signal', 'live'])
    args = parser.parse_args()
    logger = FileLogger('portfolio', version='v0563')
    set_logger(logger)
    try:
        if args.mode == 'live':
            print('LIVE: apply v0563 CoreT + trend guard to {}. Account: {}'.format(
                cfg.STOCK_QMT, ACCOUNT))
            if input('Type yes to continue: ').strip().lower() != 'yes':
                return
        PortfolioRunner(dry_run=args.mode == 'signal').run()
    finally:
        logger.close()


if __name__ == '__main__':
    main()
