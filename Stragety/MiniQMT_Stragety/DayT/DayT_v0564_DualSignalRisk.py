# -*- coding: utf-8 -*-
"""v0564 DualSignalRisk — 出场/入场信号独立计算，第三规则裁决冲突。

## v0563 的双稳区问题

v0563 的外层用状态机解决出场/入场冲突：risk_on 时只看出场条件，risk_off 时
只看入场条件。当价格落在 MA20 < close < stop 之间时，两个条件**同时成立**：

  - 出场信号: close < peak − 3×ATR  →  应该空仓
  - 入场信号: close > MA20          →  应该持仓

状态机的「只看一个」让切换取决于昨天是 ON 还是 OFF → **每天翻一次**。
601869 在 2026-09 的 15 个交易日全部处于双稳区。

## 本文件的解法：信号解耦 + 第三规则

把出场和入场拆成**两个独立的参考信号**，不再嵌套在状态机里：

  exit_signal  = close < peak − K_ATR × ATR    (True = 市场恐慌)
  entry_signal = close > MA20                    (True = 趋势企稳)

然后用 `RISK_RESOLVE_MODE` 裁决冲突：

  EXIT_PRIORITY  — 出场信号优先：只要 exit=True 就 risk_off（v0563 行为）
  ENTRY_PRIORITY — 入场信号优先：只要 entry=True 就 risk_on
  HOLD           — 冲突时保持前值：只有两个信号一致时才切换

HOLD 模式的直觉：「两个专家意见不一致时，不改主意」。在双稳区里它会锁住
在上次的决策上，直到价格离开双稳区（跌破 MA20 或涨破 stop）才行动。

## 与 v0563 的关系

v0563 的状态机等价于 EXIT_PRIORITY。v0564 把它参数化，并增加了两种替代。
默认 EXIT_PRIORITY，行为与 v0563 逐位一致。

## 无未来函数

与 v0563 相同：`load_daily_snapshot` 保证日线严格早于当日，信号在开盘前算完。
python ./run_bigqmt.py --strategy Stragety\MiniQMT_Stragety\DayT\DayT_v0564_DualSignalRisk.py --mode live
This is NOT a continuation of the DayTradeing_v13-v41 / DayT_v39-v058 lines.
Its intraday core is v0562 unchanged; the addition is the daily overlay.
"""
SHORT_NEW_ENTRY_CUTOFF = '14:20:00'
SHORT_CONFIRM_MIN_BARS = 2
SHORT_CONFIRM_MIN_EXTENSION_PCT = 0.0015
SHORT_CONFIRM_MIN_PULLBACK_PCT = 0.0020
REENTRY_UP_UNITS_SCALE = 0.80

# ── 外层：双信号风控 ──
RISK_LAYER_ENABLED = True
RISK_ATR_PERIOD = 14
RISK_K_ATR = 3.0
RISK_MA_REENTRY = 20
RISK_DAILY_LOOKBACK = 280
BASE_TARGET_SHARES = 800
# 出场/入场冲突的裁决规则。默认 EXIT_PRIORITY 与 v0563 行为逐位一致。
#   EXIT_PRIORITY  — 出场信号优先（v0563 状态机等价行为）
#   ENTRY_PRIORITY — 入场信号优先
#   HOLD           — 冲突时保持前值
RISK_RESOLVE_MODE = 'EXIT_PRIORITY'
RISK_RESOLVE_MODES = ('EXIT_PRIORITY', 'ENTRY_PRIORITY', 'HOLD')

import math
import os
import sys
import time as _time
import traceback as _traceback
from datetime import datetime

import numpy as np
import pandas as pd

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
FILL_TIMEOUT_SEC = 8.0
TERMINAL_ORDER_STATUSES = (53, 54, 56, 57)
RISK_LABELS = ('RISK-OFF sell', 'RISK-RESTORE buy')
T_TARGET_VALUE = 40000.0
T_POSITION_FRACTION = 0.40
SYMBOL_LOT_OVERRIDES = {}


def confirmed_short_reversal(trigger, peak, price, armed_bars,
                             minimum_bars=SHORT_CONFIRM_MIN_BARS,
                             minimum_extension_pct=SHORT_CONFIRM_MIN_EXTENSION_PCT,
                             minimum_pullback_pct=SHORT_CONFIRM_MIN_PULLBACK_PCT):
    if trigger <= 0 or peak <= 0 or price <= 0:
        return False
    if armed_bars < minimum_bars:
        return False
    return (peak / trigger - 1.0 >= minimum_extension_pct and
            (peak - price) / peak >= minimum_pullback_pct)


def calculate_execution_capacity(base_can_use, available_cash, price,
                                 max_daily_trades, lot_size=TRADE_LOT_SIZE):
    sellable_shares = max(0, int(base_can_use or 0))
    sellable_lots = sellable_shares // lot_size
    cash_lots = int(float(available_cash or 0) /
                    (float(price) * lot_size * 1.01)) if price and price > 0 else 0
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
        long_reasons.append('T+1: sellable base shares {} sh < {} sh'.format(
            sellable_shares, lot_size))
    return {
        'short_lots': short_lots, 'long_lots': long_lots,
        'cash_lots': cash_lots, 'sellable_lots': sellable_lots,
        'can_short': can_short, 'can_long': can_long,
        'short_reason': short_reason, 'long_reason': '; '.join(long_reasons),
    }


def scale_reentry_signal(signal):
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


def resolve_signal_open(now_hms, tick_open, latest_complete_close):
    after_hours = now_hms < '09:30:00' or now_hms >= '15:00:00'
    latest_close = float(latest_complete_close or 0.0)
    if after_hours and latest_close > 0:
        return latest_close, 'AFTER-HOURS latest_complete_close'
    tick_price = float(tick_open or 0.0)
    if tick_price > 0:
        return tick_price, 'TICK_OPEN'
    return latest_close, 'LATEST_COMPLETE_CLOSE fallback'


def format_signal_base_source(source):
    source_labels = {
        'AFTER-HOURS latest_complete_close': 'latest complete close; after-hours',
        'TICK_OPEN': 'today open tick',
        'LATEST_COMPLETE_CLOSE fallback': 'latest complete close; tick-open fallback',
    }
    return source_labels.get(source, str(source or 'unknown'))


# ═══════════════════════════════════════════════════════════════════════════
#  外层：双信号风控（v0564 的核心区别）
# ═══════════════════════════════════════════════════════════════════════════

def compute_risk_signals(closes, highs, lows):
    """从日线序列计算两个独立的参考信号。纯函数，无状态。

    返回 list[dict]，每个 dict 对应一天：
      exit_signal  = True  表示「市场恐慌，应该空仓」
      entry_signal = True  表示「趋势企稳，应该持仓」
      peak / atr / ma20 / stop 用于日志和调试
    """
    close = np.asarray(closes, dtype=float)
    high = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    n = len(close)
    need = max(RISK_ATR_PERIOD, RISK_MA_REENTRY) + 1
    if n < need or not (np.isfinite(close).all() and
                        np.isfinite(high).all() and np.isfinite(low).all()):
        return None
    if np.any(close <= 0) or np.any(high <= 0) or np.any(low <= 0):
        return None

    results = []
    running_peak = float(close[0])
    for i in range(n):
        running_peak = max(running_peak, float(close[i]))
        if i < max(RISK_ATR_PERIOD, RISK_MA_REENTRY):
            results.append({'exit': False, 'entry': False, 'peak': running_peak,
                            'atr': 0.0, 'ma20': 0.0, 'stop': 0.0})
            continue
        atr = float((high[i - RISK_ATR_PERIOD + 1:i + 1] -
                     low[i - RISK_ATR_PERIOD + 1:i + 1]).mean())
        ma20 = float(close[i - RISK_MA_REENTRY + 1:i + 1].mean())
        stop = running_peak - RISK_K_ATR * atr
        exit_sig = bool(close[i] < stop)
        entry_sig = bool(close[i] > ma20)
        results.append({'exit': exit_sig, 'entry': entry_sig,
                        'peak': running_peak, 'atr': atr, 'ma20': ma20,
                        'stop': stop, 'close': float(close[i])})
    return results


