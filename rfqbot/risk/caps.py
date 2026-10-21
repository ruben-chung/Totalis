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
from .exposure import Exposure, ModelPool, deltas_on, es_at, outcome_matrix

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
    hi = min(max(start, 1.0), s_max)
    while feasible(hi):
        if hi >= s_max:
            return INF if s_max >= 1e9 else s_max
        hi = min(hi * 2, s_max)
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


# -- per-cap solvers on a book exposure and a per-share candidate exposure --------------------
def outcome_limit(base: Exposure, cand: Exposure, limit: float | None) -> tuple[float, str, float]:
    """(largest size, binding outcome label, usage): |b + s c| <= limit over the canonical outcomes of
    every modeled game the candidate touches and every factor it touches. ``usage`` is the book's
    largest |b| / limit among the outcomes the candidate *adds to* (same sign), for the skew."""
    best, label, use = INF, "", 0.0
    if limit is None:
        return best, label, use
    for g, vc in cand.game_vec.items():
        model = base.pool.get(g)
        if model is None:
            continue
        outs, O, m = outcome_matrix(model)
        if not outs:
            continue
        cs = deltas_on(vc, O, m)
        vb = base.game_vec.get(g)
        bs = deltas_on(vb, O, m) if vb is not None else np.zeros(len(outs))
        for i, (lbl, _) in enumerate(outs):
            c, b = float(cs[i]), float(bs[i])
            if np.isnan(c) or np.isnan(b):
                continue
            s = linear_limit(b, c, limit)
            if s < best:
                best, label = s, f"{g} {lbl}"
            if c != 0:
                use = max(use, abs(b) / limit)
    for k, c in cand.factor_delta.items():
        b = base.factor_delta.get(k, 0.0)
        s = linear_limit(b, c, limit)
        if s < best:
            best, label = s, cand.factors[k].label
        if c != 0:
            use = max(use, abs(b) / limit)
    return best, label, use


def game_limit(base: Exposure, cand: Exposure, limit: float | None) -> tuple[float, str]:
    """Largest s with min_i(vb_i + s vc_i) >= floor, floor = min(-limit, min vb) (no worse than now).
    Closed form: only scenarios where the candidate loses constrain s."""
    best, label = INF, ""
    if limit is None:
        return best, label
    for g, vc in cand.game_vec.items():
        vb = base.game_vec.get(g)
        vb = np.zeros_like(vc) if vb is None else vb
        floor = min(-limit, float(vb.min()))
        neg = vc < 0
        if not neg.any():
            continue
        s = float(((vb[neg] - floor) / (-vc[neg])).min())
        s = 0.0 if s < 1e-6 else s
        if s < best:
            best, label = s, g
    return best, label


def es1_limit(base: Exposure, cand: Exposure, limit: float | None, start: float) -> tuple[float, float]:
    """(largest s with ES1(book + s cand) <= max(limit, ES1 now), tail mean per share).
    ES1 is convex in s; its slope at 0 is the candidate's mean loss in the book's tail, so the
    linearised size is an upper bound. It is checked exactly and bisected only when it fails."""
    if limit is None or not (cand.game_vec or cand.factor_delta):
        return INF, 0.0
    pb, pc = base.pnl_draws(), cand.pnl_draws()
    tail = base.tail_index(0.01)
    slope = float(-pc[tail].mean())                   # dES1/ds at s -> 0 (loss per share in the tail)
    es0 = float(-pb[tail].mean())
    ceiling = max(limit, es0)
    if slope <= 1e-12:
        return INF, slope                             # hedges the tail: this cap never binds
    s_lin = max(0.0, (ceiling - es0) / slope)
    if s_lin == 0.0:
        return 0.0, slope
    if es_at(pb + s_lin * pc, 0.01) <= ceiling + 1e-9:
        return s_lin, slope

    def feasible(s: float) -> bool:
        return es_at(pb + s * pc, 0.01) <= ceiling + 1e-9
    return bisect_limit(feasible, min(start, s_lin), iters=30, s_max=s_lin), slope


def notional_limits(book: Book, candidate: Position, caps: Caps) -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
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
        out[cap] = (best, label)
    return out


def solve(base: Exposure, cand: Exposure, book: Book, candidate: Position, caps: Caps) -> CapResult:
    """All caps for a per-share candidate exposure ``cand`` against the book exposure ``base``."""
    head: dict[str, float] = {}
    detail: dict[str, str] = {}
    head["outcome"], detail["outcome"], _ = outcome_limit(base, cand, caps.limit("outcome"))
    head["game"], detail["game"] = game_limit(base, cand, caps.limit("game"))
    head["es1"], _ = es1_limit(base, cand, caps.limit("es1"), candidate.size)
    detail["es1"] = "portfolio"
    for cap, (s, lbl) in notional_limits(book, candidate, caps).items():
        head[cap], detail[cap] = s, lbl
    cap_size = min(head.values())
    binding = min(head, key=head.get) if cap_size < candidate.size - 1e-9 else None   # type: ignore[arg-type]
    return CapResult(candidate.size, min(candidate.size, cap_size), cap_size, binding, head,
                     {k: v for k, v in detail.items() if v})


def check(book: Book, candidate: Position, models: ModelPool | Mapping[str, Any] | None = None, *, caps: Caps | None = None,
          exposure: Exposure | None = None, n_draws: int = 20_000, seed: int = 0) -> CapResult:
    """Largest size of ``candidate`` (same legs, sign and price) that keeps every cap satisfied."""
    caps = caps or Caps.from_env()
    base = _exposure(book, models, exposure, n_draws, seed)
    cand = base.derive([replace(candidate, size=1.0)])          # per-share vectors
    return solve(base, cand, book, candidate, caps)


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
    tail = base.tail_index(0.01)
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
