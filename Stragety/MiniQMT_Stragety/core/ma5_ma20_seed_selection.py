"""Shared v11 HS main-board MA5/MA20 + ATR selection arithmetic.

Rule source: MA5MA20/HS_MainBoard_MA5_MA20_Screener_v11_NoST_ATR_bigqmt_redis.py.
Only the pure rules are reused; this module never imports a versioned strategy
or chooses Redis. Background scanning uses a caller-supplied read-only API.
"""
import math
import json
from datetime import datetime
import threading
import time

SHORT_MA_DAYS = 5
LONG_MA_DAYS = 20
MA5_BELOW_PERCENT = 3.0
ATR_DAYS = 14
MIN_ATR_PERCENT = 7.0

def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _date_key(value):
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    text = str(value).replace("-", "").replace("/", "")
    # Bridge calendars may return Unix milliseconds instead of date strings.
    try:
        number = float(value)
        if number > 100000000000:
            return datetime.fromtimestamp(number / 1000).strftime('%Y%m%d')
        if 1000000000 <= number <= 9999999999:
            return datetime.fromtimestamp(number).strftime('%Y%m%d')
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else ""


def calculate_ma_candidate(completed_rows, current_price,
                           below_ma5_percent=MA5_BELOW_PERCENT,
                           min_atr_percent=MIN_ATR_PERCENT):
    """Dynamic MA5/MA20 plus completed-bar ATR filter (v13 backtest definition)."""
    price = _positive(current_price)
    if price is None:
        return None
    if len(completed_rows) < max(LONG_MA_DAYS - 1, ATR_DAYS + 1):
        return None
    rows = []
    for row in completed_rows[-max(LONG_MA_DAYS - 1, ATR_DAYS + 1):]:
        high = _positive(row.get('high'))
        low = _positive(row.get('low'))
        close = _positive(row.get('close'))
        if any(value is None for value in (high, low, close)):
            return None
        if not low <= close <= high:
            return None
        rows.append((high, low, close))
    closes = [row[2] for row in rows]
    true_ranges = [
        max(rows[i][0] - rows[i][1],
            abs(rows[i][0] - rows[i-1][2]), abs(rows[i][1] - rows[i-1][2]))
        for i in range(1, len(rows))
    ]
    atr14 = sum(true_ranges[-ATR_DAYS:]) / ATR_DAYS
    atr_percent = atr14 / closes[-1] * 100.0
    ma5 = (sum(closes[-(SHORT_MA_DAYS - 1):]) + price) / SHORT_MA_DAYS
    ma20 = (sum(closes[-(LONG_MA_DAYS - 1):]) + price) / LONG_MA_DAYS
    gap_pct = (price / ma5 - 1.0) * 100.0
    below_ma5 = price < ma5 * (1.0 - below_ma5_percent / 100.0)
    above_ma20 = price > ma20
    return {
        "price": price,
        "ma5": ma5,
        "ma20": ma20,
        "gap_pct": gap_pct,
        "below_ma5": below_ma5,
        "above_ma20": above_ma20,
        "atr14": atr14,
        "atr_percent": atr_percent,
        "atr_pass": atr_percent >= min_atr_percent,
        "matched": below_ma5 and above_ma20 and atr_percent >= min_atr_percent,
    }


def is_main_board(code):
    """Shanghai/Shenzhen main-board A shares only; no B shares, ETFs or Beijing."""
    code = str(code).upper()
    parts = code.partition('.')
    number = parts[0]
    separator = parts[1]
    exchange = parts[2]
    if not separator or len(number) != 6 or not number.isdigit():
        return False
    return ((exchange == "SH" and number.startswith(("600", "601", "603", "605")))
            or (exchange == "SZ" and number.startswith(("000", "001", "002", "003"))))


def allowed_name(name):
    """Missing names cannot establish non-ST status, so exclude them."""
    normalized = str(name or "").strip().upper().replace(" ", "")
    return bool(normalized and normalized != "-" and "ST" not in normalized)


def _completed_rows(frame, today):
    """Keep OHLC aligned, exclude today's bar, reject invalid bars in calculation."""
    if frame is None:
        return []
    rows = {}
    for index, row in frame.iterrows():
        date = _date_key(index)
        if date and date < today:
            rows[date] = {field: row.get(field) for field in ("high", "low", "close")}
    return [rows[date] for date in sorted(rows)]


