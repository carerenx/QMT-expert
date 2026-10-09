# -*- coding: utf-8 -*-
"""
infra/connector.py — MiniQMT 连接层 + QMT 接口模拟
===================================================
  MiniQMTConnector  : 封装 xtdata + xttrader (行情/交易)
  MockContextInfo    : 模拟 QMT ContextInfo 对象
  MockPosition/Account : 模拟 QMT 持仓/账户对象
  get_trade_detail_data / order_shares : 模拟 QMT 全局函数
"""
import time as _time
import traceback as _traceback
import math
from datetime import datetime, timedelta
from typing import Optional

from core import config as cfg
from .logger import _log

# ============================================================================
# 全局引用 (策略主循环中初始化)
# ============================================================================

_global_conn: Optional['MiniQMTConnector'] = None
_global_dry_run = False


def set_global_conn(conn: 'MiniQMTConnector', dry_run: bool = False):
    """设置全局连接引用 (StrategyRunner.run() 中调用)"""
    global _global_conn, _global_dry_run
    _global_conn = conn
    _global_dry_run = dry_run


def _pick(obj, *names, default=None):
    """按顺序取第一个存在的字段值。

    兼容 xttrader 委托/成交对象的多种结构:
      - 官方 XtOrder/XtTrade: order_status / price / traded_volume / order_volume ...
      - 原生 C++ 对象: m_nOrderStatus / m_dPrice / m_nTradedVolume ...
      - dict / SON 对象: obj['字段'] 下标访问 (兜底)
    """
    for n in names:
        # 1) 属性访问 (pybind11 C++ 对象 / Python 类)
        try:
            v = getattr(obj, n)
        except Exception:
            v = None
        if v is not None:
            return v
        # 2) 下标访问 (dict / SON)
        try:
            v = obj[n]
        except Exception:
            continue
        if v is not None:
            return v
    return default


# Keep the current daily bar only after the exchange-close data has had a
# 10-minute settlement buffer.  Before this, it is still an intraday bar.
DAILY_BAR_FINALIZE_TIME = '15:10:00'


def exclude_incomplete_daily_bar(dataframe, today=None, keep_current_day=False):
    """Remove today's forming daily bar unless the post-close window has passed."""
    if dataframe is None or len(dataframe) == 0:
        return dataframe, False
    today_key = today or datetime.now().strftime('%Y%m%d')
    last_key = str(dataframe.index[-1]).replace('-', '')[:8]
    if last_key == today_key and not keep_current_day:
        return dataframe.iloc[:-1].copy(), True
    return dataframe, False


def _normalize_trade_date(value):
    """Normalize xtdata/string/pandas date values to YYYYMMDD."""
    if value is None:
        return ''
    if hasattr(value, 'strftime'):
        try:
            return value.strftime('%Y%m%d')
        except Exception:
            pass
    if isinstance(value, (int, float)):
        stamp = float(value)
        # xtdata/pandas may expose a YYYYMMDD index as an integer.  Treat it
        # as a calendar key before considering Unix seconds/milliseconds.
        if stamp.is_integer() and 19000101 <= stamp <= 29991231:
            return str(int(stamp))
        if stamp > 100000000000:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp).strftime('%Y%m%d')
        except Exception:
            return ''
    text = str(value).replace('-', '').replace('/', '')
    return text[:8] if len(text) >= 8 else text


def _daily_values_valid(dataframe):
    """Return False when a complete daily bar contains unusable numbers."""
    for field in ('open', 'high', 'low', 'close'):
        for value in dataframe[field]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(number) or number <= 0:
                return False
    for field in ('volume', 'amount'):
        for value in dataframe[field]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(number) or number < 0:
                return False
    return True


# ============================================================================
# MiniQMTConnector
# ============================================================================

