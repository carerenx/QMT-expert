# -*- coding: utf-8 -*-
"""v0565 MultiSymbolDayT (legacy filename: EpochPeakMultiVoteRisk).

一个 PortfolioRunner 自动发现账户内全部当前持仓，并为每个标的创建独立的
StrategyRunner。各标的状态、日线缓存和成交账本隔离，账户现金与回补预留共享；
运行中新增持仓会被自动加入，零持仓但仍有待回补腿的 runner 会继续保留。

外层为纯观察层，不下单、不改变底仓、不阻断内层交易。它把历史市场高点与
当前持仓周期高水位分离。`market_peak` 只用于观察；
`position_peak` 只在持仓期更新，跌破 `position_peak - 3 * ATR` 时硬退出。
空仓后由三张恢复票决定回场：站上 MA20、MA20 上升、突破前 5 日最高收盘；
默认至少 2 票且连续确认 2 日。回场时重置 `position_peak`，开始新的风险周期。

退出和回场分别只在各自状态中拥有观察意义，不再进行同日信号优先级裁决。
内层 REV-T 每日首次卖出可卖持仓的 60%，第二次起卖出剩余可卖整手；未成交不计数。

运行：
python ./run_bigqmt.py --strategy Stragety/MiniQMT_Stragety/DayT/DayT_v0565_EpochPeakMultiVoteRisk.py --mode live
"""
SHORT_NEW_ENTRY_CUTOFF = '14:20:00'
SHORT_CONFIRM_MIN_BARS = 2
SHORT_CONFIRM_MIN_EXTENSION_PCT = 0.0015
SHORT_CONFIRM_MIN_PULLBACK_PCT = 0.0020
REENTRY_UP_UNITS_SCALE = 0.80

# ── 外层：持仓周期高水位 + 多票确认回场 ──
RISK_LAYER_ENABLED = True
RISK_ATR_PERIOD = 14
RISK_K_ATR = 3.0
RISK_MA_REENTRY = 20
RISK_MA_SLOPE_LOOKBACK = 5
RISK_BREAKOUT_LOOKBACK = 5
RISK_ENTRY_MIN_VOTES = 2
RISK_ENTRY_CONFIRM_DAYS = 2
RISK_DAILY_LOOKBACK = 280
REV_T_SELL_FRACTION = 0.60
PORTFOLIO_REFRESH_SEC = 60.0
DAILY_PLAN_TIME = '09:26:00'
PORTFOLIO_HEARTBEAT_SEC = 1800.0
TRADING_CALENDAR_RETRY_SEC = 60.0
SCHEDULER_LAG_WARN_SEC = 5.0
POST_CLOSE_DATA_READY_TIME = '15:10:00'
CLOSE_RETRY_SEC = 60.0

import hashlib
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
    _normalize_trade_date,
)

ACCOUNT = cfg.ACCOUNT
TRADE_LOT_SIZE = cfg.TRADE_LOT_SIZE
STATE_IDLE = cfg.STATE_IDLE
STATE_SPIKING = cfg.STATE_SPIKING
STATE_SOLD = cfg.STATE_SOLD
STATE_DIPPING = cfg.STATE_DIPPING
STATE_DONE = cfg.STATE_DONE
STATE_BT_DIPPING = cfg.STATE_BT_DIPPING
STATE_BT_BOUGHT = cfg.STATE_BT_BOUGHT
STATE_BT_SPIKING = cfg.STATE_BT_SPIKING
FILL_TIMEOUT_SEC = 8.0
TERMINAL_ORDER_STATUSES = (53, 54, 56, 57)
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


def calculate_rev_t_sell_shares(base_can_use, lot_size=TRADE_LOT_SIZE,
                                fraction=REV_T_SELL_FRACTION):
    """Size one REV-T sale as a whole-lot fraction with a one-lot minimum."""
    sellable = max(0, int(base_can_use or 0))
    if lot_size <= 0:
        raise ValueError('lot_size must be greater than zero')
    if not math.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError('fraction must be in (0, 1]')
    if sellable < lot_size:
        return 0
    fractional = int(sellable * fraction)
    whole_lots = fractional // lot_size * lot_size
    return min(sellable, max(lot_size, whole_lots))


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
    after_hours = now_hms < DAILY_PLAN_TIME or now_hms >= '15:00:00'
    latest_close = float(latest_complete_close or 0.0)
    if after_hours and latest_close > 0:
        return latest_close, 'AFTER-HOURS latest_complete_close'
    tick_price = float(tick_open or 0.0)
    if tick_price > 0:
        source = 'AUCTION_OPEN' if now_hms < '09:30:00' else 'TICK_OPEN'
        return tick_price, source
    return latest_close, 'LATEST_COMPLETE_CLOSE fallback'


def format_signal_base_source(source):
    source_labels = {
        'AFTER-HOURS latest_complete_close': 'latest complete close; after-hours',
        'TICK_OPEN': 'today open tick',
        'LATEST_COMPLETE_CLOSE fallback': 'latest complete close; tick-open fallback',
    }
    return source_labels.get(source, str(source or 'unknown'))


# ═══════════════════════════════════════════════════════════════════════════
#  外层：持仓周期高水位 + 多票确认回场
# ═══════════════════════════════════════════════════════════════════════════

def count_reentry_votes(close, ma20, previous_ma20, breakout_level):
    """Return the three named recovery votes and their count."""
    votes = {
        'above_ma20': bool(close > ma20),
        'ma20_rising': bool(ma20 > previous_ma20),
        'short_breakout': bool(close > breakout_level),
    }
    return votes, sum(int(value) for value in votes.values())


