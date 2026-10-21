"""Hard caps and marginal risk (stage 3 part 1, docs/risk-exposure-brief.md).

Caps are layers over the exposure of the book plus a candidate position at size s (shares):

* ``outcome``:  |delta_O(book + s * cand)| <= RISK_MAX_OUTCOME_DELTA * capital for every canonical
  outcome of every modeled game the candidate touches and every unmodeled factor it touches
  (deltas are linear in s, closed form);
* ``game``:     min over scenarios of V_g(book + s * cand) >= -RISK_MAX_GAME_WORST_LOSS * capital
  for each game the candidate touches (concave in s, bisection);
* ``es1``:      1% expected shortfall of the portfolio Monte Carlo <= RISK_MAX_PORTFOLIO_ES1 *
  capital (convex in s, bisection on common random numbers);
* ``sport`` / ``day``: collateral at risk per sport / per game date <= the fraction of capital
  (closed form; off unless set).

A cap the book already breaches does not block a candidate that leaves that measure no worse
(reducing trades are always allowed); a candidate that worsens an existing breach gets size 0.
Quoting never prices around a cap: ``allowed_size`` is the size, full stop.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

import numpy as np

from .book import Book, Position
from .exposure import Exposure, ModelPool, canonical_outcomes, es_at

CAP_NAMES = ("outcome", "game", "es1", "sport", "day")
INF = float("inf")


@dataclass(frozen=True)
class Caps:
    capital_usd: float = 50_000.0
    max_outcome_delta: float = 0.05        # fraction of capital, per outcome / factor
    max_game_worst_loss: float = 0.10      # fraction of capital, worst scenario of one game
    max_portfolio_es1: float = 0.15        # fraction of capital, 1% expected shortfall
    max_sport_notional: float | None = None   # fraction of capital, collateral at risk per sport (None = off)
    max_day_notional: float | None = None     # fraction of capital, collateral at risk per game date (None = off)

    def limit(self, cap: str) -> float | None:
        frac = {"outcome": self.max_outcome_delta, "game": self.max_game_worst_loss, "es1": self.max_portfolio_es1,
                "sport": self.max_sport_notional, "day": self.max_day_notional}[cap]
        return None if frac is None else frac * self.capital_usd

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Caps":
        env = os.environ if env is None else env

        def f(name: str, default: float | None) -> float | None:
            v = env.get(name)
            if v is None or v.strip() == "":
                return default
            return float(v)
        return cls(capital_usd=f("RISK_CAPITAL_USD", 50_000.0) or 50_000.0,
                   max_outcome_delta=f("RISK_MAX_OUTCOME_DELTA", 0.05) or 0.05,
                   max_game_worst_loss=f("RISK_MAX_GAME_WORST_LOSS", 0.10) or 0.10,
                   max_portfolio_es1=f("RISK_MAX_PORTFOLIO_ES1", 0.15) or 0.15,
                   max_sport_notional=f("RISK_MAX_SPORT_NOTIONAL", None),
                   max_day_notional=f("RISK_MAX_DAY_NOTIONAL", None))

    def as_dict(self) -> dict[str, Any]:
        return {"capital_usd": self.capital_usd, "max_outcome_delta": self.max_outcome_delta,
                "max_game_worst_loss": self.max_game_worst_loss, "max_portfolio_es1": self.max_portfolio_es1,
                "max_sport_notional": self.max_sport_notional, "max_day_notional": self.max_day_notional}


@dataclass
class CapResult:
    requested: float
    allowed_size: float                       # min(requested, cap_size)
    cap_size: float                           # largest size every cap allows (inf when none binds)
    binding_cap: str | None                   # the cap giving cap_size, when it is below the request
    headroom_by_cap: dict[str, float]         # cap -> largest size it alone allows (inf = unconstrained)
    detail: dict[str, str] = field(default_factory=dict)   # cap -> what binds (outcome label, game, ...)

    @property
    def ok(self) -> bool:
        return self.allowed_size >= self.requested - 1e-9

    def as_dict(self) -> dict[str, Any]:
        return {"requested": self.requested, "allowed_size": self.allowed_size, "cap_size": self.cap_size,
                "binding_cap": self.binding_cap, "headroom_by_cap": dict(self.headroom_by_cap), "detail": dict(self.detail)}


@dataclass
class MarginalRisk:
    size: float
    es1_before: float
    es1_after: float
    d_es1_per_share: float                    # (ES1 with the candidate at its size - ES1 without) / size
    tail_mean_per_share: float                # -E[candidate P&L per share | book in its 1% tail]: dES1/ds at s -> 0
    cov_by_game: dict[str, float]             # cov(candidate V_g per share, book V_g) over scenarios
    corr_by_game: dict[str, float]

    def __float__(self) -> float:
        return self.d_es1_per_share

    def as_dict(self) -> dict[str, Any]:
        return {"size": self.size, "es1_before": self.es1_before, "es1_after": self.es1_after,
                "d_es1_per_share": self.d_es1_per_share, "tail_mean_per_share": self.tail_mean_per_share,
                "cov_by_game": dict(self.cov_by_game), "corr_by_game": dict(self.corr_by_game)}


# ----------------------------------------------------------------------------- solving
def linear_limit(b: float, c: float, limit: float) -> float:
    """Largest s >= 0 with |b + s c| <= limit, allowing a reducing move through an existing breach."""
    if abs(c) < 1e-12:
        return INF
    hi = (limit - b) / c if c > 0 else (-limit - b) / c
    return max(0.0, hi)


def bisect_limit(feasible, start: float, *, iters: int = 60, s_max: float = 1e9) -> float:
    """Largest s in [0, s_max] with feasible(s), for a feasible set that is an interval containing 0."""
    if not feasible(1e-9):
        return 0.0
    hi = max(start, 1.0)
    while feasible(hi):
        hi *= 2
        if hi >= s_max:
            return INF
    lo = 0.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return lo if lo >= 1e-6 else 0.0


def _exposure(book: Book, models: Any, exposure: Exposure | None, n_draws: int, seed: int) -> Exposure:
    return exposure if exposure is not None else Exposure(book, models, n_draws=n_draws, seed=seed)


def check(book: Book, candidate: Position, models: ModelPool | Mapping[str, Any] | None = None, *, caps: Caps | None = None,
          exposure: Exposure | None = None, n_draws: int = 20_000, seed: int = 0) -> CapResult:
    """Largest size of ``candidate`` (same legs, sign and price) that keeps every cap satisfied."""
    caps = caps or Caps.from_env()
    base = _exposure(book, models, exposure, n_draws, seed)
    cand = base.derive([replace(candidate, size=1.0)])          # per-share vectors
    head: dict[str, float] = {}
    detail: dict[str, str] = {}

    # outcome deltas (linear in s)
    L = caps.limit("outcome")
    best, label = INF, ""
    if L is not None:
        for g in cand.game_vec:
            model = base.pool.get(g)
            if model is None:
                continue
            for lbl, spec in canonical_outcomes(model):
                c = cand.delta(g, spec)
                if c is None:
                    continue
                b = base.delta(g, spec) if g in base.game_vec else 0.0
                s = linear_limit(b or 0.0, c, L)
                if s < best:
                    best, label = s, f"{g} {lbl}"
        for k, c in cand.factor_delta.items():
            s = linear_limit(base.factor_delta.get(k, 0.0), c, L)
            if s < best:
                best, label = s, cand.factors[k].label
    head["outcome"], detail["outcome"] = best, label

    # per-game worst scenario (concave in s)
    L = caps.limit("game")
    best, label = INF, ""
    if L is not None:
        for g, vc in cand.game_vec.items():
            vb = base.game_vec.get(g)
            vb = np.zeros_like(vc) if vb is None else vb
            floor = min(-L, float(vb.min()))

            def feasible(s: float, vb=vb, vc=vc, floor=floor) -> bool:
                return float((vb + s * vc).min()) >= floor - 1e-9
            s = bisect_limit(feasible, candidate.size)
            if s < best:
                best, label = s, g
    head["game"], detail["game"] = best, label

    # portfolio ES1 (convex in s)
    L = caps.limit("es1")
    best = INF
    if L is not None and (cand.game_vec or cand.factor_delta):
        pb, pc = base.pnl_draws(), cand.pnl_draws()
        ceiling = max(L, es_at(pb, 0.01))

        def feasible_es(s: float) -> bool:
            return es_at(pb + s * pc, 0.01) <= ceiling + 1e-9
        best = bisect_limit(feasible_es, candidate.size)
    head["es1"], detail["es1"] = best, "portfolio"

    # notional by sport / day (linear)
    per_share = candidate.max_loss_per_share
    for cap, what, keys in (("sport", "sport", [candidate.sport or "?"]), ("day", "date", candidate.game_dates or ["?"])):
        L = caps.limit(cap)
        best, label = INF, ""
        if L is not None and per_share > 0:
            used = book.notional_by(what)
            for k in keys:
                s = max(0.0, (L - used.get(k, 0.0)) / per_share)
                if s < best:
                    best, label = s, k
        head[cap], detail[cap] = best, label

    cap_size = min(head.values())
    binding = None
    if cap_size < candidate.size - 1e-9:
        binding = min(head, key=head.get)   # type: ignore[arg-type]
    return CapResult(candidate.size, min(candidate.size, cap_size), cap_size, binding, head,
                     {k: v for k, v in detail.items() if v})


def marginal_risk(book: Book, candidate: Position, models: ModelPool | Mapping[str, Any] | None = None, *,
                  exposure: Exposure | None = None, n_draws: int = 20_000, seed: int = 0) -> MarginalRisk:
    """Change in portfolio 1% expected shortfall per share of the candidate, and the covariance of
    its per-share P&L vector with the book's in every game it touches. Not wired into quoting."""
    base = _exposure(book, models, exposure, n_draws, seed)
    cand = base.derive([replace(candidate, size=1.0)])
    pb, pc = base.pnl_draws(), cand.pnl_draws()
    es0 = es_at(pb, 0.01)
    size = candidate.size if candidate.size > 0 else 1.0
    es1 = es_at(pb + size * pc, 0.01)
    k = max(1, int(round(0.01 * pb.shape[0])))
    tail = np.argpartition(pb, k - 1)[:k]
    cov: dict[str, float] = {}
    corr: dict[str, float] = {}
    for g, vc in cand.game_vec.items():
        vb = base.game_vec.get(g)
        if vb is None:
            cov[g] = corr[g] = 0.0
            continue
        c = float(np.mean((vc - vc.mean()) * (vb - vb.mean())))
        cov[g] = c
        sd = float(vc.std() * vb.std())
        corr[g] = c / sd if sd > 0 else 0.0
    return MarginalRisk(size, es0, es1, (es1 - es0) / size, float(-pc[tail].mean()), cov, corr)