def resolve_risk_state(signals, mode=None):
    """用第三规则裁决两个参考信号，返回最终的 risk_on 序列。

    mode 默认取 RISK_RESOLVE_MODE。三种裁决规则：

    EXIT_PRIORITY:  只要 exit=True 就 risk_off（v0563 等价行为）
    ENTRY_PRIORITY: 只要 entry=True 就 risk_on
    HOLD:           冲突时保持前值，只有一致时才切换
    """
    resolve = RISK_RESOLVE_MODE if mode is None else mode
    if resolve not in RISK_RESOLVE_MODES:
        raise ValueError('unknown resolve mode: ' + str(resolve))

    risk_on = True  # 初始满仓
    result = []
    for sig in signals:
        exit_s = sig['exit']
        entry_s = sig['entry']

        if resolve == 'EXIT_PRIORITY':
            # v0563 状态机等价：出场优先
            if risk_on:
                risk_on = not exit_s
            else:
                risk_on = entry_s

        elif resolve == 'ENTRY_PRIORITY':
            # 入场优先：只要趋势企稳就持仓
            if not risk_on:
                risk_on = entry_s
            else:
                risk_on = not exit_s

        elif resolve == 'HOLD':
            # 冲突时保持前值
            if exit_s == entry_s:
                # 两个信号一致：明确
                risk_on = entry_s  # exit=False&entry=True → hold; exit=True&entry=False → flat
            # else: 冲突，保持 risk_on 不变

        result.append(risk_on)
    return result


def replay_dual_signal_risk(closes, highs, lows, mode=None):
    """v0564 的外层：双信号 + 第三规则裁决。纯函数。

    返回 dict:
      risk_on      — 最终的持仓决策
      exit_signal  — 出场信号（True = 市场恐慌）
      entry_signal — 入场信号（True = 趋势企稳）
      resolve_mode — 使用的裁决规则
      peak / atr / ma20 / stop / reason  — 与 v0563 兼容的调试信息
    """
    signals = compute_risk_signals(closes, highs, lows)
    if signals is None:
        return None
    last = signals[-1]
    risk_seq = resolve_risk_state(signals, mode)
    risk_on = risk_seq[-1]
    resolve = RISK_RESOLVE_MODE if mode is None else mode

    # 生成 reason 字符串
    if last['exit'] and last['entry']:
        conflict_desc = {
            'EXIT_PRIORITY': 'exit wins → risk_off',
            'ENTRY_PRIORITY': 'entry wins → risk_on',
            'HOLD': 'conflict → hold previous (risk_on={})'.format(risk_on),
        }
        reason = ('CONFLICT: exit(peak-3ATR={:.2f}) AND entry(MA20={:.2f}) '
                  '| {} | close={:.2f}').format(
                      last['stop'], last['ma20'], conflict_desc[resolve],
                      last['close'])
    elif last['exit']:
        reason = 'RISK-OFF exit: close {:.2f} < stop {:.2f}'.format(
            last['close'], last['stop'])
    elif last['entry']:
        reason = 'RISK-ON entry: close {:.2f} > MA20 {:.2f}'.format(
            last['close'], last['ma20'])
    else:
        reason = 'RISK-ON hold: neither exit nor entry triggered'

    return {
        'risk_on': risk_on, 'exit_signal': last['exit'],
        'entry_signal': last['entry'], 'resolve_mode': resolve,
        'peak': last['peak'], 'atr': last['atr'], 'ma20': last['ma20'],
        'stop': last['stop'], 'daily_close': last['close'],
        'reason': reason,
        'signal_history': list(zip(risk_seq, [s['exit'] for s in signals],
                                   [s['entry'] for s in signals])),
    }


