# -*- coding: utf-8 -*-
"""Historical leadership persistence; every feature ends at the signal close."""


def leader_lifecycle(prepared):
    result = prepared.sort_values(["code", "date"]).copy()
    leader = result.mom60_rank >= .95
    result["leader_day"] = leader.astype(float)
    group = result.groupby("code", sort=False)
    streak_group = (~leader).groupby(result.code).cumsum()
    result["leader_streak"] = result.leader_day.groupby([result.code, streak_group]).cumsum()
    result["leader_persistence"] = group.leader_day.transform(lambda x: x.rolling(20).mean())
    result["rank_change5"] = result.mom60_rank - group.mom60_rank.shift(5)
    result["persistence_rank"] = result.groupby("date").leader_persistence.rank(pct=True)
    result["rank_change_rank"] = result.groupby("date").rank_change5.rank(pct=True)
    return result.drop(columns="leader_day")


def select_lifecycle(prepared, mode):
    result = prepared.copy()
    if mode == "fresh_leader":
        result["entry"] &= result.leader_streak.between(1, 5)
    elif mode == "mature_leader":
        result["entry"] &= result.leader_streak >= 10
    elif mode == "persistent_rank":
        result["score"] = .5 * result.score + .5 * result.persistence_rank.fillna(result.score)
    elif mode == "emerging_rank":
        result["score"] = .5 * result.score + .5 * result.rank_change_rank.fillna(result.score)
    elif mode != "baseline":
        raise ValueError(mode)
    return result