def usage(exposure: Exposure, book: Book, caps: Caps) -> dict[str, dict[str, Any]]:
    """How much of each cap the current book uses (ratio >= 1 = breached), with what drives it."""
    out: dict[str, dict[str, Any]] = {}
    rep_deltas: list[tuple[str, float]] = []
    for g in exposure.game_vec:
        rep_deltas += [(f"{d.game} {d.label}", d.delta) for d in exposure.canonical_deltas(g)]
    rep_deltas += [(exposure.factors[k].label, d) for k, d in exposure.factor_delta.items()]
    L = caps.limit("outcome")
    if rep_deltas and L:
        lbl, d = max(rep_deltas, key=lambda t: abs(t[1]))
        out["outcome"] = {"ratio": abs(d) / L, "value": d, "limit": L, "driver": lbl}
    L = caps.limit("game")
    if exposure.game_vec and L:
        g, w = min(((g, float(v.min())) for g, v in exposure.game_vec.items()), key=lambda t: t[1])
        out["game"] = {"ratio": max(0.0, -w) / L, "value": w, "limit": L, "driver": g}
    L = caps.limit("es1")
    if L and (exposure.game_vec or exposure.factor_delta):
        es = es_at(exposure.pnl_draws(), 0.01)
        out["es1"] = {"ratio": es / L, "value": es, "limit": L, "driver": "portfolio"}
    for cap, what in (("sport", "sport"), ("day", "date")):
        L = caps.limit(cap)
        used = book.notional_by(what)
        if L and used:
            k, v = max(used.items(), key=lambda t: t[1])
            out[cap] = {"ratio": v / L, "value": v, "limit": L, "driver": k}
    return out