class MiniQMTConnector:
    """封装 xtdata + xttrader 连接, 提供行情/交易查询/下单接口"""

    def __init__(self):
        self.xtdata = None
        self.xttrader = None
        self.trader = None
        self.callback = None
        self._data_connected = False
        self._trade_connected = False
        self._account_obj = None
        self._daily_data_cache = None
        self._daily_raw_cache = None
        self._daily_snapshot_meta = None

        self.last_order_status = None
        self.last_order_id = None
        self.last_trade = None
        self.order_pending = False
        # ★ 下单快照 (方向, 价格, 数量, 代码) — 推送对象价格/数量字段为 0 时用于回退
        self.last_order_info = None
        self._order_field_dumped = False

    # ── 连接 / 断开 ──

    def connect_data(self):
        """连接行情 (xtdata)"""
        from xtquant import xtdata as _xtdata
        self.xtdata = _xtdata
        try:
            _xtdata.connect()
            self._data_connected = True
            _log('[连接] MiniQMT 行情服务已连接')
            return True
        except Exception as e:
            _log(f'[连接] 行情服务连接失败: {e}')
            _log('[提示] 请先启动 MiniQMT (QMT → 右上角"极简mode")')
            return False

    def connect_trade(self, account_id=cfg.ACCOUNT, path=cfg.MINIQMT_PATH, session=cfg.SESSION_ID):
        """连接交易 (xttrader)"""
        from xtquant import xttrader as _xttrader, xtconstant

        class _Callback(_xttrader.XtQuantTraderCallback):
            def __init__(self, parent):
                super().__init__()
                self.parent = parent

            def on_connected(self):
                _log('[交易] MiniQMT 交易服务已连接')

            def on_disconnected(self, reason):
                _log(f'[交易] 连接断开: {reason}')

            def on_stock_order(self, order):
                from xtquant import xtconstant
                sm = {
                    xtconstant.ORDER_UNREPORTED: '未报',
                    xtconstant.ORDER_WAIT_REPORTING: '待报',
                    xtconstant.ORDER_REPORTED: '已报',
                    xtconstant.ORDER_REPORTED_CANCEL: '已报待撤',
                    xtconstant.ORDER_PARTSUCC_CANCEL: '部成待撤',
                    xtconstant.ORDER_PART_CANCEL: '部撤',
                    xtconstant.ORDER_CANCELED: '已撤',
                    xtconstant.ORDER_PART_SUCC: '部成',
                    xtconstant.ORDER_SUCCEEDED: '全成',
                    xtconstant.ORDER_JUNK: '废单',
                }
                # ★ xttrader 委托对象字段 (XtOrder): order_status/price/traded_volume/order_volume
                #   _pick 兼容原生 m_ 前缀字段 (m_nOrderStatus/m_dPrice/m_nTradedVolume/m_nOrderVolume)
                status = _pick(order, 'order_status', 'm_nOrderStatus', default=-1)
                if status in sm:
                    # 买卖方向: 优先 order_type(23买/24卖), 回退 offset_flag(48开/49平)
                    otype = _pick(order, 'order_type', 'm_nOrderType', default=0)
                    offset_flag = _pick(order, 'offset_flag', 'm_nOffsetFlag', default=0)
                    if otype == xtconstant.STOCK_BUY:
                        d = '买'
                    elif otype == xtconstant.STOCK_SELL:
                        d = '卖'
                    elif offset_flag == xtconstant.OFFSET_FLAG_CLOSE:
                        d = '卖'
                    else:
                        d = '买'
                    p = _pick(order, 'price', 'm_dPrice', 'm_dLimitPrice', default=0.0)
                    vt = _pick(order, 'traded_volume', 'm_nTradedVolume', 'm_nVolumeTraded', default=0)
                    vo = _pick(order, 'order_volume', 'm_nOrderVolume', 'm_nVolumeTotalOriginal', default=0)
                    # ★ 一次性诊断: 推送对象价格/委托量字段为 0 时 dump 全部字段, 排查真实字段名/值
                    if (p <= 0 and vo <= 0) and not self.parent._order_field_dumped:
                        self.parent._order_field_dumped = True
                        _log('[委托诊断] 推送对象字段值:')
                        for attr in dir(order):
                            if not attr.startswith('_'):
                                try:
                                    _log(f'  {attr} = {getattr(order, attr)}')
                                except Exception:
                                    pass
                    # ★ 回退: 推送对象价格/委托量字段为 0 时, 用下单快照补齐真实价格/手数
                    sub = self.parent.last_order_info
                    if sub is not None:
                        _, _sub_price, _sub_shares, _ = sub
                        if p <= 0:
                            p = _sub_price
                        if vo <= 0:
                            vo = _sub_shares
                    _log(f'[委托] {d} Y{p:.2f} {vt}/{vo}股 -> {sm[status]}')
                    # 终态: 已撤/废单/全成 → 清除pending
                    if status in (xtconstant.ORDER_CANCELED, xtconstant.ORDER_JUNK,
                                  xtconstant.ORDER_SUCCEEDED):
                        self.parent.order_pending = False
                    # 废单时dump全部字段用于排错
                    if status == xtconstant.ORDER_JUNK:
                        _log('[废单诊断] 委托对象字段:')
                        for attr in dir(order):
                            if not attr.startswith('_'):
                                try:
                                    _log(f'  {attr} = {getattr(order, attr)}')
                                except Exception:
                                    pass
                self.parent.last_order_status = status
                oid = _pick(order, 'order_id', 'm_nOrderID', default=None)
                if oid is not None:
                    self.parent.last_order_id = oid

            def on_stock_trade(self, trade):
                from xtquant import xtconstant
                # 股票成交: 方向优先 order_type(23买/24卖), 回退 offset_flag(48开/49平)
                otype = _pick(trade, 'order_type', 'm_nOrderType', default=0)
                offset_flag = _pick(trade, 'offset_flag', 'm_nOffsetFlag', default=0)
                if otype == xtconstant.STOCK_BUY:
                    d = '买'
                elif otype == xtconstant.STOCK_SELL:
                    d = '卖'
                elif offset_flag == xtconstant.OFFSET_FLAG_CLOSE:
                    d = '卖'
                else:
                    d = '买'
                p = _pick(trade, 'traded_price', 'm_dTradedPrice', 'm_dPrice', default=0.0)
                v = _pick(trade, 'traded_volume', 'm_nTradedVolume', 'm_nVolume', default=0)
                code = _pick(trade, 'stock_code', 'm_strInstrumentID', default='')
                # ★ 回退: 推送对象价格/数量/代码字段为空时, 用下单快照补齐
                sub = self.parent.last_order_info
                if sub is not None:
                    _, _sub_price, _sub_shares, _sub_code = sub
                    if p <= 0:
                        p = _sub_price
                    if v <= 0:
                        v = _sub_shares
                    if not code:
                        code = _sub_code
                if not code:
                    code = cfg.STOCK_QMT
                _log(f'[成交] {d} {code} Y{p:.2f} x {v}股 = Y{p*v:,.0f}')
                self.parent.last_trade = trade
                self.parent.order_pending = False

            def on_stock_position(self, position):
                pass

            def on_stock_asset(self, asset):
                pass

            def on_order_error(self, order_error):
                msg = getattr(order_error, 'error_msg', str(order_error))
                _log(f'[下单错误] {msg}')
                self.parent.order_pending = False

            def on_cancel_error(self, cancel_error):
                _log(f'[撤单错误] {cancel_error}')

            def on_account_status(self, account_status):
                # 提取字段 (XtAccountStatus: account_id, account_type, status)
                acc_id = getattr(account_status, 'account_id', '?')
                acc_type = getattr(account_status, 'account_type', -1)
                acc_stat = getattr(account_status, 'status', -1)

                # 状态映射
                stat_map = {
                    xtconstant.ACCOUNT_STATUS_OK: '正常',
                    xtconstant.ACCOUNT_STATUS_WAITING_LOGIN: '等待登录',
                    xtconstant.ACCOUNT_STATUS_FAIL: '失败',
                    xtconstant.ACCOUNT_STATUS_INITING: '初始化中',
                    xtconstant.ACCOUNT_STATUS_CORRECTING: '校正中',
                    xtconstant.ACCOUNT_STATUS_CLOSED: '已关闭',
                    xtconstant.ACCOUNT_STATUS_ASSIS_FAIL: '辅助失败',
                    xtconstant.ACCOUNT_STATUS_DISABLEBYSYS: '系统禁用',
                    xtconstant.ACCOUNT_STATUS_DISABLEBYUSER: '用户禁用',
                }
                type_map = xtconstant.ACCOUNT_TYPE_DICT
                stat_name = stat_map.get(acc_stat, f'未知({acc_stat})')
                type_name = type_map.get(acc_type, f'未知({acc_type})')

                # 去重: 只有状态变化时才打印
                last = getattr(self.parent, '_last_acc_status', None)
                new_key = (acc_id, acc_type, acc_stat)
                if new_key != last:
                    self.parent._last_acc_status = new_key
                    _log(f'[账号状态] {acc_id} {type_name}: {stat_name}')
                # 非正常状态始终打印(不管是否重复)
                elif acc_stat != xtconstant.ACCOUNT_STATUS_OK:
                    _log(f'[账号状态] {acc_id} {type_name}: {stat_name} (重复)')

        self.callback = _Callback(self)
        self.xttrader = _xttrader
        self.trader = _xttrader.XtQuantTrader(path, session, self.callback)

        try:
            self.trader.start()
            ret = self.trader.connect()
            if ret != 0:
                _log(f'[交易] 连接失败, 返回码: {ret}')
                return False

            accounts = self.trader.query_account_infos()
            if not accounts:
                _log('[交易] 未找到资金账号 — 请在MiniQMT中登录账号')
                return False

            for acc in accounts:
                if acc.account_id == account_id:
                    self._account_obj = acc
                    break
            if self._account_obj is None:
                self._account_obj = accounts[0]
                _log(f'[交易] 使用账号: {self._account_obj.account_id}')

            self.trader.subscribe(self._account_obj)
            _time.sleep(0.5)
            self._trade_connected = True
            _log(f'[交易] 已订阅账号 {self._account_obj.account_id}')
            return True
        except Exception as e:
            _log(f'[交易] 连接异常: {e}')
            _traceback.print_exc()
            return False

    def disconnect(self):
        """断开所有连接"""
        if self._trade_connected and self._account_obj:
            try:
                self.trader.unsubscribe(self._account_obj)
                self.trader.stop()
            except Exception:
                pass
            self._trade_connected = False
        if self._data_connected:
            try:
                self.xtdata.disconnect()
            except Exception:
                pass
            self._data_connected = False

    @property
    def data_connected(self):
        return self._data_connected

    @property
    def trade_connected(self):
        return self._trade_connected

    # ── 数据查询 ──

    def get_intraday_bars(self, period='10m', count=200):
        """
        Fetch intraday bar data from xtdata.

        Args:
            period: bar period ('1m', '5m', '10m', '15m', '30m', '1h')
            count: number of bars to fetch

        Returns:
            dict with 'close', 'open', 'high', 'low' lists, or None
        """
        code = cfg.STOCK_QMT
        try:
            # Step 1: auto-download missing intraday bars from server
            self.xtdata.download_history_data(
                code, period=period, start_time='', end_time=''
            )

            # Step 2: read from local cache
            df = self.xtdata.get_market_data(
                field_list=['open', 'high', 'low', 'close', 'volume'],
                stock_list=[code],
                period=period,
                count=count,
                dividend_type='front',
            )
            if df is not None and len(df) > 0:
                # get_market_data returns DataFrame directly (single stock)
                if len(df) > count:
                    df = df.iloc[-count:]
                return {
                    'close': df['close'].values.tolist(),
                    'open': df['open'].values.tolist(),
                    'high': df['high'].values.tolist(),
                    'low': df['low'].values.tolist(),
                    'volume': df['volume'].values.tolist(),
                }
            return None
        except Exception as e:
            _log(f'[Intraday] fetch error: {e}')
            return None

    def refresh_daily_cache(self):
        """强制刷新日线数据缓存（跨日时调用）"""
        self._daily_data_cache = None
        self._daily_raw_cache = None
        self._daily_snapshot_meta = None

    def load_daily_snapshot(self, length, today=None, tick_last_close=0.0,
                            tick_time=None, retries=3, retry_delay=1.0,
                            now_hms=None, stock_code=None,
                            align_leading_window=False):
        """Load aligned, fresh complete daily bars from MiniQMT's active data source.

        Returns a dict with ``adjusted`` (front-adjusted indicator data), ``raw``
        (unadjusted validation data), and ``last_complete_date``.  Returns None
        when freshness or price-source validation fails after all retries.
        """
        code = stock_code or cfg.STOCK_QMT
        today_key = today or datetime.now().strftime('%Y%m%d')
        current_hms = now_hms or datetime.now().strftime('%H:%M:%S')
        keep_current_day = current_hms >= DAILY_BAR_FINALIZE_TIME
        end = today_key
        start = (datetime.now() - timedelta(days=365 * 6)).strftime('%Y%m%d')
        fields = ['open', 'high', 'low', 'close', 'volume', 'amount']
        attempts = max(1, int(retries))
        query_count = length + 1 if align_leading_window else length

        for attempt in range(1, attempts + 1):
            try:
                # BigQMT caches each adjustment type separately.  Refresh both
                # keys so the adjusted indicator series and raw validation
                # series are drawn from matching history windows.
                for dividend_type in ('front', 'none'):
                    self.xtdata.download_history_data(
                        code, period='1d', start_time='', end_time='',
                        dividend_type=dividend_type)
            except Exception as e:
                _log('[DailyData] download failed attempt {}/{}: {}'.format(
                    attempt, attempts, e))

            try:
                adjusted_data = self.xtdata.get_local_data(
                    field_list=fields, stock_list=[code], period='1d',
                    start_time=start, end_time=end, count=query_count,
                    dividend_type='front')
                raw_data = self.xtdata.get_local_data(
                    field_list=fields, stock_list=[code], period='1d',
                    start_time=start, end_time=end, count=query_count,
                    dividend_type='none')
            except Exception as e:
                _log('[DailyData] read failed attempt {}/{}: {}'.format(
                    attempt, attempts, e))
                adjusted_data = {}; raw_data = {}

            adjusted = adjusted_data.get(code) if adjusted_data else None
            raw = raw_data.get(code) if raw_data else None
            reason = ''
            tick_last_close_one_day_stale = False
            tick_last_close_adjusted_match = False
            if adjusted is None or raw is None or len(adjusted) == 0 or len(raw) == 0:
                reason = 'daily data missing'
            else:
                adjusted = adjusted.copy()
                raw = raw.copy()
                adjusted.index = [_normalize_trade_date(v) for v in adjusted.index]
                raw.index = [_normalize_trade_date(v) for v in raw.index]
                adjusted, removed_adjusted = exclude_incomplete_daily_bar(
                    adjusted, today=today_key, keep_current_day=keep_current_day)
                raw, removed_raw = exclude_incomplete_daily_bar(
                    raw, today=today_key, keep_current_day=keep_current_day)
                if removed_adjusted or removed_raw:
                    _log('[DailyData] excluded incomplete current-day bar {}'.format(today_key))
                elif (keep_current_day and len(adjusted) > 0 and
                      _normalize_trade_date(adjusted.index[-1]) == today_key):
                    _log('[DailyData] retained post-close current-day bar {} '
                         '(after {})'.format(today_key, DAILY_BAR_FINALIZE_TIME))

                # Health-gate the same window the strategy will consume.  Old
                # suspended/legacy records outside that window are irrelevant.
                adjusted = adjusted.tail(length)
                raw = raw.tail(length)

                if align_leading_window and not adjusted.index.equals(raw.index):
                    shorter = adjusted if len(adjusted) <= len(raw) else raw
                    longer = raw if shorter is adjusted else adjusted
                    suffix = longer.index[-len(shorter):] if len(shorter) else []
                    if len(shorter) and shorter.index.equals(suffix):
                        common_index = shorter.index
                        adjusted = adjusted.loc[common_index]
                        raw = raw.loc[common_index]
                        _log('[DailyData ALIGN] ignored leading window-only '
                             'date difference; common_count={} first={} last={}'.format(
                                 len(common_index), common_index[0], common_index[-1]))

                missing_cols = [f for f in fields if f not in adjusted.columns or f not in raw.columns]
                if missing_cols:
                    reason = 'missing fields {}'.format(','.join(missing_cols))
                elif not adjusted.index.equals(raw.index):
                    reason = 'adjusted/raw date indexes differ'
                elif len(adjusted) == 0:
                    reason = 'no complete daily bars'
                elif not _daily_values_valid(adjusted) or not _daily_values_valid(raw):
                    reason = 'non-finite or invalid daily values'
                else:
                    try:
                        trade_dates = self.xtdata.get_trading_dates(
                            code.split('.')[-1], start_time='',
                            end_time=today_key, count=5)
                        normalized_dates = sorted(set(
                            _normalize_trade_date(v) for v in trade_dates
                            if _normalize_trade_date(v)))
                        if keep_current_day and today_key in normalized_dates:
                            expected_date = today_key
                        else:
                            prior_dates = [d for d in normalized_dates if d < today_key]
                            expected_date = prior_dates[-1] if prior_dates else ''
                    except Exception as e:
                        expected_date = ''
                        reason = 'trading calendar unavailable: {}'.format(e)

                    actual_date = _normalize_trade_date(adjusted.index[-1])
                    if not reason and not expected_date:
                        reason = 'previous trading date unavailable'
                    elif not reason and actual_date != expected_date:
                        reason = 'last complete {} expected {}'.format(
                            actual_date, expected_date)
                    elif not reason and tick_last_close and tick_last_close > 0:
                        raw_close = float(raw.iloc[-1]['close'])
                        tick_date = _normalize_trade_date(tick_time)
                        if tick_date:
                            # `lastClose` belongs to the trading day before the
                            # full-tick timestamp.  At/after close the latest
                            # daily bar has the same date as the tick, so it is
                            # intentionally *not* the bar to compare here.
                            tick_prior = raw.loc[
                                [str(index) < tick_date for index in raw.index]]
                            if len(tick_prior) == 0:
                                reason = ('no raw close before tick date {} '
                                          'for tick lastClose'.format(tick_date))
                            else:
                                expected_tick_close = float(tick_prior.iloc[-1]['close'])
                                if abs(expected_tick_close - float(tick_last_close)) > 0.02:
                                    adjusted_prior = adjusted.loc[
                                        [str(index) < tick_date for index in adjusted.index]]
                                    adjusted_tick_close = (
                                        float(adjusted_prior.iloc[-1]['close'])
                                        if len(adjusted_prior) else 0.0)
                                    if (adjusted_tick_close > 0 and
                                            abs(adjusted_tick_close -
                                                float(tick_last_close)) <= 0.02):
                                        tick_last_close_adjusted_match = True
                                        _log(
                                            '[DailyData ADJUSTED-MATCH] raw close {:.2f} '
                                            'before tick date {} differs from tick lastClose '
                                            '{:.2f}, but front-adjusted close {:.2f} matches; '
                                            'accepting corporate-action reference price'.format(
                                                expected_tick_close, tick_date,
                                                float(tick_last_close),
                                                adjusted_tick_close))
                                    else:
                                        reason = ('raw close {:.2f} before tick date {} '
                                                  '!= tick lastClose {:.2f}; adjusted close '
                                                  '{:.2f} also differs'.format(
                                                      expected_tick_close, tick_date,
                                                      float(tick_last_close),
                                                      adjusted_tick_close))
                        elif abs(raw_close - float(tick_last_close)) > 0.02:
                            previous_raw_close = (
                                float(raw.iloc[-2]['close'])
                                if len(raw) > 1 else 0.0)
                            if abs(previous_raw_close - float(tick_last_close)) <= 0.02:
                                tick_last_close_one_day_stale = True
                                _log(
                                    '[DailyData WARN] tick lastClose {:.2f} is one '
                                    'trading day stale; using verified raw close {:.2f}'.format(
                                        float(tick_last_close), raw_close))
                            else:
                                adjusted_close = float(adjusted.iloc[-1]['close'])
                                if abs(adjusted_close - float(tick_last_close)) <= 0.02:
                                    tick_last_close_adjusted_match = True
                                    _log(
                                        '[DailyData ADJUSTED-MATCH] raw close {:.2f} '
                                        'differs from tick lastClose {:.2f}, but '
                                        'front-adjusted close {:.2f} matches; accepting '
                                        'corporate-action reference price'.format(
                                            raw_close, float(tick_last_close),
                                            adjusted_close))
                                else:
                                    reason = ('raw close {:.2f} != tick lastClose {:.2f}; '
                                              'adjusted close {:.2f} also differs'.format(
                                                  raw_close, float(tick_last_close),
                                                  adjusted_close))

            if not reason:
                actual_date = _normalize_trade_date(adjusted.index[-1])
                self._daily_data_cache = adjusted
                self._daily_raw_cache = raw
                self._daily_snapshot_meta = {
                    'last_complete_date': actual_date,
                    'count': len(adjusted),
                }
                _log('[DATA-HEALTH] last_complete={} expected={} raw_close={:.2f} '
                     'tick_lastClose={:.2f} count={} aligned=True status=OK'.format(
                         actual_date, expected_date, float(raw.iloc[-1]['close']),
                         float(tick_last_close or 0.0), len(adjusted)))
                return {
                    'adjusted': adjusted,
                    'raw': raw,
                    'last_complete_date': actual_date,
                    'verified_last_close': float(raw.iloc[-1]['close']),
                    'tick_last_close_one_day_stale': tick_last_close_one_day_stale,
                    'tick_last_close_adjusted_match': tick_last_close_adjusted_match,
                }

            _log('[DATA-STALE] attempt {}/{}: {}'.format(attempt, attempts, reason))
            if attempt < attempts and retry_delay > 0:
                _time.sleep(retry_delay)

        self._daily_data_cache = None
        self._daily_raw_cache = None
        self._daily_snapshot_meta = None
        return None

    def get_history_data(self, length, period, field):
        """对应 QMT: ContextInfo.get_history_data(N, '1d', field)"""
        code = cfg.STOCK_QMT
        if self._daily_data_cache is None:
            end = datetime.now().strftime('%Y%m%d')
            start = (datetime.now() - timedelta(days=365 * 6)).strftime('%Y%m%d')
            # ★ 先下载最新日线，确保本地数据包含最近交易日
            try:
                self.xtdata.download_history_data(
                    code, period='1d', start_time='', end_time=''
                )
            except Exception as e:
                _log('[DailyData] download failed: {}'.format(e))
            data = self.xtdata.get_local_data(
                field_list=['open', 'high', 'low', 'close', 'volume', 'amount'],
                stock_list=[code],
                period='1d',
                start_time=start,
                end_time=end,
                dividend_type='front',
            )
            if code in data and len(data[code]) > 0:
                self._daily_data_cache, removed_today = exclude_incomplete_daily_bar(data[code])
                if removed_today:
                    _log('[DailyData] excluded incomplete current-day bar {}'.format(end))
                volumes = self._daily_data_cache['volume'].values.tolist() \
                    if 'volume' in self._daily_data_cache.columns else []
                positive_recent = sum(1 for value in volumes[-21:] if value and value > 0)
                if len(volumes) < 21 or positive_recent < 21:
                    _log('[DailyData WARN] invalid volume history: count={} positive_recent={}/21'.format(
                        len(volumes), positive_recent))
            else:
                _log('[DailyData WARN] no local daily data for {}'.format(code))
                return {}

        df = self._daily_data_cache
        field_map = {
            'close': 'close', 'open': 'open', 'high': 'high',
            'low': 'low', 'volume': 'volume', 'amount': 'amount',
        }
        col = field_map.get(field, field)
        if col in df.columns:
            vals = df[col].values.tolist()
            if len(vals) > length:
                vals = vals[-length:]
            return {code: vals}
        return {}

    def get_full_tick(self, codes):
        """对应 QMT: ContextInfo.get_full_tick([code])"""
        try:
            tick = self.xtdata.get_full_tick(codes)
            return tick if tick else {}
        except Exception:
            return {}

    # ── 交易查询 ──

    def query_positions(self):
        if not self._trade_connected:
            return []
        try:
            positions = self.trader.query_stock_positions(self._account_obj)
            return positions or []
        except Exception:
            return []

    def query_account(self):
        if not self._trade_connected:
            return None
        try:
            return self.trader.query_stock_asset(self._account_obj)
        except Exception:
            return None

    # ── 下单 ──

    def _recover_order_id(self, remark, stock_code, order_type, shares):
        """Resolve an accepted asynchronous order by its unique correlation remark."""
        for delay in (0.0, 0.2, 0.5):
            if delay:
                _time.sleep(delay)
            try:
                orders = self.trader.query_stock_orders(
                    self._account_obj, cancelable_only=False) or []
            except Exception:
                continue
            for order in orders:
                if (_pick(order, 'order_remark', 'm_strOrderRemark', default='') == remark and
                        _pick(order, 'stock_code', 'm_strInstrumentID', default='') == stock_code and
                        int(_pick(order, 'order_type', 'm_nOrderType', default=0)) == order_type and
                        int(_pick(order, 'order_volume', 'm_nOrderVolume', default=0)) == shares):
                    order_id = _pick(order, 'order_id', 'm_nOrderID',
                                     'order_sysid', 'm_strOrderSysID', default=None)
                    if order_id is not None and str(order_id) not in ('', '0', '-1'):
                        _log('[ORDER-ID-RECOVERED] remark={} order={}'.format(
                            remark, order_id))
                        return order_id
        return None

    def order_stock(self, stock_code, shares, style, price=None):
        """对应 QMT: order_shares(...)"""
        from xtquant import xtconstant

        if not self._trade_connected:
            _log('[下单跳过-未连接交易]')
            return

        if shares > 0:
            order_type = xtconstant.STOCK_BUY
            dir_name = '买入'
        else:
            order_type = xtconstant.STOCK_SELL
            shares = abs(shares)
            dir_name = '卖出'

        # 将策略层的报价风格显式映射到 XtQuant 报价类型。
        # 非指定价委托的 price 必须传 0，由 QMT 按对应规则取价。
        style_name = str(style or 'FIX').upper()
        if style_name == 'COMPETE':
            price_type = xtconstant.MARKET_PEER_PRICE_FIRST
            order_price = 0
        elif style_name == 'LATEST':
            price_type = xtconstant.LATEST_PRICE
            order_price = 0
        elif style_name == 'FIX':
            price_type = xtconstant.FIX_PRICE
            order_price = price if price and price > 0 else 0
            if order_price <= 0:
                raise ValueError('FIX style requires a positive price')
        else:
            raise ValueError('unsupported order style: {}'.format(style))

        # ★ 记录下单快照, 供委托/成交推送回调在价格/数量字段为 0 时回退显示真实值
        self.last_order_info = (dir_name, order_price, shares, stock_code)

        display_price = 'PEER' if style_name == 'COMPETE' else (
            'LATEST' if style_name == 'LATEST' else 'Y{:.2f}'.format(order_price))
        _log('  >>> 下单{}: {} x {}股 {} [{}]'.format(
            dir_name, display_price, shares, stock_code, style_name))

        try:
            sequence = getattr(self, '_order_sequence', 0) + 1
            self._order_sequence = sequence
            correlation_remark = 'DayT{}{:x}{:x}'.format(
                'B' if order_type == xtconstant.STOCK_BUY else 'S',
                _time.time_ns(), sequence)
            ret = self.trader.order_stock(
                self._account_obj, stock_code, order_type, shares,
                price_type, order_price,
                cfg.STRATEGY_NAME, correlation_remark,
            )
            if ret is None or str(ret) in ('', '0', '-1'):
                ret = self._recover_order_id(
                    correlation_remark, stock_code, order_type, shares)
            self.order_pending = True
            return ret
        except Exception as e:
            _log('[下单异常] {}'.format(e))
            self.order_pending = False
            return None

    def cancel_order(self, order_id):
        """撤单 (按 order_id)。超时未成交时调用, 避免委托挂单残留。"""
        if not self._trade_connected or order_id is None:
            return False
        try:
            self.trader.cancel_order_stock(self._account_obj, order_id)
            _log(f'[撤单] order_id={order_id}')
            return True
        except Exception as e:
            _log(f'[撤单异常] {e}')
            return False


