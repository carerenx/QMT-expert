# -*- coding: utf-8 -*-
"""Structural minute/day reconciliation, not source or universe certification.

Check: python -m unittest discover -s Stragety/RedisQMT/Trend100/tests -v
240 local minute-start labels, finite OHLCV/amount, share/yuan units, raw daily match.
No downloads, fills or live command. See ../ResearchDesign.md.
"""
from math import isclose, isfinite

from .execution import MinuteBar
from .phases import has_closing_auction


def audit_minute_session(frame, raw_daily, expected):
    """Return all detected issues; raw_daily must be the unadjusted daily row.

    The caller supplies the certified trading calendar and raw data provenance.
    Passing this check does not prove that synthetic or altered inputs are genuine.
    """
    issues = []
    try:
        closing_auction = has_closing_auction(raw_daily.get('exchange'),
            raw_daily.get('asset_kind'), expected[0].date())
    except ValueError as error:
        return {'passed': False, 'issues': ['unverified_phase_metadata:' + str(error)], 'rows': len(frame)}
    required = list(MinuteBar.__dataclass_fields__)
    missing = [column for column in required if column not in frame]
    if missing:
        return {'passed': False, 'issues': ['missing_fields:' + ','.join(missing)], 'rows': len(frame)}
    if not frame.index.equals(expected):
        issues.append('minute_clock_mismatch')
    for at, row in frame[required].iterrows():
        try:
            MinuteBar(**row.to_dict()).validate()
        except (ValueError, TypeError):
            issues.append('invalid_bar:' + str(at))
            continue
        if row.volume != int(row.volume):
            issues.append('noninteger_share_volume:' + str(at))
        if not isinstance(row.tradable, bool):
            issues.append('tradable_must_be_boolean:' + str(at))
        if row.volume == 0:
            if row.amount != 0:
                issues.append('amount_without_volume:' + str(at))
        elif not row.low * row.volume - 1 <= row.amount <= row.high * row.volume + 1:
            issues.append('amount_share_price_inconsistent:' + str(at))
        if (closing_auction and at.time().hour == 14 and at.time().minute >= 57
                and isinstance(row.tradable, bool) and row.tradable):
            issues.append('auction_matching_unsupported:' + str(at))
    if frame.empty:
        issues.append('empty_session')
    else:
        if frame.volume.sum() == 0:
            if not (raw_daily.get('suspended') is True and raw_daily.get('suspension_source')):
                issues.append('zero_volume_session_without_suspension_evidence')
            if frame.tradable.any():
                issues.append('zero_volume_session_marked_tradable')
        aggregates = {'open': frame.open.iloc[0], 'high': frame.high.max(),
                      'low': frame.low.min(), 'close': frame.close.iloc[-1],
                      'volume': frame.volume.sum(), 'amount': frame.amount.sum()}
        for column, actual in aggregates.items():
            expected_value = raw_daily.get(column)
            tolerance = 1.0 if column == 'amount' else 1e-7
            if (expected_value is None or not isfinite(expected_value)
                    or not isfinite(actual)
                    or not isclose(actual, expected_value, rel_tol=1e-10, abs_tol=tolerance)):
                issues.append('daily_mismatch:' + column)
    return {'passed': not issues, 'issues': issues, 'rows': len(frame)}