def screen_main_board(data, today, log, stopped=lambda: False, universe=(), report_callback=None):
    """Reuse v11 ranking; reject stale/gapped completed windows per symbol.

    Never download, heal missing history, subscribe, place orders or fall back
    to another data source. Missing historical rows reduce valid coverage.
    """
    if universe:
        codes = sorted({str(code).upper() for code in universe if is_main_board(code)})
    else:
        codes = sorted({str(code).upper() for code in
                        (data.get_stock_list_in_sector('沪深A股') or []) if is_main_board(code)})
    if not codes:
        raise RuntimeError('HS main-board universe empty; no automatic fallback')
    dates = [_date_key(value) for value in
             data.get_trading_dates('SH', start_time='', end_time=today, count=30)]
    if not dates or dates[-1] != today:
        raise RuntimeError('source calendar has not confirmed the current trading day')
    completed_dates = sorted({date for date in dates if date and date < today})
    if len(completed_dates) < 19:
        raise RuntimeError('insufficient source trading-calendar history')
    expected_dates = completed_dates[-19:]
    selected = []
    valid = 0
    counts = dict(empty_history=0, stale_history=0, gapped_or_short_history=0,
                  invalid_price=0, invalid_ohlc=0, valid=0)
    examples = {}

    def reject(reason, code, dates):
        counts[reason] += 1
        samples = examples.setdefault(reason, [])
        if len(samples) < 3:
            samples.append({'code': code, 'last_complete': dates[-1] if dates else '',
                            'missing_days': len(set(expected_dates) - set(dates))})

    for offset in range(0, len(codes), 50):
        if stopped():
            return []
        batch = codes[offset:offset + 50]
        history = data.get_market_data_ex(
            field_list=['high', 'low', 'close'], stock_list=batch, period='1d',
            end_time=today, count=40, dividend_type='front', fill_data=False,
            chunk_size=0, timeout_seconds=8, use_formula=False,
            heal=False, subscribe=False) or {}
        ticks = data.get_full_tick(batch) or {}
        for code in batch:
            frame = history.get(code)
            if frame is None or frame.empty:
                reject('empty_history', code, [])
                continue
            row_dates = sorted({_date_key(value) for value in frame.index
                                if _date_key(value) and _date_key(value) < today})
            if row_dates[-19:] != expected_dates:
                reason = ('stale_history' if not row_dates or row_dates[-1] < expected_dates[-1]
                          else 'gapped_or_short_history')
                reject(reason, code, row_dates)
                continue
            if _positive((ticks.get(code) or {}).get('lastPrice')) is None:
                reject('invalid_price', code, row_dates)
                continue
            rows = _completed_rows(frame, today)
            result = calculate_ma_candidate(rows, (ticks.get(code) or {}).get('lastPrice'))
            if result is None:
                reject('invalid_ohlc', code, row_dates)
                continue
            valid += 1
            counts['valid'] += 1
            if result['matched']:
                selected.append((result['gap_pct'], code))
        log('[SEED-SCREEN] history {}/{} checked; valid={} technical={}'.format(
            min(offset + len(batch), len(codes)), len(codes), valid, len(selected)))
    selected.sort(key=lambda item: (item[0], item[1]))
    result_codes = []
    for item in selected:
        if stopped():
            return []
        code = item[1]
        detail = data.get_instrument_detail(code) or {}
        name = detail.get('InstrumentName')
        if allowed_name(name) and '退' not in str(name):
            result_codes.append(code)
    log('[SEED-SCREEN-DONE] universe={} valid={} selected={}; MA5-gap ascending'.format(
        len(codes), valid, len(result_codes)))
    coverage_report = {
        'date': today, 'universe': len(codes), 'counts': counts,
        'coverage': 'COMPLETE' if valid == len(codes) else 'PARTIAL',
        'expected_last_complete': expected_dates[-1],
        'technical_miss': valid - len(selected),
        'name_excluded': len(selected) - len(result_codes),
        'examples': examples,
    }
    log('[SEED-COVERAGE] ' + json.dumps(coverage_report, ensure_ascii=False, sort_keys=True))
    if report_callback is not None:
        report_callback(coverage_report)
    return result_codes


class SeedSelectionWorker:
    """Read-only scanning on its own client/thread; caller never waits for it."""
    def __init__(self, data_factory, log, universe=(), refresh_seconds=300, minimum_coverage=0.0):
        self.data_factory = data_factory
        self.log = log
        self.universe = universe
        self.refresh_seconds = refresh_seconds
        if not 0 <= minimum_coverage <= 1:
            raise ValueError('minimum coverage must be in [0, 1]')
        self.minimum_coverage = minimum_coverage
        self._guard = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._day = ''
        self._started = None
        self._result = None
        self._busy = False

    def request(self, today):
        with self._guard:
            now = time.monotonic()
            if self._busy:
                return
            if self._day == today and self._started is not None and now - self._started < self.refresh_seconds:
                return
            self._day = today
            self._started = now
            self._busy = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='dayt-seed-screen', daemon=True)
                self._thread.start()
        self._wake.set()

    def snapshot(self, today, max_age_seconds=900):
        with self._guard:
            result = self._result
            if (not result or result['date'] != today or result.get('error')
                    or time.monotonic() - result['completed_at'] > max_age_seconds):
                return None
            return dict(result)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._guard:
                today = self._day
            data = None
            try:
                data = self.data_factory()
                coverage = {}
                if self.minimum_coverage:
                    codes = screen_main_board(data, today, self.log, self._stop.is_set,
                                              self.universe, coverage.update)
                    ratio = coverage.get('counts', {}).get('valid', 0) / max(1, coverage.get('universe', 0))
                    if ratio < self.minimum_coverage:
                        raise RuntimeError('verified input coverage {:.2%} < {:.2%}; seed admission blocked'.format(
                            ratio, self.minimum_coverage))
                else:
                    codes = screen_main_board(data, today, self.log, self._stop.is_set, self.universe)
                result = {'date': today, 'codes': tuple(codes), 'error': '', 'coverage': coverage}
            except Exception as error:
                self.log('[SEED-SCREEN-ERROR] {}; no candidates from this round'.format(error))
                result = {'date': today, 'codes': (), 'error': str(error)}
            finally:
                if data is not None:
                    transport = getattr(getattr(data, 'client', None), '_transport_instance', None)
                    if transport is not None:
                        try:
                            transport.stop()
                        except Exception:
                            pass
            result['completed_at'] = time.monotonic()
            with self._guard:
                self._result = result
                self._busy = False

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