def replay_epoch_peak_risk(closes, highs, lows):
    """Replay the epoch-peak risk state from complete daily bars.

    Exit is a hard position-epoch trailing stop.  Re-entry is available only
    while flat and requires a configurable quorum for consecutive days.
    """
    try:
        close = np.asarray(closes, dtype=float)
        high = np.asarray(highs, dtype=float)
        low = np.asarray(lows, dtype=float)
    except (TypeError, ValueError):
        return None
    n = len(close)
    need = max(
        RISK_ATR_PERIOD,
        RISK_MA_REENTRY + RISK_MA_SLOPE_LOOKBACK,
        RISK_BREAKOUT_LOOKBACK + 1,
    )
    if (n < need or len(high) != n or len(low) != n or
            not (np.isfinite(close).all() and np.isfinite(high).all() and
                 np.isfinite(low).all())):
        return None
    if np.any(close <= 0) or np.any(high <= 0) or np.any(low <= 0):
        return None
    if not (1 <= RISK_ENTRY_MIN_VOTES <= 3):
        raise ValueError('RISK_ENTRY_MIN_VOTES must be between 1 and 3')
    if RISK_ENTRY_CONFIRM_DAYS < 1:
        raise ValueError('RISK_ENTRY_CONFIRM_DAYS must be at least 1')

    risk_on = True
    market_peak = float(close[0])
    position_peak = float(close[0])
    low_since_exit = 0.0
    confirmation_streak = 0
    flip_count = 0
    history = []

    warmup = max(
        RISK_ATR_PERIOD,
        RISK_MA_REENTRY + RISK_MA_SLOPE_LOOKBACK,
        RISK_BREAKOUT_LOOKBACK,
    )
    for i in range(n):
        current = float(close[i])
        market_peak = max(market_peak, current)
        if risk_on:
            position_peak = max(position_peak, current)
        if i < warmup:
            history.append({
                'risk_on': risk_on,
                'close': current,
                'market_peak': market_peak,
                'position_peak': position_peak,
                'atr': 0.0,
                'ma20': 0.0,
                'previous_ma20': 0.0,
                'breakout_level': 0.0,
                'stop': 0.0,
                'exit_signal': False,
                'entry_signal': False,
                'entry_votes': {},
                'entry_vote_count': 0,
                'entry_confirmation_streak': confirmation_streak,
                'transition': '',
            })
            continue

        atr = float((high[i - RISK_ATR_PERIOD + 1:i + 1] -
                     low[i - RISK_ATR_PERIOD + 1:i + 1]).mean())
        ma20 = float(close[i - RISK_MA_REENTRY + 1:i + 1].mean())
        previous_end = i - RISK_MA_SLOPE_LOOKBACK
        previous_ma20 = float(
            close[previous_end - RISK_MA_REENTRY + 1:previous_end + 1].mean())
        breakout_level = float(
            close[i - RISK_BREAKOUT_LOOKBACK:i].max())
        votes, vote_count = count_reentry_votes(
            current, ma20, previous_ma20, breakout_level)
        stop = position_peak - RISK_K_ATR * atr
        exit_signal = bool(risk_on and current < stop)
        entry_signal = False
        transition = ''
        reported_confirmation_streak = confirmation_streak

        if risk_on and exit_signal:
            risk_on = False
            low_since_exit = current
            confirmation_streak = 0
            flip_count += 1
            transition = 'EXIT'
        elif not risk_on:
            low_since_exit = (current if low_since_exit <= 0 else
                              min(low_since_exit, current))
            if vote_count >= RISK_ENTRY_MIN_VOTES:
                confirmation_streak += 1
            else:
                confirmation_streak = 0
            reported_confirmation_streak = confirmation_streak
            entry_signal = confirmation_streak >= RISK_ENTRY_CONFIRM_DAYS
            if entry_signal:
                risk_on = True
                position_peak = current
                low_since_exit = 0.0
                confirmation_streak = 0
                flip_count += 1
                transition = 'REENTRY'
                stop = position_peak - RISK_K_ATR * atr

        history.append({
            'risk_on': risk_on,
            'close': current,
            'market_peak': market_peak,
            'position_peak': position_peak,
            'atr': atr,
            'ma20': ma20,
            'previous_ma20': previous_ma20,
            'breakout_level': breakout_level,
            'stop': stop,
            'exit_signal': exit_signal,
            'entry_signal': entry_signal,
            'entry_votes': votes,
            'entry_vote_count': vote_count,
            'entry_confirmation_streak': reported_confirmation_streak,
            'transition': transition,
        })

    last = history[-1]
    if last['transition'] == 'EXIT':
        reason = 'EXIT: close {:.2f} < epoch stop {:.2f}'.format(
            last['close'], last['stop'])
    elif last['transition'] == 'REENTRY':
        reason = 'REENTRY: quorum {}/3 confirmed; epoch peak reset {:.2f}'.format(
            last['entry_vote_count'], last['position_peak'])
    elif last['risk_on']:
        reason = 'RISK-ON: epoch stop {:.2f}'.format(last['stop'])
    else:
        reason = 'RISK-OFF: reentry votes {}/3, confirmation {}/{}'.format(
            last['entry_vote_count'], last['entry_confirmation_streak'],
            RISK_ENTRY_CONFIRM_DAYS)
    return {
        'risk_on': last['risk_on'],
        'exit_signal': last['exit_signal'],
        'entry_signal': last['entry_signal'],
        'position_peak': last['position_peak'],
        'market_peak': last['market_peak'],
        'atr': last['atr'],
        'ma20': last['ma20'],
        'previous_ma20': last['previous_ma20'],
        'breakout_level': last['breakout_level'],
        'stop': last['stop'],
        'daily_close': last['close'],
        'entry_votes': last['entry_votes'],
        'entry_vote_count': last['entry_vote_count'],
        'entry_confirmation_streak': last['entry_confirmation_streak'],
        'flip_count': flip_count,
        'reason': reason,
        'history': history,
    }