# ============================================================================
# QMT ContextInfo 模拟
# ============================================================================

class MockContextInfo:
    """模拟 QMT 的 ContextInfo 对象, 提供策略需要的接口"""

    def __init__(self, connector: MiniQMTConnector):
        self.conn = connector
        self.st = {}
        self.accID = cfg.ACCOUNT
        self._barpos = 0

    @property
    def barpos(self):
        return self._barpos

    @barpos.setter
    def barpos(self, value):
        self._barpos = value

    def set_universe(self, stock_list):
        pass

    def set_account(self, acc_id):
        self.accID = acc_id

    def get_history_data(self, length, period, field, dividend_type=0, skip_paused=True):
        if period != '1d':
            _log(f'[警告] 仅支持1d周期, 收到: {period}')
            return {}
        return self.conn.get_history_data(length, period, field)

    def get_full_tick(self, codes):
        return self.conn.get_full_tick(codes)

    def is_last_bar(self):
        return True

    def run_time(self, func_name, interval, start_time='', market=''):
        pass

    def get_stock_name(self, code):
        return cfg.STOCK_NAME

    def get_open_date(self, code):
        return '20140701'


# ============================================================================
# QMT 数据结构模拟
# ============================================================================

class MockPosition:
    """模拟 QMT 的持仓对象"""
    def __init__(self, xt_pos=None):
        if xt_pos is not None:
            self.m_strInstrumentID = xt_pos.stock_code.split('.')[0] if hasattr(xt_pos, 'stock_code') else ''
            self.m_nVolume = getattr(xt_pos, 'volume', 0)
            self.m_dOpenPrice = getattr(xt_pos, 'open_price', 0.0)
            self.m_nCanUseVolume = getattr(xt_pos, 'can_use_volume', getattr(xt_pos, 'volume', 0))
        else:
            self.m_strInstrumentID = ''
            self.m_nVolume = 0
            self.m_dOpenPrice = 0.0
            self.m_nCanUseVolume = 0


