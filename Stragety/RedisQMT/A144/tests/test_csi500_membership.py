# -*- coding: utf-8 -*-
"""Offline official-file fixtures; never connects to QMT or a live broker."""
import io
import json
from pathlib import Path

import pandas as pd
import pytest

from Stragety.RedisQMT.A144.core.csi500_membership import (
    CURRENT_URL, DETAIL_URL, NOTICE_URL, ensure, fetch, parse_anchor, prepare, reconstruct,
)
from Stragety.RedisQMT.A144.core.dual_regime_execution import atomic_save
from Stragety.RedisQMT.A144.core.dual_regime_runtime import load_universe


DATES = ['20261009', '20261012', '20261013', '20261014', '20261015', '20261016', '20261019']
BEFORE = {f'{600000 + number:06d}.SH' for number in range(500)}
AFTER = BEFORE - {'600000.SH'} | {'600500.SH'}
CHANGE = dict(effective='20261012', incoming=['600500.SH'], outgoing=['600000.SH'])


def anchor_bytes(codes=AFTER, date='20261016'):
    rows = [[date, '000905', 'CSI500', 'CSI500', code[:6], 'test', 'test', 'SH', 'Shanghai Stock Exchange']
            for code in sorted(codes)]
    output = io.BytesIO()
    pd.DataFrame(rows, columns=list('abcdefghi')).to_excel(output, index=False)
    return output.getvalue()


def workbook():
    output = io.BytesIO()
    with pd.ExcelWriter(output) as writer:
        pd.DataFrame([['000905', 'CSI500', '600500']], columns=['index', 'name', 'code']).to_excel(writer, sheet_name='调入', index=False)
        pd.DataFrame([['000905', 'CSI500', '600000']], columns=['index', 'name', 'code']).to_excel(writer, sheet_name='调出', index=False)
    return output.getvalue()


def test_reverse_adjustment_preserves_actual_historical_membership():
    result = reconstruct('20261016', AFTER, [CHANGE], DATES, '20261019')
    assert set(result['20261009']) == BEFORE
    assert set(result['20261012']) == AFTER
    assert len(result['20261009']) == len(result['20261012']) == 500


def test_forward_change_after_anchor_is_applied_for_current_day():
    change = dict(CHANGE, effective='20261019')
    result = reconstruct('20261016', BEFORE, [change], DATES, '20261019')
    assert set(result['20261009']) == BEFORE
    assert set(result['20261019']) == AFTER


def test_two_adjustments_same_effective_day_keep_final_after_snapshot():
    another = dict(effective='20261012', incoming=['600501.SH'], outgoing=['600001.SH'])
    after_both = AFTER - {'600001.SH'} | {'600501.SH'}
    result = reconstruct('20261016', after_both, [CHANGE, another], DATES, '20261019')
    assert set(result['20261009']) == BEFORE
    assert set(result['20261012']) == after_both


def test_stale_anchor_and_conflicting_adjustment_are_rejected():
    with pytest.raises(ValueError, match='stale'):
        reconstruct('20261013', AFTER, [], DATES, '20261019')
    with pytest.raises(ValueError, match='conflicts'):
        reconstruct('20261016', BEFORE, [CHANGE], DATES, '20261019')


def test_official_anchor_rejects_duplicate_constituents():
    assert parse_anchor(anchor_bytes())[0] == '20261016'
    with pytest.raises(ValueError, match='500'):
        parse_anchor(anchor_bytes(AFTER - {'600500.SH'}))


def test_prepare_writes_file_loadable_by_real_portfolio(tmp_path):
    attachment = 'https://oss-ch.csindex.com.cn/notice/test.xlsx'
    calls = []
    def downloader(url, folder, payload=None):
        calls.append(url)
        if url == CURRENT_URL:
            value = anchor_bytes()
        elif url == NOTICE_URL:
            assert payload['page']['page'] == 1
            value = json.dumps(dict(code='200', total=1, data=[dict(id=1, title='关于中证500指数样本调整的公告')])).encode()
        elif url.startswith(DETAIL_URL):
            value = json.dumps(dict(code='200', data=dict(id=1,
                content='<p>中证500指数样本于2026年10月9日收市后生效。</p>',
                enclosureList=[dict(fileUrl=attachment)]))).encode()
        elif url == attachment:
            value = workbook()
        else:
            pytest.fail('Unexpected source ' + url)
        return value, dict(url=url, sha256='test-fixture')
    path = tmp_path / 'csi500_membership.json'
    result = prepare(path, DATES, '20261019', downloader)
    assert result['valid_through'] == '20261019'
    loaded = load_universe(path, DATES[:-1], '20261019')
    assert loaded[0]['20261009'] == BEFORE and loaded[1] == AFTER
    assert len(calls) == 4


def test_failed_refresh_does_not_replace_previous_file(tmp_path):
    path = tmp_path / 'csi500_membership.json'
    atomic_save(path, dict(source='previous', valid_through='20261016', snapshots={'20261012': sorted(AFTER)}))
    previous = path.read_bytes()
    def failed(url, folder, payload=None):
        raise OSError('network unavailable')
    with pytest.raises(OSError):
        prepare(path, DATES, '20261019', failed)
    assert path.read_bytes() == previous


def test_current_cache_requires_no_network_or_calendar_query(tmp_path):
    path = tmp_path / 'csi500_membership.json'
    atomic_save(path, dict(source='test', valid_through='20261019', snapshots={'20261009': sorted(AFTER)}))
    class NoNetwork:
        def get_trading_dates(self, *args, **kwargs):
            pytest.fail('Unexpected network/calendar query')
    ensure(path, NoNetwork(), '20261019', auto_refresh=True)


def test_untrusted_source_host_rejected_before_network(tmp_path):
    with pytest.raises(ValueError, match='Unexpected'):
        fetch('https://example.org/changed.xlsx', tmp_path)