class StrategyRunner:
    """One symbol, one position, one state machine. No forced liquidation."""

    def __init__(self, portfolio, stock_qmt, stock_name=''):
        self.portfolio = portfolio
        self.stock_qmt = stock_qmt
        self.stock_code = stock_qmt.split('.')[0]
        self.stock_name = stock_name or stock_qmt
        self.trade_lot = SYMBOL_LOT_OVERRIDES.get(
            stock_qmt, 200 if self.stock_code.startswith(('688', '689')) else 100)
        self.version = 'v0565'
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
        self._market_closed_logged_date = ''
        self._last_tick_guard_log = 0.0
        self._last_tick_guard_reason = ''
        self._last_close_attempt = 0.0

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
        init_attempts = int(self.st.get('init_attempts', 0) or 0)
        last_init_time = float(self.st.get('last_init_time', 0.0) or 0.0)
        self.st.update({
            'daily_signal': None, 'base_shares': 0, 'base_can_use': 0,
            'base_cost': 0.0, 'entry_price': 0.0, 'fstate': STATE_IDLE,
            'peak_price': 0.0, 'dip_price': 0.0,
            'sell_fill_price': 0.0, 'buyback_target': 0.0,
            'buyback_target_pct': 0.0, 'day_pnl': 0.0,
            'total_t_days': self.total_t_days, 'total_pnl': self.total_pnl,
            'trade_date': '', 'initialized': False,
            'init_attempts': init_attempts, 'last_init_time': last_init_time,
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
            'rev_t_sell_fills': 0,
            '_market_open_logged': False, '_post_market_printed': False,
            '_sell_trigger_hit_key': '',
            '_incomplete_signal_logged': False,
            # ── 外层：周期高水位 + 多票回场 ──
            'risk_on': True, 'risk_ready': False, 'risk_reason': '',
            'risk_switch_done': '', 'risk_atr': 0.0,
            'risk_position_peak': 0.0, 'risk_market_peak': 0.0,
            'risk_ma20': 0.0, 'risk_daily_close': 0.0, 'risk_asof': '',
            'risk_exit_signal': False, 'risk_entry_signal': False,
            'risk_entry_votes': {}, 'risk_entry_vote_count': 0,
            'risk_entry_confirmation_streak': 0,
            'risk_breakout_level': 0.0, 'risk_previous_ma20': 0.0,
            'risk_transition_status': 'OBSERVE-ONLY',
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
        if self.st.get('reentry_pending'):
            return 'next-T awaiting confirmed closing price / valid ATR history'
        if self.st.get('locked', False):
            return self.st.get('lock_reason', 'strategy locked')
        return ''

    # ═══ 外层：周期高水位 + 多票回场 ═══

    def _load_risk_daily(self, today, tick_data, last_close, snapshot=None):
        if snapshot is None:
            self.conn.refresh_daily_cache()
            try:
                snapshot = self.conn.load_daily_snapshot(
                    RISK_DAILY_LOOKBACK, today=today,
                    tick_last_close=last_close,
                    tick_time=tick_data.get('timetag') or tick_data.get('time'),
                    retries=1, retry_delay=0.0)
            except Exception as error:
                self._log('[RISK-DAILY] {}'.format(error))
                return None
        if not snapshot:
            return None
        frame = snapshot.get('adjusted')
        if frame is None:
            frame = snapshot.get('raw')
        need = max(
            RISK_ATR_PERIOD,
            RISK_MA_REENTRY + RISK_MA_SLOPE_LOOKBACK,
            RISK_BREAKOUT_LOOKBACK + 1,
        )
        if frame is None or len(frame) < need:
            return None
        return frame

    def _update_risk_switch(self, today, tick_data, last_close, snapshot=None):
        st = self.st
        if not RISK_LAYER_ENABLED:
            st.update({'risk_ready': True, 'risk_on': True,
                       'risk_reason': 'LAYER-DISABLED'})
            return
        frame = self._load_risk_daily(
            today, tick_data, last_close, snapshot=snapshot)
        if frame is None:
            st.update({'risk_ready': False,
                       'risk_reason': 'DATA-UNAVAILABLE; observation unavailable'})
            self._log('[RISK-OBSERVE] 日线不可用；不影响内层交易')
            return

        result = replay_epoch_peak_risk(
            frame['close'].tolist(), frame['high'].tolist(), frame['low'].tolist())
        if result is None:
            st.update({'risk_ready': False,
                       'risk_reason': 'INSUFFICIENT DATA; observation unavailable'})
            return

        st.update({
            'risk_ready': True,
            'risk_on': result['risk_on'],
            'risk_reason': result['reason'],
            'risk_atr': result['atr'],
            'risk_position_peak': result['position_peak'],
            'risk_market_peak': result['market_peak'],
            'risk_ma20': result['ma20'],
            'risk_previous_ma20': result['previous_ma20'],
            'risk_breakout_level': result['breakout_level'],
            'risk_daily_close': result['daily_close'],
            'risk_asof': str(frame.index[-1]),
            'risk_exit_signal': result['exit_signal'],
            'risk_entry_signal': result['entry_signal'],
            'risk_entry_votes': result['entry_votes'],
            'risk_entry_vote_count': result['entry_vote_count'],
            'risk_entry_confirmation_streak': result['entry_confirmation_streak'],
            '_risk_flip_count': result['flip_count'],
        })

    def _apply_risk_switch(self, price):
        """Record the outer signal without allowing it to affect live trading."""
        st = self.st
        st['risk_transition_status'] = 'OBSERVE-ONLY'
        self._log('[RISK-OBSERVE] {} | no order, no position change, '
                  'inner strategy remains enabled'.format(
                      st.get('risk_reason', 'data unavailable')))
        return True

    # ═══ 每日初始化 ═══

    def _daily_init(self):
        today = datetime.now().strftime('%Y%m%d')
        if (self.st.get('trade_date', '') == today and
                self.st.get('initialized', False)):
            self._refresh_position()
            return
        if self.st.get('trade_date') and self.has_open_legs():
            self._lock_all_trading(
                'previous trading-day T leg still open; manual review required')
            return
        self._init_state()
        self.st['trade_date'] = today

        tick_data = self.ctx.get_full_tick([self.stock_qmt]).get(self.stock_qmt, {})
        today_open = float(tick_data.get('open', 0) or 0)
        curr_price_now = float(tick_data.get('lastPrice', 0) or 0)
        last_close = float(tick_data.get('lastClose', 0) or 0)

        self.conn.refresh_daily_cache()
        snapshot_length = max(RISK_DAILY_LOOKBACK, cfg.HIST_DATA_LEN)
        snapshot = self.conn.load_daily_snapshot(
            snapshot_length, today=today, tick_last_close=last_close,
            tick_time=tick_data.get('timetag') or tick_data.get('time'),
            retries=1, retry_delay=0.0)
        if snapshot is None:
            self.st.update({
                'risk_ready': False,
                'risk_reason': 'DATA-UNAVAILABLE; observation unavailable',
            })
            self._refresh_position()
            self._lock_all_trading('daily data unavailable or stale')
            return
        self._update_risk_switch(
            today, tick_data, last_close, snapshot=snapshot)

        hist = snapshot['adjusted'].tail(cfg.HIST_DATA_LEN)
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
        signal['plan_price'] = curr_price_now
        signal['plan_tick_time'] = tick_data.get('timetag') or tick_data.get('time')

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
        do_short = signal['do_short'] and capacity['can_short']
        if not signal['do_short']:
            short_reason = signal.get('blocked_reason', 'signal blocked')
        elif not capacity['can_short']:
            short_reason = capacity['short_reason']
        else:
            short_reason = ''
        do_long = capacity['can_long']
        long_reason = capacity['long_reason'] if not do_long else ''

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
        self.st['short_reason'] = short_reason
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
        allowed = bool(signal.get('short_signal_allowed',
                                  signal.get('do_short', False)))
        short = (allowed and cap['can_short'] and
                 st.get('trade_count_short', 0) < cfg.MAX_DAILY_TRADES)
        long = (cap['can_long'] and
                st.get('trade_count_long', 0) < cfg.MAX_DAILY_TRADES)
        changed = (short, long) != (st.get('do_short'), st.get('do_long'))
        st['do_short'] = short
        st['do_long'] = long
        st['long_reason'] = cap['long_reason']
        st['avail_cash'] = cash
        reason = (signal.get('short_reason', '')
                  if not allowed else cap['short_reason'])
        signal['do_short'] = short
        signal['short_reason'] = reason
        st['short_reason'] = reason
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

    def _rev_t_sell_fraction(self):
        """First confirmed sale uses 60%; later sales use remaining whole lots."""
        return (REV_T_SELL_FRACTION if self.st.get('rev_t_sell_fills', 0) == 0
                else 1.0)

    def _new_t_shares(self, price, side):
        capacity = self._paired_long_capacity(price)
        if side == 'SELL':
            return calculate_rev_t_sell_shares(
                capacity['pairing_shares'], self.trade_lot,
                self._rev_t_sell_fraction())
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
        if is_new_leg:
            actual = actual // self.trade_lot * self.trade_lot
        if is_new_leg:
            if side == 'SELL':
                sizing = 'sellable-fraction={:.0f}% (first sale 60%, later remaining)'.format(
                    self._rev_t_sell_fraction() * 100)
            else:
                sizing = 'target=Y{:.0f} base-fraction={:.0f}%'.format(
                    T_TARGET_VALUE, T_POSITION_FRACTION * 100)
            self._log('[T-SIZE] {} | price=Y{:.2f} {} | {} units x {} sh '
                      '= {} sh (~Y{:.0f})'.format(
                          label, price, sizing, actual // self.trade_lot,
                          self.trade_lot, actual, price * actual))
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
                    if volume < 0 or volume > wanted:
                        raise ValueError(
                            'invalid traded volume {} for wanted {}'.format(
                                volume, wanted))
                    terminal = int(order.order_status) in TERMINAL_ORDER_STATUSES
                    if terminal and volume == 0:
                        return 'TIMEOUT', 0
                    if ((volume == wanted or terminal) and volume > 0 and
                            math.isfinite(actual_price) and actual_price > 0):
                        self._execution_price = actual_price
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
                                  'realized-gross=Y{:.2f} fees=NOT-INCLUDED'.format(
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

    def _short_block_reason(self):
        signal = self.st.get('daily_signal') or {}
        return (self.st.get('short_reason') or
                signal.get('short_reason') or
                signal.get('blocked_reason') or
                'unknown')

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
        if 'buy_trigger' not in sig:
            if not self.st.get('_incomplete_signal_logged', False):
                self.st['_incomplete_signal_logged'] = True
                self._log('[TRIGGER-SKIP] daily signal incomplete; '
                          'waiting for successful re-initialization')
            return False
        open_now = float(tick_data.get('open', 0) or 0)
        open_old = float(sig.get('open_price', 0) or 0)
        if open_now <= 0 or (open_old > 0 and abs(open_now - open_old) < 0.005):
            return False
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
        return True

    def _log_sell_trigger_hit(self, price):
        """Log the first sell-threshold hit for each REV-T cycle."""
        st = self.st
        signal = st.get('daily_signal') or {}
        trigger = float(signal.get('sell_trigger', 0.0) or 0.0)
        if (not math.isfinite(price) or price <= 0 or
                not math.isfinite(trigger) or trigger <= 0 or price < trigger):
            return False
        cycle = int(st.get('trade_count_short', 0) or 0) + 1
        enabled = bool(st.get('do_short', False))
        hit_key = '{}:{:.2f}:{}'.format(cycle, trigger, enabled)
        if st.get('_sell_trigger_hit_key') == hit_key:
            return False
        st['_sell_trigger_hit_key'] = hit_key
        planned = calculate_rev_t_sell_shares(
            st.get('base_can_use', 0), self.trade_lot, self._rev_t_sell_fraction())
        status = 'ENABLED planned={} sh'.format(planned) if enabled else (
            'BLOCKED {}'.format(self._short_block_reason()))
        self._log('[SELL-TRIG HIT] price Y{:.2f} >= trigger Y{:.2f} | '
                  'REV-T cycle={} {}'.format(price, trigger, cycle, status))
        return True

    def _log_daily_plan_formulas(self, signal, base_can_use):
        """Print the numeric formulas behind both inner-layer trade plans."""
        open_price = float(signal.get('open_price', 0.0) or 0.0)
        atr_pct = float(signal.get('atr_pct', 0.0) or 0.0)
        sell_mult = float(signal.get('sell_mult', 0.0) or 0.0)
        raw_default = open_price * (
            1.0 + atr_pct * sell_mult * cfg.SELL_TRIGGER_SCALE)
        sell_raw = float(signal.get('sell_trigger_raw', raw_default) or 0.0)
        range_pct = float(signal.get('daily_range_ma10', 0.0) or 0.0)
        range_cap = round(
            open_price * (1.0 + range_pct * cfg.DAILY_RANGE_CAP_MULT), 2)
        sell_trigger = float(signal.get('sell_trigger', 0.0) or 0.0)
        range_capped = bool(signal.get('range_capped', False))

        if cfg.DAILY_RANGE_CAP_ENABLED:
            selected_by = 'CAP' if range_capped else 'RAW'
            final_formula = (
                'min(raw Y{:.2f}, cap Y{:.2f}) [{}]'.format(
                    sell_raw, range_cap, selected_by))
        else:
            final_formula = 'raw Y{:.2f} [range-cap OFF]'.format(sell_raw)
        self._log(
            '[REV-T FORMULA] raw Y{:.2f} = open Y{:.2f} × '
            '(1 + ATR {:.2f}% × mult {:.3f} × scale {:.2f}); '
            'cap Y{:.2f} = open Y{:.2f} × '
            '(1 + rangeMA10 {:.2f}% × cap-mult {:.2f}); '
            'final Y{:.2f} = {}'.format(
                sell_raw, open_price, atr_pct * 100, sell_mult,
                cfg.SELL_TRIGGER_SCALE, range_cap, open_price,
                range_pct * 100, cfg.DAILY_RANGE_CAP_MULT,
                sell_trigger, final_formula))

        buyback_trigger = round(
            sell_trigger * (1.0 - atr_pct * cfg.BUYBACK_TRIGGER_MULT), 2)
        self._log(
            '[REV-T FORMULA] buyback Y{:.2f} = sell Y{:.2f} × '
            '(1 - ATR {:.2f}% × {:.2f})'.format(
                buyback_trigger, sell_trigger, atr_pct * 100,
                cfg.BUYBACK_TRIGGER_MULT))

        sellable = max(0, int(base_can_use or 0))
        sell_shares = calculate_rev_t_sell_shares(
            sellable, self.trade_lot, self._rev_t_sell_fraction())
        if sellable < self.trade_lot:
            self._log('[REV-T SIZE] 0 sh because sellable {} < 1 lot {}'.format(
                sellable, self.trade_lot))
        else:
            self._log(
                '[REV-T SIZE] min({}, max({}, floor('
                '{} × {:.0f}% / {}) × {})) = {} sh'.format(
                    sellable, self.trade_lot, sellable,
                    self._rev_t_sell_fraction() * 100, self.trade_lot,
                    self.trade_lot, sell_shares))

        buy_floor_default = round(
            open_price * (1.0 - cfg.BUY_TRIGGER_PCT), 2)
        buy_floor = float(
            signal.get('buy_trigger_floor', buy_floor_default) or 0.0)
        buy_trail = float(signal.get(
            'buy_trigger_max_trail',
            signal.get('buy_trigger_trail', 0.0)) or 0.0)
        buy_trigger = float(signal.get('buy_trigger', 0.0) or 0.0)
        sellback = float(signal.get('sellback_target_hint', 0.0) or 0.0)
        self._log(
            '[FWD-T FORMULA] buy Y{:.2f} = max('
            'floor Y{:.2f} = open Y{:.2f} × (1 - {:.2f}%), '
            'trail-max Y{:.2f} = max-observed(price × (1 - {:.2f}%)))'.format(
                buy_trigger, buy_floor, open_price,
                cfg.BUY_TRIGGER_PCT * 100, buy_trail,
                cfg.BUY_TRIGGER_TRAIL * 100))
        self._log(
            '[FWD-T FORMULA] sellback Y{:.2f} = buy Y{:.2f} × '
            '(1 + {:.2f}%)'.format(
                sellback, buy_trigger, cfg.SELLBACK_RISE_PCT * 100))

    def _print_daily_brief(self, signal):
        """开盘前的完整交易计划：周期止损 + 多票回场 + 内层触发价。"""
        st = self.st
        risk_on = st.get('risk_on', True)
        exit_s = st.get('risk_exit_signal', False)
        entry_s = st.get('risk_entry_signal', False)
        position_peak = st.get('risk_position_peak', 0)
        market_peak = st.get('risk_market_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = position_peak - RISK_K_ATR * atr
        ma20 = st.get('risk_ma20', 0)
        votes = st.get('risk_entry_votes') or {}
        vote_count = st.get('risk_entry_vote_count', 0)
        vote_streak = st.get('risk_entry_confirmation_streak', 0)
        base_shares = st.get('base_shares', 0)
        base_can_use = st.get('base_can_use', 0)
        avail_cash = st.get('avail_cash', 0)

        self._log('[RISK] {} | {}'.format(
            'OBSERVE-ON' if risk_on else 'OBSERVE-OFF',
            st.get('risk_reason', '')))
        self._log('[ANCHORS] epoch-peak Y{:.2f} stop Y{:.2f} ATR {:.2f} | '
                  'market-peak(observe-only) Y{:.2f}'.format(
                      position_peak, stop, atr, market_peak))
        self._log('[VOTES] above-MA20={} MA20-rising={} short-breakout={} '
                  '=> {}/3; confirm {}/{}'.format(
                      votes.get('above_ma20', False),
                      votes.get('ma20_rising', False),
                      votes.get('short_breakout', False),
                      vote_count, vote_streak, RISK_ENTRY_CONFIRM_DAYS))

        action = ('EXIT observed' if exit_s else
                  'REENTRY observed' if entry_s else 'no transition')
        self._log('[RISK-OBSERVE] {}；不下单、不改变持仓、不阻断内层'.format(action))

        # ── 内层：REV-T / FWD-T 计划（与外层观察状态无关）──
        curr_price = signal.get('plan_price') or signal.get('open_price', 0)
        do_short = st.get('do_short', False)
        do_long = st.get('do_long', False)
        sell_trig = signal.get('sell_trigger', 0)
        buy_trig = signal.get('buy_trigger', 0)
        sellback = signal.get('sellback_target_hint', 0)
        long_lots = st.get('long_lots', 0)

        self._log_daily_plan_formulas(signal, base_can_use)

        if do_short:
            atr_pct = signal.get('atr_pct', 0)
            buyback_trig = round(
                sell_trig * (1.0 - atr_pct * cfg.BUYBACK_TRIGGER_MULT), 2)
            gap = sell_trig - curr_price if curr_price > 0 else 0
            sell_shares = calculate_rev_t_sell_shares(
                base_can_use, self.trade_lot, self._rev_t_sell_fraction())
            self._log('[REV-T] ENABLED sell {} sh ({:.0f}% target, one-lot '
                      'minimum) | '
                      'sell Y{:.2f} (距现价 +Y{:.2f}, +{:.1f}%) | '
                      'buyback Y{:.2f} (-{:.1f}%)'.format(
                          sell_shares, self._rev_t_sell_fraction() * 100,
                          sell_trig, gap,
                          gap / curr_price * 100 if curr_price > 0 else 0,
                          buyback_trig,
                          atr_pct * cfg.BUYBACK_TRIGGER_MULT * 100))
        else:
            self._log('[REV-T] BLOCKED: {}'.format(self._short_block_reason()))
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

        # ── 账户 ──
        self._log('[ACCOUNT] {} sh (sellable {}) | cash Y{:,}'.format(
            base_shares, base_can_use, int(avail_cash)))

        # ── 关键价格标记 ──
        self._log('[PRICE-MAP] stop Y{:.2f} | MA20 Y{:.2f} | 现价 Y{:.2f}'.format(
            stop, ma20, curr_price))

    # ═══ 状态机（与 v0562 相同）═══

    def _handle_idle(self, price):
        st = self.st
        signal = st.get('daily_signal', {})
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
            if actual_sold > 0:
                st['rev_t_sell_fills'] = st.get('rev_t_sell_fills', 0) + 1
            st['buyback_target'] = round(price * (1.0 - buyback_pct), 2)
            st['sell_fill_price'] = price
            st['short_legs'].append((price, actual_sold))
            if status == 'PARTIAL':
                self._log('[REV-T sell PARTIAL] {}'.format(actual_sold))
            st['fstate'] = STATE_SOLD
            self._log_rev_t_next_plan(actual_sold, price, buyback_pct)

    def _log_rev_t_next_plan(self, shares, sell_price, buyback_pct):
        """Log the complete buyback plan immediately after a REV-T sell fill."""
        normal_target = round(sell_price * (1.0 - buyback_pct), 2)
        tightened_pct = buyback_pct * cfg.BUYBACK_TIGHTEN_MULT
        tightened_target = round(sell_price * (1.0 - tightened_pct), 2)
        tighten_condition = round(sell_price * 0.995, 2)
        self._log('[NEXT-PLAN] REV-T buyback {} sh | sold Y{:.2f} | '
                  'touch <= Y{:.2f} (-{:.2f}%) then rebound >= {:.2f}% from low'.format(
                      shares, sell_price, normal_target, buyback_pct * 100,
                      cfg.BOUNCE_PCT * 100))
        self._log('[NEXT-PLAN] after 30 strategy ticks, if price > Y{:.2f}, '
                  'touch line tightens to Y{:.2f} (-{:.2f}%) | '
                  'no end-of-day forced close; unclosed legs remain open'.format(
                      tighten_condition, tightened_target,
                      tightened_pct * 100))

    def _handle_sold(self, price):
        st = self.st
        sp = st['sell_fill_price']
        bt = st['buyback_target']
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
        self._log_rev_t_next_plan(
            self._leg_shares(st['short_legs']), st['sell_fill_price'],
            st['daily_signal']['atr_pct'] * cfg.BUYBACK_TRIGGER_MULT)
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
            if delta > 0:
                self._log_fwd_t_next_plan()

    def _log_fwd_t_next_plan(self):
        """Print the sellback plan after a confirmed FWD-T buy fill."""
        legs = self.st.get('long_legs', [])
        shares = self._leg_shares(legs)
        avg_price = self._leg_avg_price(legs)
        target = self.st.get('bt_sellback_target', 0.0)
        self._log('[NEXT-PLAN] FWD-T sellback {} sh | avg buy Y{:.4f} | '
                  'watch >= Y{:.2f} (+{:.2f}%) then pullback >= {:.2f}% from peak; '
                  'touching watch line alone does not sell'.format(
                      shares, avg_price, target,
                      cfg.SELLBACK_RISE_PCT * 100, cfg.PULLBACK_PCT * 100))
        self._log('[NEXT-PLAN] no end-of-day forced close; unclosed legs '
                  'remain open; normal sellback uses available base holdings')

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
                if sold > 0:
                    self._log_fwd_t_next_plan()

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

    def _log_next_t_entry_plan(self, price, cash, sellable):
        """Publish concrete entry plans once after each closing execution."""
        st = self.st
        signal = st.get('daily_signal') or {}
        pending = st.get('reentry_pending')
        closing = getattr(self, '_last_executed_order', None) or {}
        now = cfg.now_hms()
        block = self._new_leg_block_reason()
        sell_trigger = float(signal.get('sell_trigger', 0) or 0)
        buy_trigger = float(signal.get('buy_trigger', 0) or 0)
        short_reason = block
        long_reason = block
        if not short_reason:
            if not st.get('do_short', False):
                short_reason = self._short_block_reason()
            elif now >= SHORT_NEW_ENTRY_CUTOFF:
                short_reason = 'new REV-T entry cutoff {}'.format(SHORT_NEW_ENTRY_CUTOFF)
            elif st.get('trade_count_short', 0) >= cfg.MAX_DAILY_TRADES:
                short_reason = 'daily REV-T limit reached'
            elif sellable < self.trade_lot or sell_trigger <= 0:
                short_reason = 'sellable shares or sell trigger unavailable'
        if not long_reason:
            if now >= '14:57:00' or (st.get('do_short') and now >= SHORT_NEW_ENTRY_CUTOFF):
                long_reason = 'current entry scheduler cutoff reached'
            elif not st.get('do_long', False):
                long_reason = st.get('long_reason') or 'FWD-T disabled'
            elif st.get('trade_count_long', 0) >= cfg.MAX_DAILY_TRADES:
                long_reason = 'daily FWD-T limit reached'
            elif sellable < self.trade_lot or buy_trigger <= 0:
                long_reason = 'T+1 pairing shares or buy trigger unavailable'
        sell_shares = calculate_rev_t_sell_shares(
            sellable, self.trade_lot, self._rev_t_sell_fraction())
        buy_shares = calculate_t_shares(
            buy_trigger, sellable, self.trade_lot,
            T_TARGET_VALUE, T_POSITION_FRACTION, cash) if buy_trigger > 0 else 0
        if not long_reason and (buy_shares < self.trade_lot or
                                cash < price * self.trade_lot * 1.01):
            long_reason = 'cash insufficient for one lot'
        key = (closing.get('order_id'), st.get('next_t_cycle', 0), bool(pending),
               sell_trigger, buy_trigger, sell_shares, buy_shares,
               short_reason, long_reason)
        if st.get('_next_entry_plan_key') == key:
            return
        st['_next_entry_plan_key'] = key
        if pending:
            self._log('[NEXT-PLAN] next-T WAIT: {}; new entries paused until '
                      'closing fill and ATR thresholds are verified'.format(block))
            return
        self._log('[NEXT-PLAN] next-T #{} | last fill Y{:.4f} | current Y{:.2f} | '
                  'sellable={} cash=Y{:.2f}; quantities rechecked at order time'.format(
                      st.get('next_t_cycle', 0), self._execution_price or 0.0,
                      price, sellable, cash))
        self._log('[NEXT-PLAN] REV-T {} | sell watch >= Y{:.2f}, planned={} sh | '
                  'peak extension >= {:.2f}%, pullback >= {:.2f}%, '
                  'at least {} strategy ticks'.format(
                      'BLOCKED: ' + short_reason if short_reason else 'ENABLED',
                      sell_trigger, sell_shares, SHORT_CONFIRM_MIN_EXTENSION_PCT * 100,
                      SHORT_CONFIRM_MIN_PULLBACK_PCT * 100, SHORT_CONFIRM_MIN_BARS))
        self._log('[NEXT-PLAN] FWD-T {} | buy touch <= Y{:.2f}, planned={} sh | '
                  'rebound >= {:.2f}% from low; sellback watch = actual '
                  'avg buy x (1 + {:.2f}%), then peak pullback >= {:.2f}%'.format(
                      'BLOCKED: ' + long_reason if long_reason else 'ENABLED',
                      buy_trigger, buy_shares, cfg.BOUNCE_PCT * 100,
                      cfg.SELLBACK_RISE_PCT * 100, cfg.PULLBACK_PCT * 100))

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
        self._log_next_t_entry_plan(price, cash, sellable)
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
    #   PRE_MARKET  (<09:30)    盘前准备：daily init + 计划预览
    #   MORNING     (09:30~11:30) 上午盘：风控执行 + 内层交易 + 心跳
    #   LUNCH       (11:30~13:00) 午休：状态快照
    #   AFTERNOON   (13:00~14:57) 下午盘：同上午，最后入场截止
    #   LATE        (14:57~15:00) 尾盘：仅按价格信号平已开交易腿
    #   POST_MARKET (>15:00)    盘后：收市摘要 + 明日预判

    _PERIOD_PRE_MARKET = 'PRE_MARKET'
    _PERIOD_MORNING = 'MORNING'
    _PERIOD_LUNCH = 'LUNCH'
    _PERIOD_AFTERNOON = 'AFTERNOON'
    _PERIOD_LATE = 'LATE'
    _PERIOD_POST_MARKET = 'POST_MARKET'

    def _detect_period(self, now_hms):
        """根据当前时间判定所属时段。"""
        if now_hms < '09:30:00':
            return self._PERIOD_PRE_MARKET
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
        now_hms = cfg.now_hms()
        if now_hms < DAILY_PLAN_TIME:
            return False
        if (self.st.get('initialized', False) and
                self.st.get('trade_date', '') == today):
            return True
        if now_ts - self.st.get('last_init_time', 0.0) < 60.0:
            return False
        self.st['last_init_time'] = now_ts
        self.st['init_attempts'] = self.st.get('init_attempts', 0) + 1
        try:
            self._log('[DAILY-PREPARE] date={} scheduled={} actual={} '
                      'attempt={}; refresh state, prices and thresholds'.format(
                          today, DAILY_PLAN_TIME, now_hms, self.st['init_attempts']))
            tick_data, price = self._get_tick_price()
            if price <= 0 or not self._tick_is_current(tick_data, today, now_ts):
                self._log('[INIT-WAIT] current-day price unavailable; retry in 60s')
                return False
            self._daily_init()
            signal = self.st.get('daily_signal') or {}
            ready = (self.st.get('initialized', False) and
                     self.st.get('trade_date', '') == today and
                     'sell_trigger' in signal and
                     'buy_trigger' in signal)
            if not ready:
                self._log('[INIT-WAIT] {} | retry in 60s; '
                          'no trigger calculation or order allowed'.format(
                              self.st.get('lock_reason') or
                              'daily signal incomplete'))
                return False
            self._print_daily_brief(signal)
            self._log('[DAILY-PLAN-READY] date={} scheduled={} price=Y{:.2f} '
                      'tick={} anchor={} Y{:.2f}; next: wait for trading '
                      'session and threshold confirmation'.format(
                          today, DAILY_PLAN_TIME,
                          signal.get('plan_price', price),
                          signal.get('plan_tick_time', ''),
                          signal.get('open_price_source', ''),
                          signal.get('open_price', 0.0)))
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

    def _tick_is_current(self, tick_data, today, now_ts):
        """Fail closed unless the live tick explicitly belongs to today."""
        tick_time = (tick_data or {}).get('timetag') or (tick_data or {}).get('time')
        tick_date = _normalize_trade_date(tick_time)
        if tick_date == today:
            self._last_tick_guard_reason = ''
            return True
        reason = 'tick_date={} expected={}'.format(tick_date or 'missing', today)
        if (reason != self._last_tick_guard_reason or
                now_ts - self._last_tick_guard_log >= 30.0):
            self._last_tick_guard_reason = reason
            self._last_tick_guard_log = now_ts
            self._log('[STALE-TICK] {}; state machine and orders blocked'.format(
                reason))
        return False

    def _log_market_closed_once(self, today):
        if self._market_closed_logged_date == today:
            return
        self._market_closed_logged_date = today
        self._log('[MARKET-CLOSED] date={}; initialization, state transitions '
                  'and orders blocked'.format(today))

    # ═══ 各时段处理 ═══

    def _handle_pre_market(self, now_ts, today):
        """盘前：等待 daily init，每 300 秒心跳。"""
        self._try_daily_init(now_ts, today)
        if now_ts - self._last_heartbeat >= 300:
            self._last_heartbeat = now_ts
            self._file_log('[WAIT] 盘前 {} 等待开盘'.format(cfg.now_hms()))
        (yield 1)

    def _handle_morning(self, tick_data, price, now_ts):
        """上午盘：风控执行 + 内层交易 + 心跳。"""
        self._update_fwd_buy_trigger(price)
        self._retry_atr_reentry()
        # 09:30 后拿真实开盘价重算 trigger；集合竞价阶段绝不下单。
        self._recalc_open_trigger(tick_data, price)
        self._log_sell_trigger_hit(price)

        if not self._ensure_risk_transition(price, now_ts):
            (yield 1)
            return

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
            votes = self.st.get('risk_entry_vote_count', 0)
            self._file_log('[LUNCH] {} | risk={} votes={}/3 | {} trades today'.format(
                fs, risk, votes, self.total_t_days))
        (yield 10)

    def _handle_afternoon(self, tick_data, price, now_ts):
        """下午盘：同上午，最后入场截止 14:20。"""
        self._update_fwd_buy_trigger(price)
        self._retry_atr_reentry()
        self._log_sell_trigger_hit(price)

        if not self._ensure_risk_transition(price, now_ts):
            (yield 1)
            return

        self._run_inner_state_machine(price, now_ts)

        if now_ts - self._last_heartbeat >= 1800:
            self._last_heartbeat = now_ts
            self._heartbeat(price)
        (yield 0.5)

    def _ensure_risk_transition(self, price, now_ts):
        """记录当日外层观察结果；它永远不阻断内层交易。"""
        st = self.st
        if st.get('risk_switch_done') == st.get('trade_date'):
            return True
        settled = self._apply_risk_switch(price)
        if settled:
            st['risk_switch_done'] = st.get('trade_date')
            self._refresh_capacity()
            return True
        if now_ts - self._last_heartbeat >= 30:
            self._last_heartbeat = now_ts
            self._file_log('[RISK-TRANSITION] status={}'.format(
                st.get('risk_transition_status', 'PENDING')))
        return False

    def _handle_late(self, tick_data, price, now_ts):
        """Close existing legs only on normal price signals; never force close."""
        fstate = self.st.get('fstate', STATE_IDLE)
        if fstate == STATE_SOLD:
            self._handle_sold(price)
        elif fstate == STATE_DIPPING:
            self._handle_dipping(price)
        elif fstate == STATE_BT_BOUGHT:
            self._handle_bt_bought(price)
        elif fstate == STATE_BT_SPIKING:
            self._handle_bt_spiking(price)
        if now_ts - self._last_heartbeat >= 30:
            self._last_heartbeat = now_ts
            fs = self.st.get('fstate', STATE_IDLE)
            self._file_log('[LATE] {} Y{:.2f} normal price signals only; '
                           'unclosed legs remain open at market close'.format(fs, price))
        (yield 1)

    def _verified_close(self, tick_data, now_ts, today, now_hms):
        """Return the finalized same-day unadjusted daily close, never a tick."""
        if now_hms < POST_CLOSE_DATA_READY_TIME:
            return None, ''
        if now_ts - self._last_close_attempt < CLOSE_RETRY_SEC:
            return None, ''
        self._last_close_attempt = now_ts
        self.conn.refresh_daily_cache()
        snapshot = self.conn.load_daily_snapshot(
            5, today=today,
            tick_last_close=float((tick_data or {}).get('lastClose', 0) or 0),
            tick_time=((tick_data or {}).get('timetag') or
                       (tick_data or {}).get('time')),
            retries=1, retry_delay=0.0, now_hms=now_hms)
        if snapshot is None or snapshot.get('last_complete_date') != today:
            actual = snapshot.get('last_complete_date') if snapshot else 'unavailable'
            self._log('[CLOSE-WAIT] expected={} actual={}; retry in {}s'.format(
                today, actual, int(CLOSE_RETRY_SEC)))
            return None, ''
        close_price = float(snapshot['raw'].iloc[-1]['close'])
        if not math.isfinite(close_price) or close_price <= 0:
            self._log('[CLOSE-WAIT] invalid finalized close {}; retry in {}s'.format(
                close_price, int(CLOSE_RETRY_SEC)))
            return None, ''
        return close_price, 'RAW-DAILY'

    def _handle_post_market(self, tick_data, now_ts, today, now_hms):
        """盘后：正式日线可用后只打印一次收市摘要。"""
        if not self.st.get('_post_market_printed', False):
            verified_close = self._verified_close(
                tick_data, now_ts, today, now_hms)
            price = verified_close[0]
            source = verified_close[1]
            if price is not None:
                self.st['_post_market_printed'] = True
                self._print_post_market(price, today, source)
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
        if (self.st.get('fstate') == STATE_DONE and
                now < '14:57:00'):
            self._try_resume(now_ts)

    # ═══ 主循环 ═══

    def run(self):
        self._init_state()
        self._log('[START] {} {}'.format(
            self.version, 'SIGNAL' if self.dry_run else 'LIVE'))
        today = datetime.now().strftime('%Y%m%d')
        if not self.portfolio.is_trading_day(today, self.stock_qmt):
            self._log_market_closed_once(today)

        try:
            while self._running:
                now = cfg.now_hms()
                now_ts = _time.time()
                today = datetime.now().strftime('%Y%m%d')
                period = self._detect_period(now)

                if not self.portfolio.is_trading_day(today, self.stock_qmt):
                    self._log_market_closed_once(today)
                    (yield 30)
                    continue

                # ── 盘后首次进入：打收市摘要 ──
                if period == self._PERIOD_POST_MARKET:
                    tick_result = self._get_tick_price()
                    tick_data = tick_result[0] or {}
                    yield from self._handle_post_market(
                        tick_data, now_ts, today, now)
                    continue

                # ── 盘前：等 init ──
                if period == self._PERIOD_PRE_MARKET:
                    yield from self._handle_pre_market(now_ts, today)
                    continue

                # 日线初始化失败时只定时重试，绝不允许不完整的
                # daily_signal 进入触发价计算或任何下单路径。
                if not self._try_daily_init(now_ts, today):
                    (yield 1)
                    continue

                # ── 以下时段需要 tick 数据 ──
                tick_data, price = self._get_tick_price()
                if price <= 0:
                    (yield 1)
                    continue

                # ── 午休：只打快照 ──
                if period == self._PERIOD_LUNCH:
                    yield from self._handle_lunch(now_ts)
                    continue

                if not self._tick_is_current(tick_data, today, now_ts):
                    (yield 1)
                    continue

                # ── 尾盘：正常信号平仓 ──
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
            self._log('[STOP] {} v0565 cum {} days gross~Y{:,.0f}'.format(
                self.stock_name, self.total_t_days, self.total_pnl))

    def _heartbeat(self, price):
        """每分钟的状态监控：当前状态 + 距离下一步的幅度 + 触发条件。"""
        st = self.st
        fs = st.get('fstate', STATE_IDLE)
        risk_on = st.get('risk_on', True)
        position_peak = st.get('risk_position_peak', 0)
        market_peak = st.get('risk_market_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = position_peak - RISK_K_ATR * atr if atr > 0 else 0
        ma20 = st.get('risk_ma20', 0)

        self._file_log('[HB] {} Y{:.2f} | risk-observe={} transition={} votes={}/3 '
                       'confirm={}/{}'.format(
                           fs, price, 'ON' if risk_on else 'OFF',
                           st.get('risk_transition_status', ''),
                           st.get('risk_entry_vote_count', 0),
                           st.get('risk_entry_confirmation_streak', 0),
                           RISK_ENTRY_CONFIRM_DAYS))

        # ── 距离关键价格 ──
        if atr > 0:
            d_stop = price - stop
            d_ma20 = price - ma20
            self._file_log('  epoch-peak Y{:.2f} stop Y{:.2f} ({:+.1f}) '
                           'MA20 Y{:.2f} ({:+.1f}) | market-peak Y{:.2f}'.format(
                               position_peak, stop, d_stop, ma20, d_ma20,
                               market_peak))

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
                    self._short_block_reason()))
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
        elif fs == STATE_DONE:
            # T 周期已结束：显示下次触发价和容量状态
            next_trig = self._rev_sell_trigger()
            sellable = int(st.get('base_can_use', 0))
            cash = self._available_cash()
            gap = next_trig - price if next_trig > 0 and price > 0 else 0
            self._file_log('  T-DONE | next sell Y{:.2f} ({:+.1f}) | '
                          'sellable={} cash=Y{:.0f}'.format(
                              next_trig, gap, sellable, cash))

    def _print_post_market(self, price, close_date, close_source):
        """盘后收市摘要：周期止损、多票回场、账户与内层状态。"""
        st = self.st
        risk_on = st.get('risk_on', True)
        exit_s = st.get('risk_exit_signal', False)
        entry_s = st.get('risk_entry_signal', False)
        position_peak = st.get('risk_position_peak', 0)
        market_peak = st.get('risk_market_peak', 0)
        atr = st.get('risk_atr', 0)
        stop = position_peak - RISK_K_ATR * atr if atr > 0 else 0
        ma20 = st.get('risk_ma20', 0)
        base_shares = st.get('base_shares', 0)
        avail_cash = st.get('avail_cash', 0)
        flips = st.get('_risk_flip_count', 0)
        day_pnl = st.get('day_pnl', 0)
        votes = st.get('risk_entry_votes') or {}
        vote_count = st.get('risk_entry_vote_count', 0)
        vote_streak = st.get('risk_entry_confirmation_streak', 0)

        self._log('[CLOSE] date={} Y{:.2f} source={} | risk-observe={} '
                  'exit={} reentry={} | flips={}'.format(
                      close_date, price, close_source,
                      'ON' if risk_on else 'OFF', exit_s, entry_s, flips))
        self._log('[ANCHORS] epoch-peak Y{:.2f} stop Y{:.2f} ATR {:.2f} | '
                  'market-peak(observe-only) Y{:.2f} | MA20 Y{:.2f}'.format(
                      position_peak, stop, atr, market_peak, ma20))
        self._log('[VOTES] above-MA20={} MA20-rising={} short-breakout={} '
                  '=> {}/3; confirm {}/{}'.format(
                      votes.get('above_ma20', False),
                      votes.get('ma20_rising', False),
                      votes.get('short_breakout', False),
                      vote_count, vote_streak, RISK_ENTRY_CONFIRM_DAYS))
        if risk_on:
            self._log('[NEXT-DAY] 观察当前持仓周期峰值止损；不参与实盘')
        else:
            self._log('[NEXT-DAY] 观察回场条件：至少 {}/3 票并连续 {} 日；'
                      '不参与实盘'.format(
                RISK_ENTRY_MIN_VOTES, RISK_ENTRY_CONFIRM_DAYS))

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
        kwargs['align_leading_window'] = True
        return MiniQMTConnector.load_daily_snapshot(self, *args, **kwargs)


class PortfolioRunner:
    """One cooperative scheduler with isolated state per account holding."""

    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.conn = MiniQMTConnector()
        self.runners = {}
        self.tasks = {}
        self.last_refresh = None
        self.order_uncertain = False
        self.own_order_ids = set()
        self._trading_day_cache = {}
        self._trading_day_checked_at = {}
        self._calendar_date = None
        self._last_scheduler_tick = None
        self._last_scheduler_wall = ''
        self._last_portfolio_heartbeat = None

    def is_trading_day(self, today, stock_qmt=''):
        """Use QMT's exchange calendar and fail closed when it is unavailable."""
        market = stock_qmt.split('.')[-1] if '.' in stock_qmt else 'SH'
        key = (today, market)
        now = _time.monotonic()
        if key in self._trading_day_cache:
            if self._trading_day_cache[key]:
                return True
            if now - self._trading_day_checked_at[key] < TRADING_CALENDAR_RETRY_SEC:
                return False
        previous = self._trading_day_cache.get(key)
        normalized = []
        try:
            dates = self.conn.xtdata.get_trading_dates(
                market, start_time='', end_time=today, count=1)
            normalized = [_normalize_trade_date(value) for value in (dates or [])]
            result = bool(normalized and normalized[-1] == today)
        except Exception as error:
            result = False
            _log('[TRADING-CALENDAR-ERROR] date={} market={} {}; fail-closed'.format(
                today, market, error))
        self._trading_day_cache[key] = result
        self._trading_day_checked_at[key] = _time.monotonic()
        if not result:
            _log('[TRADING-CALENDAR-WAIT] date={} market={} returned={} '
                 'trading_day=False retry={}s; fail-closed'.format(
                     today, market, normalized, int(TRADING_CALENDAR_RETRY_SEC)))
        elif previous is False:
            _log('[TRADING-CALENDAR-RECOVERED] date={} market={} '
                 'trading_day=True'.format(today, market))
        return result

    def _portfolio_trading_day(self, today):
        code = next(iter(self.runners), '000001.SH')
        return self.is_trading_day(today, code)

    def _observe_date_rollover(self, today):
        if self._calendar_date is None:
            self._calendar_date = today
            return
        if self._calendar_date == today:
            return
        old = self._calendar_date
        self._calendar_date = today
        trading_day = self._portfolio_trading_day(today)
        _log('[DATE-ROLLOVER] old={} new={} trading_day={}'.format(
            old, today, trading_day))

    def _observe_scheduler(self, now, wall_now):
        if self._last_scheduler_tick is not None:
            elapsed = now - self._last_scheduler_tick
            if elapsed >= SCHEDULER_LAG_WARN_SEC:
                _log('[SCHEDULER-LAG] elapsed={:.1f}s previous={} current={}; '
                     'host suspension or blocking call detected'.format(
                         elapsed, self._last_scheduler_wall, wall_now))
        self._last_scheduler_tick = now
        self._last_scheduler_wall = wall_now

    def _emit_liveness_heartbeat(self, now, today, now_hms, trading_day):
        if trading_day and now_hms <= '15:00:00':
            return
        if (self._last_portfolio_heartbeat is not None and
                now - self._last_portfolio_heartbeat < PORTFOLIO_HEARTBEAT_SEC):
            return
        self._last_portfolio_heartbeat = now
        state = 'POST-MARKET' if trading_day else 'MARKET-CLOSED'
        _log('[PORTFOLIO-HB] date={} time={} trading_day={} state={} '
             'runners={} interval={}s'.format(
                 today, now_hms, trading_day, state, len(self.runners),
                 int(PORTFOLIO_HEARTBEAT_SEC)))

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
                runner._lock_all_trading(
                    'previous trading-day T leg still open; manual review required')
                continue
            runner.execution_book = ExecutionBook()
            runner._daily_init()

    def save_checkpoint(self, force=False, settled=False):
        return None

    def refresh_holdings(self, now):
        """Discover positive account holdings and add one independent runner each."""
        if (self.last_refresh is not None and
                now - self.last_refresh < PORTFOLIO_REFRESH_SEC):
            return
        self.last_refresh = now
        try:
            started = _time.monotonic()
            positions = self.conn.trader.query_stock_positions(
                self.conn._account_obj)
            elapsed = _time.monotonic() - started
            if elapsed >= SCHEDULER_LAG_WARN_SEC:
                _log('[HOLDINGS-LAG] elapsed={:.1f}s; account position query '
                     'blocked the cooperative scheduler'.format(elapsed))
            if positions is None:
                raise RuntimeError('position query returned None')
        except Exception as error:
            _log('[PORTFOLIO-ALERT] holdings query failed: {}'.format(error))
            return
        for position in positions:
            code = str(getattr(position, 'stock_code', '') or '')
            shares = int(getattr(position, 'volume', 0) or 0)
            if shares <= 0 or code in self.runners:
                continue
            if '.' not in code:
                _log('[PORTFOLIO-SKIP] missing exchange suffix: {}'.format(code))
                continue
            stock_name = str(getattr(position, 'stock_name', '') or code)
            runner = StrategyRunner(self, code, stock_name)
            self.runners[code] = runner
            self.tasks[code] = (runner.run(), 0.0)
            _log('[PORTFOLIO-ADD] {} {} shares={} lot={}'.format(
                code, stock_name, shares, runner.trade_lot))

    def _run_due_tasks(self, now):
        for code, (task, due) in list(self.tasks.items()):
            if now < due:
                continue
            set_global_conn(self.conn, self.dry_run)
            started = _time.monotonic()
            try:
                delay = next(task)
                finished = _time.monotonic()
                elapsed = finished - started
                if elapsed >= SCHEDULER_LAG_WARN_SEC:
                    _log('[WORKER-LAG] code={} elapsed={:.1f}s; inspect the '
                         'worker\'s latest market/account/data call'.format(
                             code, elapsed))
                next_due = finished + float(delay or 0.0)
                self.tasks[code] = (task, next_due)
            except StopIteration:
                del self.tasks[code]
                _log('[PORTFOLIO-ALERT] {} worker stopped; '
                     'inspect its error log'.format(code))

    def run(self):
        set_global_conn(self.conn, self.dry_run)
        try:
            if not self.conn.connect_data():
                _log('[PORTFOLIO-ERROR] market connection failed')
                return
            if not self.conn.connect_trade():
                _log('[PORTFOLIO-ERROR] account connection failed')
                return
            connected_account = str(
                getattr(self.conn._account_obj, 'account_id', '') or '')
            if connected_account and connected_account != str(ACCOUNT):
                _log('[PORTFOLIO-ERROR] connected account differs from configured account')
                return
            _log('[PORTFOLIO-START] v0565 multi-symbol account holdings; mode={} '
                 'risk=OBSERVE-ONLY REV-T-first={:.0f}% later=100%'.format(
                     'SIGNAL' if self.dry_run else 'LIVE',
                     REV_T_SELL_FRACTION * 100))
            while True:
                now = _time.monotonic()
                wall = datetime.now()
                today = wall.strftime('%Y%m%d')
                now_hms = wall.strftime('%H:%M:%S')
                self._observe_scheduler(now, wall.strftime('%Y-%m-%d %H:%M:%S'))
                self._observe_date_rollover(today)
                trading_day = self._portfolio_trading_day(today)
                self._emit_liveness_heartbeat(
                    now, today, now_hms, trading_day)
                self.refresh_holdings(now)
                self._run_due_tasks(now)
                _time.sleep(0.2)
        except (KeyboardInterrupt, SystemExit):
            _log('[PORTFOLIO-STOP] interrupted')
        finally:
            for task, _ in self.tasks.values():
                task.close()
            try:
                self.conn.disconnect()
            except Exception:
                pass


def source_fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source_file:
        for block in iter(lambda: source_file.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='v0565: multi-symbol DayT for all account holdings')
    parser.add_argument('--mode', default='signal', choices=['signal', 'live'])
    args = parser.parse_args()
    logger = FileLogger('portfolio', version='v0565')
    set_logger(logger)
    try:
        source_path = os.path.abspath(__file__)
        _log('[SOURCE] path={} mtime={} sha256={}'.format(
            source_path,
            datetime.fromtimestamp(os.path.getmtime(source_path)).isoformat(),
            source_fingerprint(source_path)))
        if args.mode == 'live':
            print('LIVE: v0565 applies DayT to ALL current and newly detected '
                  'account holdings. Account: {}'.format(ACCOUNT))
            if input('Type yes to continue: ').strip().lower() != 'yes':
                return
        PortfolioRunner(dry_run=args.mode == 'signal').run()
    finally:
        logger.close()


if __name__ == '__main__':
    main()
