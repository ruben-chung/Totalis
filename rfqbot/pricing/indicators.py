"""LegSpec -> payoff over scenarios.

``indicator(spec, scen)`` returns a float32 array in [0, 1] per scenario (1 = the leg pays)
or ``None`` when the family / period is not modeled in v1. Values are 0/1 except for
moneylines whose rules say ties pay 50-50 (``rules["tie"] == "50-50"``), where a tied period
contributes 0.5; a ``tie`` side pays 1 on a tie; ``not_X`` is 1 - X.

Periods: FG uses (H, A); 1H uses (H1, A1); 2H uses (H - H1, A - A1) which includes overtime,
consistent with both venues' 2H rules. Quarters are not modeled (None).

Spread orientation: ``team`` with ``line`` L < 0, ``side == team`` pays when
pts_team - pts_other > -L; the other side pays when pts_team - pts_other < -L (lines are
half points, so equality cannot happen).
"""
from __future__ import annotations

import numpy as np

from ..sports.legspec import LegSpec
from .joint_nfl import Scenarios

MODELED = frozenset({"moneyline", "spread", "total", "team_total", "btts", "exact_margin"})
PERIODS = ("FG", "1H", "2H")


def period_scores(scen: Scenarios, period: str) -> tuple[np.ndarray, np.ndarray] | None:
    if period == "FG":
        return scen.H.astype(np.int32), scen.A.astype(np.int32)
    if period == "1H":
        return scen.H1.astype(np.int32), scen.A1.astype(np.int32)
    if period == "2H":
        return (scen.H - scen.H1).astype(np.int32), (scen.A - scen.A1).astype(np.int32)
    return None


def indicator(spec: LegSpec, scen: Scenarios) -> np.ndarray | None:
    if spec.family not in MODELED or spec.period not in PERIODS:
        return None
    ps = period_scores(scen, spec.period)
    if ps is None:
        return None
    h, a = ps

    def pts(team: str) -> np.ndarray:
        return h if team == spec.home else a

    def other(team: str) -> str:
        return spec.away if team == spec.home else spec.home

    fam, side = spec.family, spec.side
    if fam == "moneyline":
        if side == "tie":
            return (h == a).astype(np.float32)
        neg = side.startswith("not_")
        team = side[4:] if neg else side
        if team == "tie":
            return (h != a).astype(np.float32)
        mine, theirs = pts(team), pts(other(team))
        pay = (mine > theirs).astype(np.float32)
        if spec.rules.get("tie") == "50-50":
            pay = pay + 0.5 * (mine == theirs)
        return (1.0 - pay).astype(np.float32) if neg else pay.astype(np.float32)
    if fam == "spread":
        assert spec.team is not None and spec.line is not None
        margin = pts(spec.team) - pts(other(spec.team))
        covers = margin > -spec.line
        return (covers if side == spec.team else ~covers).astype(np.float32)
    if fam == "total":
        assert spec.line is not None
        over = (h + a) > spec.line
        return (over if side == "over" else ~over).astype(np.float32)
    if fam == "team_total":
        assert spec.team is not None and spec.line is not None
        over = pts(spec.team) > spec.line
        return (over if side == "over" else ~over).astype(np.float32)
    if fam == "btts":
        yes = (h > 0) & (a > 0)
        return (yes if side == "yes" else ~yes).astype(np.float32)
    if fam == "exact_margin":
        assert spec.bucket is not None
        if spec.bucket == "tie":
            yes = h == a
        else:
            assert spec.team is not None
            margin = pts(spec.team) - pts(other(spec.team))
            if spec.bucket.endswith("+"):
                lo, hi = int(spec.bucket[:-1]), None
            else:
                lo_s, hi_s = spec.bucket.split("-")
                lo, hi = int(lo_s), int(hi_s)
            yes = margin >= lo if hi is None else (margin >= lo) & (margin <= hi)
        return (yes if side == "yes" else ~yes).astype(np.float32)
    return None  # pragma: no cover