class MockAccount:
    """模拟 QMT 的账户对象"""
    def __init__(self, asset=None):
        if asset is not None:
            self.m_dAvailable = getattr(asset, 'cash', 0.0)
            self.m_dBalance = getattr(asset, 'total_asset', 0.0)
        else:
            self.m_dAvailable = 0.0
            self.m_dBalance = 0.0


# ============================================================================
# QMT 全局函数模拟
# ============================================================================

def get_trade_detail_data(account_id, account_type, data_type):
    """模拟 QMT: get_trade_detail_data(account, 'STOCK', 'POSITION'/'ACCOUNT')"""
    conn = _global_conn
    if conn is None:
        return []

    if data_type.upper() == 'POSITION':
        xt_positions = conn.query_positions()
        return [MockPosition(xp) for xp in xt_positions]

    elif data_type.upper() == 'ACCOUNT':
        asset = conn.query_account()
        if asset is not None:
            return [MockAccount(asset)]
        return [MockAccount()]

    return []


def order_shares(stockcode, shares, style='LATEST', price=None,
                 ContextInfo=None, accId=None):
    """
    模拟 QMT: order_shares(code, shares, style, price, ContextInfo, accId)
    兼容 QMT 的多种参数重排调用方式。
    """
    conn = _global_conn
    if conn is None:
        _log('[order_shares] 未连接, 跳过')
        return

    _price = price
    if price is not None and not isinstance(price, (int, float)):
        # price 参数实际传的是 ContextInfo
        _price = None

    if _global_dry_run:
        direction = '买入' if shares > 0 else '卖出'
        px = _price if _price else '(对手价)'
        _log(f'[模拟] {direction} {stockcode} {px} x {abs(shares)}股')
        return 0

    return conn.order_stock(stockcode, shares, style, _price)
