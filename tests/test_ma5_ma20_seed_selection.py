import ast
from pathlib import Path
import math
import json
from types import SimpleNamespace

import pandas as pd

from Stragety.MiniQMT_Stragety.core import ma5_ma20_seed_selection as shared


def original_rules():
    path = Path(__file__).resolve().parents[1] / (
        'Stragety/MiniQMT_Stragety/MA5MA20/'
        'HS_MainBoard_MA5_MA20_Screener_v11_NoST_ATR_bigqmt_redis.py')
    tree = ast.parse(path.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_positive', 'calculate_ma_candidate', 'is_main_board', 'allowed_name')]
    namespace = dict(math=math, SHORT_MA_DAYS=5, LONG_MA_DAYS=20,
                     ATR_DAYS=14, MA5_BELOW_PERCENT=3.0, MIN_ATR_PERCENT=7.0)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def test_shared_rules_match_existing_v11_exactly():
    old = original_rules()
    for spread in (0.1, 5.0, 10.0):
        rows = [dict(high=price + spread, low=price - spread, close=price)
                for price in ([90.0] * 15 + [120.0] * 4)]
        for price in (0.0, 80.0, 100.0, 110.0, 120.0, 150.0):
            assert shared.calculate_ma_candidate(rows, price) == old['calculate_ma_candidate'](rows, price)
    for code in ('600000.SH', '605001.SH', '000001.SZ', '003000.SZ',
                 '688001.SH', '300001.SZ', '900001.SH', '1234', '600000.SZ'):
        assert shared.is_main_board(code) == old['is_main_board'](code)
    for name in ('正常名称', 'ST股票', '*ST股票', '', None, '-'):
        assert shared.allowed_name(name) == old['allowed_name'](name)


class FakeData:
    client = SimpleNamespace(_transport_instance=None)

    def __init__(self):
        self.days = pd.bdate_range(end='2026-11-16', periods=70)
        completed = self.days[:-1][-60:]
        closes = [90.0] * 56 + [120.0] * 4
        self.frame = pd.DataFrame({'high': [value + 6 for value in closes],
                                   'low': [value - 6 for value in closes],
                                   'close': closes}, index=completed)
        self.names = {'600000.SH': '普通股票', '600001.SH': '*ST股票',
                      '600002.SH': '历史不足', '600003.SH': '未知日期'}

    def get_stock_list_in_sector(self, sector):
        assert sector == '沪深A股'
        return list(self.names) + ['688001.SH', '300001.SZ']

    def get_trading_dates(self, *args, **kwargs):
        return list(self.days[-30:])

    def get_market_data_ex(self, **kwargs):
        assert kwargs['heal'] is False
        assert kwargs['subscribe'] is False
        assert kwargs['fill_data'] is False
        return {'600000.SH': self.frame, '600001.SH': self.frame,
                '600002.SH': self.frame.iloc[-3:],
                '600003.SH': self.frame.iloc[:-1]}

    def get_full_tick(self, codes):
        return {code: {'lastPrice': 110.0} for code in codes}

    def get_instrument_detail(self, code):
        return {'InstrumentName': self.names[code]}


def test_auto_universe_excludes_st_nonmainboard_and_unverified_history():
    assert shared.screen_main_board(FakeData(), '20261116', lambda text: None) == ['600000.SH']


def test_source_calendar_not_ready_cannot_select():
    data = FakeData()
    data.get_trading_dates = lambda *a, **k: list(data.days[-30:-1])
    import pytest
    with pytest.raises(RuntimeError, match='current trading day'):
        shared.screen_main_board(data, '20261116', lambda text: None)


def test_scan_distinguishes_data_coverage_from_technical_rejections():
    data = FakeData()
    data.names.update({'600004.SH': '空数据', '600005.SH': '无报价', '600006.SH': '坏K线'})
    original = data.get_market_data_ex

    def frames(**kwargs):
        result = original(**kwargs)
        result['600004.SH'] = data.frame.iloc[:0]
        result['600005.SH'] = data.frame
        bad = data.frame.copy()
        bad.iloc[-1, bad.columns.get_loc('low')] = 9999
        result['600006.SH'] = bad
        return result

    data.get_market_data_ex = frames
    data.get_full_tick = lambda codes: {
        code: {'lastPrice': 0 if code == '600005.SH' else 110.0} for code in codes}
    messages = []
    assert shared.screen_main_board(data, '20261116', messages.append) == ['600000.SH']
    audit = json.loads(next(line.split('] ', 1)[1] for line in messages if line.startswith('[SEED-COVERAGE]')))
    assert audit['coverage'] == 'PARTIAL'
    assert audit['counts'] == {'empty_history': 1, 'stale_history': 1,
                               'gapped_or_short_history': 1, 'invalid_price': 1,
                               'invalid_ohlc': 1, 'valid': 2}
    assert sum(audit['counts'].values()) == audit['universe'] == 7
    assert audit['technical_miss'] == 0
    assert audit['name_excluded'] == 1
    assert audit['expected_last_complete'] == '20261113'
    assert audit['examples']['stale_history'][0]['code'] == '600003.SH'


def test_completed_rows_exclude_current_bar():
    data = FakeData()
    frame = data.frame.copy()
    frame.loc[pd.Timestamp('2026-11-16')] = [9999, 1, 9999]
    assert shared._completed_rows(frame, '20261116') == shared._completed_rows(data.frame, '20261116')


def test_calendar_timestamps_and_plain_dates_normalize_consistently():
    day = pd.Timestamp('2026-11-16 09:30:00')
    assert shared._date_key(int(day.timestamp() * 1000)) == '20261116'
    assert shared._date_key(int(day.timestamp())) == '20261116'
    assert shared._date_key(20261116) == '20261116'


def test_background_scan_does_not_block_scheduler_and_stale_results_expire(monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def screen(*args):
        entered.set()
        assert release.wait(3)
        return ['600000.SH']

    monkeypatch.setattr(shared, 'screen_main_board', screen)
    def data_factory():
        data = FakeData()
        data.client = SimpleNamespace(_transport_instance=SimpleNamespace(stop=cleaned.set))
        return data

    worker = shared.SeedSelectionWorker(data_factory, lambda text: None)
    try:
        worker.request('20261116')
        assert entered.wait(1)
        # request() already returned despite the scanner being blocked.
        assert worker.snapshot('20261116') is None
        release.set()
        # close() joins the worker; its completed snapshot remains inspectable.
        worker.close()
        snapshot = worker.snapshot('20261116')
        assert snapshot['codes'] == ('600000.SH',)
        assert cleaned.is_set()
        assert worker.snapshot('20261117') is None
        monkeypatch.setattr(shared.time, 'monotonic', lambda: snapshot['completed_at'] + 901)
        assert worker.snapshot('20261116') is None
    finally:
        release.set()
        worker.close()
