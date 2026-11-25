"""Reject non-trading placeholder history even when prices look valid."""
import numpy as np
import pandas as pd


def audit_sessions(frame):
    issues=[]
    valid=[]
    for date,day in frame.groupby(frame.index.astype(str).str[:8]):
        prices=day[['open','high','low','close']]
        finite=np.isfinite(prices.to_numpy()).all()
        if not finite or (prices<=0).any().any():
            reason='INVALID_PRICE'
        elif (day.volume<=0).all() or (day.amount<=0).all():
            reason='ALL_ZERO_ACTIVITY_PLACEHOLDER'
        elif len(day)<240:
            reason='INCOMPLETE_SESSION'
        elif ((day.high<day[['open','close','low']].max(axis=1)) |
              (day.low>day[['open','close','high']].min(axis=1))).any():
            reason='INCONSISTENT_OHLC'
        else:
            reason=''
        if not reason:
            required=set(pd.date_range(date+' 09:31',date+' 11:30',freq='min').strftime('%Y%m%d%H%M%S'))
            required.update(pd.date_range(date+' 13:01',date+' 15:00',freq='min').strftime('%Y%m%d%H%M%S'))
            actual=set(day.index.astype(str))
            if not day.index.is_unique:
                reason='DUPLICATE_MINUTE'
            elif not required.issubset(actual):
                reason='MISSING_SCHEDULED_MINUTE'
            elif not actual.issubset(required|{date+'093000'}):
                reason='OUTSIDE_SESSION_MINUTE'
        if reason:
            issues.append({'date':date,'rows':len(day),'positive_volume':int((day.volume>0).sum()),
                           'positive_amount':int((day.amount>0).sum()),'reason':reason})
        else:
            valid.append(date)
    return {'valid_dates':valid,'rejected':issues,'valid_sessions':len(valid),'rejected_sessions':len(issues)}


def valid_history(frame,min_history=60):
    audit=audit_sessions(frame)
    clean=frame.loc[frame.index.astype(str).str[:8].isin(audit['valid_dates'])].copy()
    dates=sorted(set(clean.index.astype(str).str[:8]))
    eligible=set(dates[min_history:])
    return clean,eligible,audit


def require_window(frame,start,end,min_sessions=40,max_boundary_lag_days=10):
    if frame.empty:
        raise ValueError('EMPTY_REAL_DATA_WINDOW')
    dates=sorted(set(frame.index.astype(str).str[:8]))
    first=pd.Timestamp(dates[0])
    last=pd.Timestamp(dates[-1])
    if ((first-pd.Timestamp(start)).days>max_boundary_lag_days or
            (pd.Timestamp(end)-last).days>max_boundary_lag_days):
        raise ValueError('REQUESTED_WINDOW_NOT_COVERED: actual='+dates[0]+'..'+dates[-1])
    if len(dates)<min_sessions:
        raise ValueError('INSUFFICIENT_REAL_SESSIONS: '+str(len(dates)))
    return {'first':dates[0],'last':dates[-1],'sessions':len(dates)}