class StrategyRunner:
    """One symbol, one position, one state machine. No stop-loss, no carry."""

    def __init__(self, portfolio, stock_qmt, stock_name=''):
        self.portfolio = portfolio
        self.stock_qmt = stock_qmt
        self.stock_code = stock_qmt.split('.')[0]
        self.stock_name = stock_name or stock_qmt
        self.trade_lot = SYMBOL_LOT_OVERRIDES.get(
            stock_qmt, 200 if self.stock_code.startswith(('688', '689')) else 100)
        self.version = 'v0564'
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

    def _log(self, message):
        _log('[{}]{}'.format(self.stock_code, message))

    def _file_log(self, message):
        _log_file_only('[{}]{}'.format(self.stock_code, message))

    def has_open_legs(self):
        return (any(self.execution_book.legs.values()) or
                bool(self.st.get('short_legs') or self.st.get('long_legs')) or
                self.st.get('fstate') in (STATE_SOLD, STATE_DIPPING,
                                          STATE_BT_BOUGHT, STATE_BT_SPIKING))

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
            # ── 外层：双信号风控 ──
            'risk_on': True, 'risk_ready': False, 'risk_reason': '',
            'risk_switch_done': '', 'risk_atr': 0.0, 'risk_peak': 0.0,
            'risk_ma20': 0.0, 'risk_daily_close': 0.0, 'risk_asof': '',
            'risk_exit_signal': False, 'risk_entry_signal': False,
            'risk_resolve_mode': RISK_RESOLVE_MODE,
            '_prev_risk_on': None, '_risk_flip_count': 0,
        })

    def _lock_all_trading(self, reason):
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
            return 'RISK-OFF: {}'.format(self.st.get('risk_reason', ''))
        if self.st.get('reentry_pending'):
            return 'next-T awaiting confirmed closing price / valid ATR history'
        if self.st.get('locked', False):
            return self.st.get('lock_reason', 'strategy locked')
        return ''

    # ═══ 外层：双信号风控 ═══

    def _load_risk_daily(self, today, tick_data, last_close):
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

    def _update_risk_switch(self, today, tick_data, last_close):
        st = self.st
        if not RISK_LAYER_ENABLED:
            st.update({'risk_ready': True, 'risk_on': True,
                       'risk_reason': 'LAYER-DISABLED'})
            return
        frame = self._load_risk_daily(today, tick_data, last_close)
        if frame is None:
            st.update({'risk_ready': False, 'risk_on': True,
                       'risk_reason': 'DATA-UNAVAILABLE fail-open'})
            self._log('[RISK] 日线不可用; risk_on=True')
            return

        signals = compute_risk_signals(
            frame['close'].tolist(), frame['high'].tolist(), frame['low'].tolist())
        if signals is None:
            st.update({'risk_ready': False, 'risk_on': True,
                       'risk_reason': 'INSUFFICIENT DATA fail-open'})
            return
        risk_seq = resolve_risk_state(signals, RISK_RESOLVE_MODE)
        risk_on = risk_seq[-1]
        last = signals[-1]

        # Reason
        resolve = RISK_RESOLVE_MODE
        if last['exit'] and last['entry']:
            reason = 'CONFLICT(exit+entry) mode={} → {}'.format(
                resolve, 'OFF' if not risk_on else 'ON')
        elif last['exit']:
            reason = 'EXIT: close {:.2f} < stop {:.2f}'.format(
                last['close'], last['stop'])
        elif last['entry']:
            reason = 'ENTRY: close {:.2f} > MA20 {:.2f}'.format(
                last['close'], last['ma20'])
        else:
            reason = 'HOLD: no signal'

        st.update({
            'risk_ready': True, 'risk_on': risk_on, 'risk_reason': reason,
            'risk_atr': last['atr'], 'risk_peak': last['peak'],
            'risk_ma20': last['ma20'], 'risk_daily_close': last['close'],
            'risk_asof': str(frame.index[-1]),
            'risk_exit_signal': last['exit'], 'risk_entry_signal': last['entry'],
            'risk_resolve_mode': resolve,
        })

    def _apply_risk_switch(self, price):
        st = self.st
        if st.get('locked', False):
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
            return True
        if risk_on and not holding:
            shares = int(BASE_TARGET_SHARES)
            if shares >= self.trade_lot:
                self._log('[RISK] {} → 回场买回 {} 股'.format(
                    st.get('risk_reason', ''), shares))
                self._submit_order(shares, price, 'RISK-RESTORE buy')
            return True
        return False

    # ═══ 每日初始化 ═══

    def _daily_init(self):
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
            self._lock_all_trading('complete daily bars {} < 60'.format(len(hist)))
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

        signal = compute_signal(
            opens_list, highs_list, lows_list, closes_list, volume_list,
            yesterday_close=last_close, today_open=signal_open)
        if signal is None:
            self._lock_all_trading('compute_signal returned None')
            return

        signal['open_price_source'] = signal_open_source

        # 盘前 signal 用的是 after-hours close (yesterday close)。
        # 如果 tick 已经有真实的 open，立刻用它重锚 sell_trigger，
        # 避免 risk switch / ATR re-entry 重算时又回到旧值。
        if today_open > 0 and abs(today_open - signal.get('open_price', 0)) > 0.005:
            atr_pct = float(signal.get('atr_pct', 0) or 0)
            units = float(signal.get('sell_mult', 0.40)) * cfg.SELL_TRIGGER_SCALE
            raw = today_open * (1.0 + atr_pct * units)
            range_cap = today_open * (1.0 + float(signal.get('daily_range_ma10', 0.0) or 0.0)
                                      * cfg.DAILY_RANGE_CAP_MULT)
            capped = bool(cfg.DAILY_RANGE_CAP_ENABLED and raw > range_cap)
            old_trig = signal.get('sell_trigger', 0)
            signal['sell_trigger'] = round(range_cap if capped else raw, 2)
            signal['sell_trigger_raw'] = round(raw, 2)
            signal['range_capped'] = capped
            signal['open_price'] = today_open
            buy_trigger_floor = round(today_open * (1.0 - cfg.BUY_TRIGGER_PCT), 2)
            buy_trigger_trail = round(curr_price_now * (1.0 - cfg.BUY_TRIGGER_TRAIL), 2)
            signal['buy_trigger_floor'] = buy_trigger_floor
            signal['buy_trigger'] = max(buy_trigger_floor, buy_trigger_trail)
            signal['buy_trigger_trail'] = buy_trigger_trail
            signal['buy_trigger_max_trail'] = buy_trigger_trail
            signal['sellback_target_hint'] = round(
                signal['buy_trigger'] * (1.0 + cfg.SELLBACK_RISE_PCT), 2)
            self._log('[SIGNAL-REANCHOR] open Y{:.2f}→Y{:.2f} trig Y{:.2f}→Y{:.2f}'.format(
                signal.get('open_price', 0), today_open, old_trig, signal['sell_trigger']))

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
        do_short = signal['do_short'] and capacity['can_short'] and risk_on
        if not risk_on:
            short_reason = 'RISK-OFF: ' + self.st.get('risk_reason', '')
        elif not signal['do_short']:
            short_reason = signal.get('blocked_reason', 'signal blocked')
        elif not capacity['can_short']:
            short_reason = capacity['short_reason']
        else:
            short_reason = ''
        do_long = capacity['can_long'] and risk_on
        long_reason = capacity['long_reason'] if not do_long else ''
        if not risk_on:
            long_reason = 'RISK-OFF: ' + self.st.get('risk_reason', '')

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
        reason = (signal.get('short_reason', '')
                  if not allowed else cap['short_reason'])
        if not risk_on:
            reason = 'RISK-OFF: ' + st.get('risk_reason', '')
        signal['do_short'] = short
        signal['short_reason'] = reason
        if changed:
            self._log('[CAPACITY] sellable={} reserved={} free={} cash=Y{:.2f} '
                      'REV={} FWD={} | {} {}'.format(
                          st.get('base_can_use', 0), reserved, free, cash,
                          short, long, reason, cap['long_reason']))

    def _cur_price(self):
        tick = self.ctx.get_full_tick([self.stock_qmt])
        price = tick.get(self.stock_qmt, {}).get('lastPrice', 0)
        if price <= 0:
            price = self.st.get('daily_signal', {}).get('open_price', 0)
        return price

    def _available_cash(self):
        account = get_trade_detail_data(ACCOUNT, 'STOCK', 'ACCOUNT')
        cash = account[0].m_dAvailable if account else 0.0
        return max(0.0, cash - self.portfolio.reserved_cash(exclude=self.stock_qmt))

    def _paired_long_capacity(self, price):
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
        self._refresh_position()
        return int(min(planned, self.st.get('base_can_use', 0)))

    def _clamp_buy_shares(self, planned, price):
        if price <= 0:
            price = self._cur_price()
        avail = self._available_cash()
        if price <= 0:
            return 0
        return int(min(planned, int(avail / (price * 1.001))))

    def _leg_shares(self, legs):
        return sum(s for _, s in legs)

    def _leg_avg_price(self, legs):
        sh = self._leg_shares(legs)
        return sum(p * s for p, s in legs) / sh if sh > 0 else 0.0

    def _new_t_shares(self, price, side):
        capacity = self._paired_long_capacity(price)
        cash = None
        if side == 'BUY':
            own_reserve = (self.portfolio.reserved_cash(exclude='') -
                           self.portfolio.reserved_cash(exclude=self.stock_qmt))
            cash = max(0.0, self._available_cash() - own_reserve)
        return calculate_t_shares(
            float(price), capacity['pairing_shares'], self.trade_lot,
            T_TARGET_VALUE, T_POSITION_FRACTION, cash)

    def _buyback_limit_price(self, fallback_price):
        tick = self.ctx.get_full_tick([self.stock_qmt]).get(self.stock_qmt, {})
        ask_prices = tick.get('askPrice', []) or []
        ask1 = float(ask_prices[0]) if len(ask_prices) > 0 and ask_prices[0] else 0.0
        base_price = ask1 if ask1 > 0 else float(fallback_price or 0.0)
        return round(base_price, 2) if base_price > 0 else 0.0

    def _submit_buyback_order(self, shares, fallback_price, label):
        limit_price = self._buyback_limit_price(fallback_price)
        if limit_price <= 0:
            self._log('[{} SKIP] FIX buyback price unavailable'.format(label))
            return 'SKIP', 0
        self._last_buyback_price = limit_price
        status, delta = self._submit_order(shares, limit_price, label, style='FIX')
        if delta:
            self._last_buyback_price = self._execution_price
        return status, delta

    # ═══ 下单 ═══

    def _submit_order(self, shares, price, label, style='COMPETE'):
        self._last_executed_order = None
        side = 'SELL' if shares < 0 else 'BUY'
        is_new_leg = label in ('REV-T sell', 'FWD-T buy')
        planned = self._new_t_shares(price, side) if is_new_leg else abs(shares)
        if side == 'SELL':
            actual = self._clamp_sell_shares(planned)
        else:
            actual = self._clamp_buy_shares(planned, price)
        if is_new_leg or label == 'RISK-RESTORE buy':
            actual = actual // self.trade_lot * self.trade_lot
        if is_new_leg:
            self._log('[T-SIZE] {} | price=Y{:.2f} target=Y{:.0f} '
                      'base-fraction={:.0f}% | {} units x {} sh = {} sh (~Y{:.0f})'.format(
                          label, price, T_TARGET_VALUE, T_POSITION_FRACTION * 100,
                          actual // self.trade_lot, self.trade_lot, actual, price * actual))
        if actual < self.trade_lot:
            self._log('[{} SKIP] {} 不足: planned {} actual {}'.format(
                label, '可卖' if side == 'SELL' else '现金', planned, actual))
            return 'SKIP', 0
        if self.dry_run:
            self._log('[SIGNAL-ORDER] {} {} shares={} price={}'.format(
                label, side, actual, price))
            return 'SKIP', 0
        signed = -actual if side == 'SELL' else actual
        self._log('[ORDER-{}] Y{:.2f} × {} sh'.format(label, price, actual))
        if self.portfolio.order_uncertain:
            self._log('[ORDER-BLOCKED] account has unresolved order')
            return 'SKIP', 0
        order_id = order_shares(
            self.stock_qmt, signed, style, price, self.ctx, ACCOUNT)
        if order_id is None or str(order_id) in ('', '0', '-1'):
            self._log('[ORDER-REJECTED] no valid order id')
            self.portfolio.order_uncertain = True
            raise RuntimeError('submission outcome unknown')
        if str(order_id) in self.portfolio.own_order_ids:
            self._log('[ORDER-ID-REUSED] order={}'.format(order_id))
            self.portfolio.order_uncertain = True
            raise RuntimeError('duplicate broker order id')
        self.portfolio.own_order_ids.add(str(order_id))
        self._submitted_order_id = order_id
        status, delta = self._wait_for_fill(signed, label, price)
        if delta:
            self._last_executed_order = dict(order_id=order_id, shares=abs(delta))
        return status, delta

    def _wait_for_fill(self, expected_shares_delta, label, trade_price,
                       timeout_sec=FILL_TIMEOUT_SEC):
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
                    terminal = int(order.order_status) in TERMINAL_ORDER_STATUSES
                    if terminal and volume == 0:
                        return 'TIMEOUT', 0
                    if ((volume == wanted or terminal) and volume > 0 and
                            math.isfinite(actual_price) and actual_price > 0):
                        self._execution_price = actual_price
                        if label in RISK_LABELS:
                            gross, completed, cycle = 0.0, False, 0.0
                        else:
                            gross, completed, cycle = self.execution_book.record(
                                order_id, label, sign * volume, actual_price)
                        self.total_pnl += gross
                        self.st['day_pnl'] = self.st.get('day_pnl', 0) + gross
                        self.total_t_days += int(completed)
                        self._last_cycle_gross = cycle
                        if completed:
                            self._log('[CYCLE-CLOSED] {} gross=Y{:.2f}'.format(label, cycle))
                        self.portfolio.own_order_ids.update(
                            v for v in ids if v)
                        self._refresh_position()
                        self._log('[EXECUTION] order={} {} qty={} avg=Y{:.4f} '
                                  'realized-gross=Y{:.2f}'.format(
                                      order_id, label, volume, actual_price, gross))
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
        self._log('[SELL-TRIG RECALC] open Y{:.2f}→Y{:.2f} trig Y{:.2f}→Y{:.2f}'.format(
            open_old, open_now, old_trig, sig['sell_trigger']))

    def _print_daily_brief(self, signal):
        """开盘前的完整交易计划：双信号 + 内层触发价 + 账户 + 下一步预览。"""
        st = self.st
        risk_on = st.get('risk_on', True)
        exit_s = st.get('risk_exit_signal', False)
        entry_s = st.get('risk_entry_signal', False)
        resolve = st.get('risk_resolve_mode', RISK_RESOLVE_MODE)
        peak = st.get('risk_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = peak - RISK_K_ATR * atr
        ma20 = st.get('risk_ma20', 0)
        close_y = st.get('risk_daily_close', 0)
        base_shares = st.get('base_shares', 0)
        base_can_use = st.get('base_can_use', 0)
        avail_cash = st.get('avail_cash', 0)

        # ── 外层：双信号状态 ──
        self._log('[RISK] {} | mode={}'.format(
            'RISK-ON 持底仓' if risk_on else 'RISK-OFF 空仓', resolve))
        self._log('[SIGNALS] exit={}({}) entry={}({})'.format(
            exit_s, 'close Y{:.2f} < stop Y{:.2f}'.format(close_y, stop) if exit_s else 'safe',
            entry_s, 'close Y{:.2f} > MA20 Y{:.2f}'.format(close_y, ma20) if entry_s else 'below MA20'))
        self._log('[ANCHORS] peak Y{:.2f} ATR {:.2f} stop Y{:.2f} | MA20 Y{:.2f} | gap {:.0f}元 ({:.1f}%)'.format(
            peak, atr, stop, ma20, stop - ma20, (stop - ma20) / close_y * 100 if close_y > 0 else 0))

        # ── 下一步预览 ──
        if exit_s and entry_s:
            self._log('[PLAN] ⚠️  双信号冲突: exit+entry 同时成立')
            if resolve == 'EXIT_PRIORITY':
                self._log('        → 出场优先: 今日清仓 (卖出 {} 股)'.format(base_can_use))
            elif resolve == 'ENTRY_PRIORITY':
                self._log('        → 入场优先: 今日维持持仓')
            elif resolve == 'HOLD':
                self._log('        → 保持前值: risk_on={}, 不动作'.format(risk_on))
        elif exit_s:
            self._log('[PLAN] → 清仓: 卖出 {} 股 (止损触发)'.format(base_can_use))
        elif entry_s:
            if base_shares > 0:
                self._log('[PLAN] → 持仓: 内层正常运行')
            else:
                self._log('[PLAN] → 回场: 买回 {} 股 (趋势企稳)'.format(BASE_TARGET_SHARES))
        else:
            self._log('[PLAN] → 无信号: {}'.format(
                '内层正常运行' if risk_on else '空仓等待'))

        # ── 内层：REV-T / FWD-T 计划 ──
        if risk_on:
            curr_price = signal.get('open_price', 0)
            do_short = st.get('do_short', False)
            do_long = st.get('do_long', False)
            sell_trig = signal.get('sell_trigger', 0)
            buy_trig = signal.get('buy_trigger', 0)
            sellback = signal.get('sellback_target_hint', 0)
            short_lots = st.get('short_lots', 0)
            long_lots = st.get('long_lots', 0)

            if do_short:
                atr_pct = signal.get('atr_pct', 0)
                buyback_trig = round(sell_trig * (1.0 - atr_pct * cfg.BUYBACK_TRIGGER_MULT), 2)
                gap = sell_trig - curr_price if curr_price > 0 else 0
                self._log('[REV-T] ENABLED {} lots | sell Y{:.2f} (距现价 +Y{:.2f}, +{:.1f}%) | '
                          'buyback Y{:.2f} (-{:.1f}%)'.format(
                              short_lots, sell_trig, gap,
                              gap / curr_price * 100 if curr_price > 0 else 0,
                              buyback_trig,
                              atr_pct * cfg.BUYBACK_TRIGGER_MULT * 100))
            else:
                self._log('[REV-T] BLOCKED: {}'.format(
                    st.get('short_reason', signal.get('short_reason', 'unknown'))))
            if do_long:
                gap = curr_price - buy_trig if curr_price > 0 else 0
                self._log('[FWD-T] ENABLED {} lots | buy Y{:.2f} (距现价 -Y{:.2f}, -{:.1f}%) | '
                          'sellback Y{:.2f} (+{:.1f}%)'.format(
                              long_lots, buy_trig, gap,
                              gap / curr_price * 100 if curr_price > 0 else 0,
                              sellback, cfg.SELLBACK_RISE_PCT * 100))
            else:
                self._log('[FWD-T] BLOCKED: {}'.format(
                    st.get('long_reason', 'unknown')))
        else:
            self._log('[INNER] 外层 RISK-OFF, 内层不启动')

        # ── 账户 ──
        self._log('[ACCOUNT] {} sh (sellable {}) | cash Y{:,}'.format(
            base_shares, base_can_use, int(avail_cash)))

        # ── 关键价格标记 ──
        self._log('[PRICE-MAP] stop Y{:.2f} | MA20 Y{:.2f} | 现价 Y{:.2f}'.format(
            stop, ma20, close_y))
        self._log('[RESOLVE] mode={}: {}'.format(resolve, {
            'EXIT_PRIORITY': '出场优先 — exit=True 时清仓',
            'ENTRY_PRIORITY': '入场优先 — entry=True 时持仓',
            'HOLD': '保持前值 — 冲突时不动作',
        }.get(resolve, '')))

    # ═══ 状态机（与 v0562 相同）═══

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
                self._log('[FWD-T dip #{}/{}] Y{:.2f} <= Y{:.2f}'.format(
                    tc + 1, cfg.MAX_DAILY_TRADES, price, buy_trigger))

    def _handle_spiking(self, price):
        st = self.st
        st['short_arm_bars'] = st.get('short_arm_bars', 0) + 1
        if price > st['peak_price']:
            st['peak_price'] = price
        peak = st['peak_price']
        trigger = st.get('short_arm_trigger', self._rev_sell_trigger())
        if confirmed_short_reversal(trigger, peak, price, st['short_arm_bars']):
            block_reason = self._new_leg_block_reason()
            if block_reason:
                self._log('[REV-T ARM CANCELED] {}'.format(block_reason))
                st['trade_count_short'] = max(0, st.get('trade_count_short', 0) - 1)
                st['fstate'] = STATE_IDLE
                st['peak_price'] = 0.0
                return
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
                    self._log('[REV-T sell TIMEOUT]')
                st['trade_count_short'] = max(0, st.get('trade_count_short', 0) - 1)
                st['fstate'] = STATE_IDLE
                return
            actual_sold = -delta
            st['buyback_target'] = round(price * (1.0 - buyback_pct), 2)
            st['sell_fill_price'] = price
            st['short_legs'].append((price, actual_sold))
            if status == 'PARTIAL':
                self._log('[REV-T sell PARTIAL] {}'.format(actual_sold))
            st['fstate'] = STATE_SOLD

    def _handle_sold(self, price):
        st = self.st
        sp = st['sell_fill_price']; bt = st['buyback_target']
        tightened_bt = bt
        if st['sell_elapsed_bars'] > 30 and price > sp * 0.995:
            tightened_bt = sp * (1.0 - st['daily_signal']['atr_pct'] *
                                 cfg.BUYBACK_TRIGGER_MULT * cfg.BUYBACK_TIGHTEN_MULT)
            tightened_bt = round(max(tightened_bt, bt), 2)
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
                self._log('[REV-T done] buyback Y{:.2f} x {}sh'.format(
                    getattr(self, '_last_buyback_price', price), bought))

    def _do_buyback(self, price, reason=''):
        st = self.st
        legs = st['short_legs'] or [(st.get('sell_fill_price', price), self.trade_lot)]
        shares = self._leg_shares(legs)
        if shares <= 0:
            return 0
        status, delta = self._submit_buyback_order(shares, price, 'REV-T buyback({})'.format(reason))
        if delta:
            price = self._execution_price
        bought = delta if delta > 0 else 0
        if bought <= 0:
            self._log('[Buyback {}-FAIL]'.format(reason))
            st['fstate'] = STATE_SOLD
            return 0
        if bought >= shares:
            st['short_legs'] = []
            st['fstate'] = STATE_DONE
            self._recalculate_next_t_triggers('REV-T')
            self._try_resume()
            return bought
        st['short_legs'] = list(self.execution_book.legs.get('SHORT', []))
        st['fstate'] = STATE_SOLD
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
                st['trade_count_long'] = max(0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                st['bt_dip_price'] = 0.0
                return
            capacity = self._paired_long_capacity(price)
            if not capacity['can_long']:
                self._log('[FWD-T BLOCKED] {}'.format(capacity['long_reason']))
                st['trade_count_long'] = max(0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                return
            status, delta = self._submit_order(self.trade_lot, price, 'FWD-T buy')
            if delta:
                price = self._execution_price
            if status in ('SKIP', 'TIMEOUT'):
                st['trade_count_long'] = max(0, st.get('trade_count_long', 0) - 1)
                st['fstate'] = STATE_BT_BOUGHT if st.get('long_legs') else STATE_IDLE
                return
            st['fstate'] = STATE_BT_BOUGHT
            st['bt_buy_fill_price'] = price
            st['long_legs'].append((price, delta))
            avg_bp = self._leg_avg_price(st['long_legs'])
            st['bt_sellback_target'] = round(avg_bp * (1.0 + cfg.SELLBACK_RISE_PCT), 2)

    def _handle_bt_bought(self, price):
        st = self.st
        target = st.get('bt_sellback_target', 999999)
        if price >= target:
            st['fstate'] = STATE_BT_SPIKING
            st['bt_sell_peak_price'] = price
            avg_bp = self._leg_avg_price(st['long_legs']) or st.get('bt_buy_fill_price', 0)
            self._log('[FWD-T sellback watch] +{:.2f}% → Y{:.2f}'.format(
                (price - avg_bp) / avg_bp * 100 if avg_bp > 0 else 0, price))

    def _handle_bt_spiking(self, price):
        st = self.st
        if price > st.get('bt_sell_peak_price', price):
            st['bt_sell_peak_price'] = price
        peak = st.get('bt_sell_peak_price', price)
        pullback = (peak - price) / peak if peak > 0 else 0
        if pullback >= cfg.PULLBACK_PCT:
            legs = st['long_legs'] or [(st.get('bt_buy_fill_price', price), self.trade_lot)]
            total_shares = self._leg_shares(legs)
            gross = sum((price - p) * s for p, s in legs)
            self._log('[FWD-T sell trig] peak Y{:.2f} pullback {:.2f}% → Y{:.2f}'.format(
                peak, pullback * 100, price))
            status, delta = self._submit_order(-total_shares, price, 'FWD-T sell')
            if delta:
                price = self._execution_price
            if status in ('SKIP', 'TIMEOUT'):
                self._log('[FWD-T sell FAIL]')
                st['fstate'] = STATE_BT_BOUGHT
                return
            sold = -delta
            if sold >= total_shares:
                st['long_legs'] = []
                st['fstate'] = STATE_DONE
                self._recalculate_next_t_triggers('FWD-T')
                self._try_resume()
            else:
                st['long_legs'] = list(self.execution_book.legs.get('LONG', []))
                st['fstate'] = STATE_BT_BOUGHT

    # ═══ 周期收尾 ═══

    def _recalculate_next_t_triggers(self, completed_by):
        st = self.st
        closing_order = getattr(self, '_last_executed_order', None)
        st['reentry_pending'] = dict(closing_order or {}, completed_by=completed_by)
        return self._retry_atr_reentry()

    def _retry_atr_reentry(self):
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
        self._log('[NEXT-T #{}] {} | ATR={:.2f}% Q={:.2f}'.format(
            self.st['next_t_cycle'], pending['completed_by'],
            result['atr_pct'] * 100, result['quantile']))
        return True

    def _try_resume(self, now_ts=None):
        """T 周期结束后尝试恢复：先重算 trigger，再检查容量，最后 resume。"""
        if now_ts is None:
            now_ts = _time.time()
        st = self.st
        # 1. 重算 ATR reentry trigger
        self._retry_atr_reentry()
        # 2. 检查容量
        self._refresh_position()
        price = self._cur_price()
        cash = max(0.0, self._available_cash())
        sellable = max(0, int(st.get('base_can_use', 0) or 0))
        tc_s = st.get('trade_count_short', 0)
        tc_l = st.get('trade_count_long', 0)
        can_s = (sellable >= self.trade_lot and
                 tc_s < cfg.MAX_DAILY_TRADES and
                 not self._new_leg_block_reason())
        can_l = (cash >= price * self.trade_lot * 1.01 and
                 tc_l < cfg.MAX_DAILY_TRADES and
                 not self._new_leg_block_reason())
        if not (can_s or can_l):
            # 详细记录为什么不能恢复
            reasons = []
            if sellable < self.trade_lot:
                reasons.append('T+1 sellable={} < {}'.format(sellable, self.trade_lot))
            if cash < price * self.trade_lot * 1.01:
                reasons.append('cash Y{:.0f} < lot Y{:.0f}'.format(
                    cash, price * self.trade_lot * 1.01))
            if tc_s >= cfg.MAX_DAILY_TRADES:
                reasons.append('REV-T {}/{} max'.format(tc_s, cfg.MAX_DAILY_TRADES))
            if tc_l >= cfg.MAX_DAILY_TRADES:
                reasons.append('FWD-T {}/{} max'.format(tc_l, cfg.MAX_DAILY_TRADES))
            block = self._new_leg_block_reason()
            if block:
                reasons.append(block)
            if reasons and now_ts - st.get('_resume_log_ts', 0) >= 300:
                st['_resume_log_ts'] = now_ts
                self._log('[RESUME BLOCKED] {} | sellable={} cash=Y{:.0f}'.format(
                    '; '.join(reasons), sellable, cash))
            return
        st['fstate'] = STATE_IDLE
        st['peak_price'] = 0.0
        st['dip_price'] = 0.0
        st['sell_fill_price'] = 0.0
        st['buyback_target'] = 0.0
        st['short_legs'] = []
        st['long_legs'] = []
        st['state_enter_time'] = cfg.now_hms()
        self._log('[RESUME] -> IDLE (sellable={} cash=Y{:.0f})'.format(sellable, cash))

    # ═══ 时段判定 ═══

    # 5 个时段，每个有自己的状态和日志策略：
    #   PRE_MARKET  (<09:25)    盘前准备：daily init + 计划预览
    #   MORNING     (09:30~11:30) 上午盘：风控执行 + 内层交易 + 心跳
    #   LUNCH       (11:30~13:00) 午休：状态快照
    #   AFTERNOON   (13:00~14:57) 下午盘：同上午，最后入场截止
    #   LATE        (14:57~15:00) 尾盘：强平 + 收工
    #   POST_MARKET (>15:00)    盘后：收市摘要 + 明日预判

    _PERIOD_PRE_MARKET = 'PRE_MARKET'
    _PERIOD_MORNING = 'MORNING'
    _PERIOD_LUNCH = 'LUNCH'
    _PERIOD_AFTERNOON = 'AFTERNOON'
    _PERIOD_LATE = 'LATE'
    _PERIOD_POST_MARKET = 'POST_MARKET'

    def _detect_period(self, now_hms):
        """根据当前时间判定所属时段。"""
        if now_hms < '09:25:00':
            return self._PERIOD_PRE_MARKET
        if now_hms < '09:30:00':
            return self._PERIOD_MORNING    # 集合竞价归入上午盘
        if now_hms < '11:30:00':
            return self._PERIOD_MORNING
        if now_hms < '13:00:00':
            return self._PERIOD_LUNCH
        if now_hms < '14:57:00':
            return self._PERIOD_AFTERNOON
        if now_hms <= '15:00:00':
            return self._PERIOD_LATE
        return self._PERIOD_POST_MARKET

    def _try_daily_init(self, now_ts, today):
        """每日重置：记录翻仓、重新初始化、打印计划。返回是否成功。"""
        if (self.st.get('initialized', False) and
                self.st.get('trade_date', '') == today):
            return True
        if now_ts - self.st.get('last_init_time', 0.0) < 60.0:
            return False
        self.st['last_init_time'] = now_ts
        self.st['init_attempts'] = self.st.get('init_attempts', 0) + 1
        try:
            prev_risk = self.st.get('risk_on')
            self._daily_init()
            if prev_risk is not None:
                curr_risk = self.st.get('risk_on')
                if prev_risk != curr_risk:
                    self.st['_risk_flip_count'] = self.st.get(
                        '_risk_flip_count', 0) + 1
            self.st['_prev_risk_on'] = prev_risk
            signal = self.st.get('daily_signal')
            if signal:
                self._print_daily_brief(signal)
            return True
        except Exception as e:
            self._lock_all_trading('daily init exception: {}'.format(e))
            self._log('[ERROR] init failed: {}'.format(e))
            _traceback.print_exc()
            return False

    def _get_tick_price(self):
        """获取当前 tick 价格。返回 (tick_data, price) 或 (None, 0)。"""
        tick = self.ctx.get_full_tick([self.stock_qmt])
        if self.stock_qmt not in tick:
            return None, 0
        tick_data = tick[self.stock_qmt]
        price = tick_data.get('lastPrice', 0)
        if not math.isfinite(price) or price <= 0:
            return tick_data, 0
        return tick_data, price

    # ═══ 各时段处理 ═══

    def _handle_pre_market(self, now_ts, today):
        """盘前：等待 daily init，每 300 秒心跳。"""
        self._try_daily_init(now_ts, today)
        if now_ts - self._last_heartbeat >= 300:
            self._last_heartbeat = now_ts
            self._file_log('[WAIT] 盘前 {} 等待开盘'.format(cfg.now_hms()))
        (yield 10)

    def _handle_morning(self, tick_data, price, now_ts):
        """上午盘：风控执行 + 内层交易 + 心跳。"""
        self._update_fwd_buy_trigger(price)
        self._retry_atr_reentry()
        # 每次 tick open 变化时重算 trigger（09:25~09:30 开盘价会变）
        self._recalc_open_trigger(tick_data, price)

        # 外层：双信号风控（每个交易日只执行一次）
        if self.st.get('risk_switch_done') != self.st.get('trade_date'):
            self.st['risk_switch_done'] = self.st.get('trade_date')
            self._apply_risk_switch(price)
            if self.st.get('risk_on', True):
                self._refresh_capacity()

        # RISK-OFF：空仓等待，内层不启动
        if not self.st.get('risk_on', True):
            if now_ts - self._last_heartbeat >= 1800:
                self._last_heartbeat = now_ts
                self._file_log('[RISK-OFF] Y{:.2f} exit={} entry={} mode={}'.format(
                    price, self.st.get('risk_exit_signal'),
                    self.st.get('risk_entry_signal'),
                    self.st.get('risk_resolve_mode')))
            (yield 5); return

        # 内层：状态机
        self._run_inner_state_machine(price, now_ts)

        if now_ts - self._last_heartbeat >= 1800:
            self._last_heartbeat = now_ts
            self._heartbeat(price)
        (yield 0.5)

    def _handle_lunch(self, now_ts):
        """午休：状态快照，每 5 分钟打一次。"""
        if now_ts - self._last_heartbeat >= 300:
            self._last_heartbeat = now_ts
            fs = self.st.get('fstate', STATE_IDLE)
            risk = 'ON' if self.st.get('risk_on') else 'OFF'
            exit_s = self.st.get('risk_exit_signal', False)
            entry_s = self.st.get('risk_entry_signal', False)
            zone = ('BISTABLE' if exit_s and entry_s else
                    'EXIT' if exit_s else 'ENTRY' if entry_s else 'CLEAR')
            self._file_log('[LUNCH] {} | risk={} zone={} | {} trades today'.format(
                fs, risk, zone, self.total_t_days))
        (yield 10)

    def _handle_afternoon(self, tick_data, price, now_ts):
        """下午盘：同上午，最后入场截止 14:20。"""
        self._update_fwd_buy_trigger(price)
        self._retry_atr_reentry()

        if not self.st.get('risk_on', True):
            if now_ts - self._last_heartbeat >= 1800:
                self._last_heartbeat = now_ts
                self._file_log('[RISK-OFF] Y{:.2f}'.format(price))
            (yield 5); return

        self._run_inner_state_machine(price, now_ts)

        if now_ts - self._last_heartbeat >= 1800:
            self._last_heartbeat = now_ts
            self._heartbeat(price)
        (yield 0.5)

    def _handle_late(self, tick_data, price, now_ts):
        """尾盘：强平未闭合腿，不再开新仓。"""
        fstate = self.st.get('fstate', STATE_IDLE)
        if fstate == STATE_SOLD:
            shares = int(self.st.get('sell_shares', 0))
            if shares > 0:
                self._log('[FORCE-FLAT] 尾盘强平 {} 股'.format(shares))
                self._submit_order(shares, price, 'REV-T buyback(FORCE)')
                self.st['short_legs'] = list(
                    self.execution_book.legs.get('SHORT', []))
                self.st['fstate'] = (STATE_IDLE if not self.st['short_legs']
                                     else STATE_SOLD)
        if now_ts - self._last_heartbeat >= 30:
            self._last_heartbeat = now_ts
            fs = self.st.get('fstate', STATE_IDLE)
            self._file_log('[LATE] {} Y{:.2f} 收工倒计时'.format(fs, price))
        (yield 1)

    def _handle_post_market(self, price, now_ts):
        """盘后：收市摘要（只打一次），每 300 秒心跳。"""
        if not self.st.get('_post_market_printed', False):
            self.st['_post_market_printed'] = True
            self._print_post_market(price)
        if now_ts - self._last_heartbeat >= 300:
            self._last_heartbeat = now_ts
            self._file_log('[POST] 盘后 {} 等待明日'.format(cfg.now_hms()))
        (yield 30)

    def _run_inner_state_machine(self, price, now_ts):
        """内层 v0562 状态机：IDLE/SPIKING/SOLD/DIPPING/BT_*。"""
        fstate = self.st.get('fstate', STATE_IDLE)
        signal = self.st.get('daily_signal')
        do_short = self.st.get('do_short', False)
        do_long = self.st.get('do_long', False)

        if fstate == STATE_IDLE and (not signal or (not do_short and not do_long)):
            if now_ts - self._last_heartbeat >= 300:
                self._last_heartbeat = now_ts
                self._file_log('[STANDBY] Y{:.2f} no trade direction'.format(price))
            return

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
            self.st['sell_elapsed_bars'] = self.st.get('sell_elapsed_bars', 0) + 1
        now = cfg.now_hms()
        if (self.st.get('fstate') in (STATE_DONE, STATE_FORCED) and
                now < '14:57:00'):
            self._try_resume(now_ts)

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
                period = self._detect_period(now)

                # ── 盘后首次进入：打收市摘要 ──
                if period == self._PERIOD_POST_MARKET:
                    tick_data, price = self._get_tick_price()
                    if price > 0:
                        yield from self._handle_post_market(price, now_ts)
                        continue
                    (yield 30); continue

                # ── 盘前：等 init ──
                if period == self._PERIOD_PRE_MARKET:
                    yield from self._handle_pre_market(now_ts, today)
                    continue

                # ── 以下时段需要 tick 数据 ──
                tick_data, price = self._get_tick_price()
                if price <= 0:
                    (yield 1); continue

                # ── 午休：只打快照 ──
                if period == self._PERIOD_LUNCH:
                    yield from self._handle_lunch(now_ts)
                    continue

                # ── 尾盘：强平 ──
                if period == self._PERIOD_LATE:
                    yield from self._handle_late(tick_data, price, now_ts)
                    continue

                # ── 上午盘 / 下午盘：交易 ──
                if period == self._PERIOD_MORNING:
                    yield from self._handle_morning(tick_data, price, now_ts)
                elif period == self._PERIOD_AFTERNOON:
                    yield from self._handle_afternoon(tick_data, price, now_ts)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self._log('[ERROR] {}'.format(e))
            _traceback.print_exc()
        finally:
            if self.st.get('fstate', '') in (STATE_SOLD, STATE_DIPPING):
                self._log('[WARN] position not bought back')
            self._log('[STOP] {} v0564 cum {} days gross~Y{:,.0f}'.format(
                self.stock_name, self.total_t_days, self.total_pnl))

    def _heartbeat(self, price):
        """每分钟的状态监控：当前状态 + 距离下一步的幅度 + 触发条件。"""
        st = self.st
        fs = st.get('fstate', STATE_IDLE)
        risk_on = st.get('risk_on', True)
        exit_s = st.get('risk_exit_signal', False)
        entry_s = st.get('risk_entry_signal', False)
        resolve = st.get('risk_resolve_mode', RISK_RESOLVE_MODE)
        peak = st.get('risk_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = peak - RISK_K_ATR * atr if atr > 0 else 0
        ma20 = st.get('risk_ma20', 0)

        # ── 外层状态（一行摘要）──
        zone = 'EXIT' if exit_s and not entry_s else \
               'ENTRY' if entry_s and not exit_s else \
               'BISTABLE' if exit_s and entry_s else 'CLEAR'
        self._file_log('[HB] {} Y{:.2f} | risk={} zone={} mode={}'.format(
            fs, price, 'ON' if risk_on else 'OFF', zone, resolve))

        # ── 距离关键价格 ──
        if atr > 0:
            d_stop = price - stop
            d_ma20 = price - ma20
            self._file_log('  stop Y{:.2f} ({:+.1f}) MA20 Y{:.2f} ({:+.1f}) | '
                          'gap {:.0f}元'.format(
                              stop, d_stop, ma20, d_ma20, stop - ma20))

        # ── 内层状态 ──
        signal = st.get('daily_signal') or {}
        if fs == STATE_IDLE:
            parts = []
            if st.get('do_short'):
                st_trig = self._rev_sell_trigger()
                if price >= st_trig:
                    parts.append('REV-T: exceeded Y{:.2f}'.format(st_trig))
                else:
                    parts.append('REV-T: +Y{:.2f} to Y{:.2f}'.format(
                        st_trig - price, st_trig))
            else:
                parts.append('REV-T: off ({})'.format(
                    st.get('short_reason', signal.get('short_reason', ''))))
            if st.get('do_long'):
                bt = signal.get('buy_trigger', 0)
                if price <= bt:
                    parts.append('FWD-T: reached Y{:.2f}'.format(bt))
                else:
                    parts.append('FWD-T: -Y{:.2f} to Y{:.2f}'.format(
                        price - bt, bt))
            else:
                parts.append('FWD-T: off ({})'.format(
                    st.get('long_reason', 'unknown')))
            self._file_log('  {}'.format(' | '.join(parts)))
        elif fs == STATE_SPIKING:
            pk = st.get('peak_price', 0)
            pb = (pk - price) / pk * 100 if pk > 0 else 0
            self._file_log('  REV-T spike: peak Y{:.2f} pullback {:.2f}%'.format(pk, pb))
        elif fs in (STATE_SOLD, STATE_DIPPING):
            sp = st.get('sell_fill_price', 0)
            bt = st.get('buyback_target', 0)
            if sp > 0:
                self._file_log('  REV-T: sold Y{:.2f} {:+.1f}% buyback Y{:.2f}'.format(
                    sp, (price - sp) / sp * 100, bt))
        elif fs == STATE_BT_DIPPING:
            dip = st.get('bt_dip_price', price)
            bounce = (price - dip) / dip * 100 if dip > 0 else 0
            self._file_log('  FWD-T: dip Y{:.2f} bounce {:.2f}%'.format(dip, bounce))
        elif fs == STATE_BT_BOUGHT:
            bp = st.get('bt_buy_fill_price', 0)
            target = st.get('bt_sellback_target', 0)
            if bp > 0:
                self._file_log('  FWD-T: bought Y{:.2f} {:+.1f}% sellback Y{:.2f}'.format(
                    bp, (price - bp) / bp * 100, target))
        elif fs == STATE_BT_SPIKING:
            pk = st.get('bt_sell_peak_price', price)
            pb = (pk - price) / pk * 100 if pk > 0 else 0
            self._file_log('  FWD-T: peak Y{:.2f} pullback {:.2f}%'.format(pk, pb))
        elif fs in (STATE_DONE, STATE_FORCED):
            # T 周期已结束：显示下次触发价和容量状态
            next_trig = self._rev_sell_trigger()
            sellable = int(st.get('base_can_use', 0))
            cash = self._available_cash()
            gap = next_trig - price if next_trig > 0 and price > 0 else 0
            self._file_log('  T-DONE | next sell Y{:.2f} ({:+.1f}) | '
                          'sellable={} cash=Y{:.0f}'.format(
                              next_trig, gap, sellable, cash))

    def _print_post_market(self, price):
        """盘后收市摘要：当日信号、翻仓统计、明日预判。"""
        st = self.st
        risk_on = st.get('risk_on', True)
        exit_s = st.get('risk_exit_signal', False)
        entry_s = st.get('risk_entry_signal', False)
        resolve = st.get('risk_resolve_mode', RISK_RESOLVE_MODE)
        peak = st.get('risk_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = peak - RISK_K_ATR * atr if atr > 0 else 0
        ma20 = st.get('risk_ma20', 0)
        base_shares = st.get('base_shares', 0)
        base_can_use = st.get('base_can_use', 0)
        avail_cash = st.get('avail_cash', 0)
        prev = st.get('_prev_risk_on')
        flips = st.get('_risk_flip_count', 0)
        day_pnl = st.get('day_pnl', 0)

        # ── 当日信号回顾 ──
        self._log('[CLOSE] 收盘 Y{:.2f} | risk={} | exit={} entry={} | mode={}'.format(
            price, 'ON' if risk_on else 'OFF', exit_s, entry_s, resolve))

        # ── 翻仓统计 ──
        if prev is not None:
            flipped = (prev != risk_on)
            self._log('[FLIP] 昨日 {} → 今日 {} {} | 累计翻仓 {} 次'.format(
                'ON' if prev else 'OFF',
                'ON' if risk_on else 'OFF',
                '⚠️ 翻仓!' if flipped else '(不变)',
                flips))
        else:
            self._log('[FLIP] 首日 baseline | 累计翻仓 {} 次'.format(flips))

        # ── 冲突状态 ──
        if exit_s and entry_s:
            self._log('[ZONE] ⚠️  BISTABLE: exit+entry 同时成立')
            self._log('        stop Y{:.2f} > close Y{:.2f} > MA20 Y{:.2f}'.format(
                stop, price, ma20))
            self._log('        只要价格在这个区间内，双信号冲突将持续')
        elif exit_s:
            self._log('[ZONE] EXIT: clear risk-off signal')
        elif entry_s:
            self._log('[ZONE] ENTRY: clear risk-on signal')
        else:
            self._log('[ZONE] CLEAR: no conflict')

        # ── 明日预判 ──
        self._log('[NEXT-DAY] 明日开盘前将重放日线，计算新的 exit/entry 信号')
        if exit_s and entry_s:
            self._log('           当前冲突 zone: MA20({:.2f}) < close < stop({:.2f})'.format(
                ma20, stop))
            self._log('           若明日收盘跌破 MA20 → exit-only → risk_off')
            self._log('           若明日收盘涨破 stop → entry-only → risk_on')
            self._log('           若仍在此区间 → 冲突持续 → mode {} 决定'.format(resolve))
        elif exit_s:
            self._log('           当前: risk-off (空仓)')
            self._log('           回场条件: 收盘 > MA20 Y{:.2f}'.format(ma20))
            self._log('           距回场: 需要 +Y{:.2f} (+{:.1f}%)'.format(
                ma20 - price, (ma20 - price) / price * 100 if price > 0 else 0))
        elif entry_s:
            self._log('           当前: risk-on (持仓)')
            self._log('           清仓条件: 收盘 < stop Y{:.2f}'.format(stop))
            self._log('           距清仓: 需要 -Y{:.2f} (-{:.1f}%)'.format(
                price - stop, (price - stop) / price * 100 if price > 0 else 0))
        else:
            self._log('           当前: risk-on (无触发)')

        # ── 账户 ──
        self._log('[ACCOUNT] {} sh | cash Y{:,} | day PnL Y{:,.0f}'.format(
            base_shares, int(avail_cash), day_pnl))

        # ── 内层状态 ──
        fs = st.get('fstate', STATE_IDLE)
        if fs in (STATE_SOLD, STATE_DIPPING):
            sp = st.get('sell_fill_price', 0)
            self._log('[INNER] {} | 卖出价 Y{:.2f} → 待买回'.format(fs, sp))
        elif fs in (STATE_BT_BOUGHT, STATE_BT_SPIKING):
            bp = st.get('bt_buy_fill_price', 0)
            target = st.get('bt_sellback_target', 0)
            self._log('[INNER] {} | 买入价 Y{:.2f} → 目标 Y{:.2f}'.format(fs, bp, target))
        elif fs == STATE_IDLE:
            self._log('[INNER] IDLE | 下一个触发: {}'.format(
                'REV-T sell Y{:.2f}'.format(self._rev_sell_trigger())
                if st.get('do_short') else '无 (REV-T blocked)'))
        else:
            self._log('[INNER] {}'.format(fs))


class SymbolConnector:
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
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.conn = MiniQMTConnector()
        self.runners = {}
        self.order_uncertain = False
        self.own_order_ids = set()

    def reserved_cash(self, exclude):
        reserve = 0.0
        for code, runner in self.runners.items():
            if code == exclude:
                continue
            reserve += sum(p * s for p, s in runner.st.get('short_legs', []))
        return reserve * 1.01

    def _prepare_trading_day(self):
        today = datetime.now().strftime('%Y%m%d')
        for runner in list(self.runners.values()):
            if (runner.st.get('initialized') and
                    runner.st.get('trade_date') == today):
                continue
            if runner.has_open_legs():
                _log('[DAILY-RESET] {} discarding open legs from {}'.format(
                    runner.stock_qmt, runner.st.get('trade_date')))
            runner._init_state()
            runner.execution_book = ExecutionBook()
            runner._daily_init()

    def save_checkpoint(self, force=False, settled=False):
        return None

    def run(self):
        set_global_conn(self.conn, self.dry_run)
        if not self.conn.connect_data():
            _log('[PORTFOLIO-ERROR] market connection failed')
            return
        if not self.conn.connect_trade():
            _log('[PORTFOLIO-ERROR] account connection failed')
            return
        _log('[PORTFOLIO-START] v0564 DualSignalRisk; mode={} resolve={}'.format(
            'SIGNAL' if self.dry_run else 'LIVE', RISK_RESOLVE_MODE))
        runner = StrategyRunner(self, cfg.STOCK_QMT, cfg.STOCK_NAME)
        self.runners[cfg.STOCK_QMT] = runner
        gen = runner.run()
        try:
            while True:
                next(gen)
        except (StopIteration, KeyboardInterrupt, SystemExit):
            pass
        finally:
            gen.close()
            try:
                self.conn.disconnect()
            except Exception:
                pass


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='v0564 DualSignalRisk: exit/entry as reference signals, '
                    'third rule resolves conflict')
    parser.add_argument('--mode', default='signal', choices=['signal', 'live'])
    args = parser.parse_args()
    logger = FileLogger('portfolio', version='v0564')
    set_logger(logger)
    try:
        if args.mode == 'live':
            print('LIVE: v0564 DualSignalRisk on {}. Account: {}'.format(
                cfg.STOCK_QMT, ACCOUNT))
            if input('Type yes to continue: ').strip().lower() != 'yes':
                return
        PortfolioRunner(dry_run=args.mode == 'signal').run()
    finally:
        logger.close()


if __name__ == '__main__':
    main()
