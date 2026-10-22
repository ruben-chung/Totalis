"""Exposure on the scenario engine (stage 3 part 1, claude-briefs/risk-exposure-brief.md).

Every active position is decomposed into independent *components*: one per game that has a
``GameModel`` (the product of its legs' indicators over that game's scenarios) and one Bernoulli
*factor* per leg the engine does not model (NBA, props, unsupported families, games with no
model). Under the independence certificate the combo's payoff is the product of its components,
so for a position with coefficient ``a = sign * size`` and price ``q``:

* its P&L vector in game g is ``a * (ind_g * prod_{c != g} p_c - q)``: the conditional expectation
  of the P&L given game g's scenario, with every other component at its probability;
* its delta to a factor's outcome is ``a * prod_{c != f} p_c * orient`` (+1 when the leg pays
  with the factor, -1 when it pays against it, i.e. the complement side of the same market);
* delta to any outcome O of a modeled game is ``E[V_g | O] - E[V_g | not O]`` on the scenarios.

Per-game vectors are conditional expectations, so they do not sum to the portfolio: the portfolio
distribution is a Monte Carlo that draws one scenario per game and one uniform per factor (common
random numbers, ``Draws``), evaluates every position exactly, and sums. Positions with the same
components are merged first, so a book of round-trip quotes costs one pass per distinct combo.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from ..sports.legspec import LegSpec, LegSpecError, canonical_key, complement_side, contract_key
from .book import Book, Position

log = logging.getLogger(__name__)
GAME, FACTOR = "game", "factor"


# ----------------------------------------------------------------------------- models
class ModelPool:
    """game prefix -> GameModel | None. Uses the quoting cache's models when given, a mapping of
    prebuilt models, and otherwise builds NFL games from the recorded books on first use."""

    def __init__(self, models: Mapping[str, Any] | None = None, *, cache: Any = None, build: bool = True,
                 books_db: str | Path | None = None, rfq_db: str | Path | None = None, at_ms: int | None = None,
                 config: Any = None, seed: int = 0, builder: Callable[[str], Any] | None = None):
        self.models: dict[str, Any] = dict(models or {})
        self.cache = cache
        self.build = build
        self.books_db, self.rfq_db, self.at_ms, self.config, self.seed = books_db, rfq_db, at_ms, config, seed
        self.builder = builder
        self.errors: dict[str, str] = {}
        self.build_s: dict[str, float] = {}

    def get(self, prefix: str | None) -> Any | None:
        if not prefix:
            return None
        if prefix in self.models:
            return self.models[prefix]
        if prefix in self.errors:
            return None
        if self.cache is not None:
            e = self.cache.entries.get(prefix)
            if e is not None and e.model is not None:
                self.models[prefix] = e.model
                return e.model
        if not self.build or not prefix.startswith("nfl-"):
            self.errors[prefix] = "no model (not built)" if prefix.startswith("nfl-") else "sport not modeled"
            return None
        t0 = time.perf_counter()
        try:
            model = self.builder(prefix) if self.builder else self._build(prefix)
        except Exception as e:  # recorded, the game falls back to factors
            self.errors[prefix] = f"{type(e).__name__}: {e}"
            self.build_s[prefix] = time.perf_counter() - t0
            return None
        self.build_s[prefix] = time.perf_counter() - t0
        self.models[prefix] = model
        return model

    def _build(self, prefix: str) -> Any:
        from ..ladders import game_markets
        from ..pricing.joint import build_game
        kw: dict[str, Any] = {}
        if self.books_db:
            kw["books_db"] = self.books_db
        if self.rfq_db:
            kw["rfq_db"] = self.rfq_db
        quotes = game_markets(prefix, self.at_ms, **kw)
        if not quotes:
            raise RuntimeError("no quotes in books")
        return build_game(quotes, config=self.config, seed=self.seed)


class Draws:
    """Common random numbers: one scenario index per game and one uniform per factor, memoised,
    seeded per key so a candidate evaluated separately sees the same draws as the book."""

    def __init__(self, n: int = 20_000, seed: int = 0):
        self.n, self.seed = n, seed
        self._game: dict[str, np.ndarray] = {}
        self._factor: dict[str, np.ndarray] = {}

    def _rng(self, key: str) -> np.random.Generator:
        h = int.from_bytes(hashlib.blake2b(f"{self.seed}|{key}".encode(), digest_size=8).digest(), "little")
        return np.random.default_rng(h)

    def game(self, prefix: str, n_scen: int) -> np.ndarray:
        if prefix not in self._game:
            self._game[prefix] = self._rng("g:" + prefix).integers(0, n_scen, self.n)
        return self._game[prefix]

    def factor(self, key: str) -> np.ndarray:
        if key not in self._factor:
            self._factor[key] = self._rng("f:" + key).random(self.n)
        return self._factor[key]


# ----------------------------------------------------------------------------- decomposition
@dataclass
class Factor:
    key: str                  # contract key (market) or position_id
    label: str
    p: float                  # probability of the reference side
    legs: int = 0


@dataclass(frozen=True)
class Component:
    kind: str                 # game | factor
    key: str                  # game prefix | factor key
    orient: int = 1           # factor: +1 pays with the reference side, -1 against it
    p: float = 0.0            # probability the component pays

    @property
    def sig(self) -> tuple[str, str, int]:
        return (self.kind, self.key, self.orient)


@dataclass
class Term:
    """Merged positions with identical components: P&L = a * I - c."""
    comps: tuple[Component, ...]
    inds: dict[str, np.ndarray]         # game prefix -> indicator product (float32, n_scen)
    a: float = 0.0                      # sum of sign * size
    c: float = 0.0                      # sum of sign * size * price
    n: int = 0
    rfq_ids: list[str] = field(default_factory=list)

    def p_rest(self, skip: Component) -> float:
        out = 1.0
        for comp in self.comps:
            if comp is not skip:
                out *= comp.p
        return out


class Exposure:
    def __init__(self, book: Book | None, models: ModelPool | Mapping[str, Any] | None, *, positions: Iterable[Position] | None = None,
                 n_draws: int = 20_000, seed: int = 0, draws: Draws | None = None, base: "Exposure | None" = None):
        """``base``: share that exposure's models, draws and factor definitions (orientation and
        probability), so a candidate's vectors are comparable with the book's."""
        self.pool = base.pool if base is not None else (models if isinstance(models, ModelPool) else ModelPool(models, build=False))
        self.positions = list(positions) if positions is not None else (book.active() if book is not None else [])
        self.draws = draws or (base.draws if base is not None else Draws(n_draws, seed))
        self.factors: dict[str, Factor] = {k: Factor(f.key, f.label, f.p) for k, f in base.factors.items()} if base is not None else {}
        self._factor_ref: dict[str, LegSpec] = dict(base._factor_ref) if base is not None else {}
        self.terms: dict[tuple, Term] = {}
        self.game_vec: dict[str, np.ndarray] = {}
        self.game_positions: Counter[str] = Counter()
        self.factor_delta: dict[str, float] = {}
        self.factor_positions: Counter[str] = Counter()
        self.flags: Counter[str] = Counter()
        self.timings: dict[str, float] = {}
        t0 = time.perf_counter()
        built0 = sum(self.pool.build_s.values())
        for pos in self.positions:
            self._add(pos)
        t1 = time.perf_counter()
        self._aggregate()
        self.timings["decompose"] = (t1 - t0) - (sum(self.pool.build_s.values()) - built0)   # model builds reported apart
        self.timings["aggregate"] = time.perf_counter() - t1
        self._mc: dict[str, Any] | None = None

    # -- decomposition ---------------------------------------------------------------
    def _add(self, pos: Position) -> None:
        by_game: dict[str | None, list] = {}
        for l in pos.legs:
            by_game.setdefault(l.game, []).append(l)
        comps: list[Component] = []
        inds: dict[str, np.ndarray] = {}
        for game, legs in by_game.items():
            model = self.pool.get(game) if game else None
            ind = None
            if model is not None and all(l.spec is not None for l in legs):
                ind = np.ones(model.scen.n, dtype=np.float32)
                for l in legs:
                    try:
                        li = model.indicator(l.spec)
                    except (ValueError, LegSpecError):
                        li = None
                    if li is None:
                        ind = None
                        self.flags["leg_unsupported_by_model"] += 1
                        break
                    ind = ind * li
            if ind is not None:
                assert game is not None
                comps.append(Component(GAME, game, 1, float(ind.mean(dtype=np.float64))))
                inds[game] = ind
                continue
            if len(legs) > 1:
                self.flags["unmodeled_same_game_as_independent"] += 1
            for l in legs:
                comps.append(self._factor_component(l))
        key = tuple(sorted(c.sig for c in comps))
        term = self.terms.get(key)
        if term is None:
            term = self.terms[key] = Term(tuple(comps), inds)
        term.a += pos.sign * pos.size
        term.c += pos.sign * pos.size * pos.price
        term.n += 1
        term.rfq_ids.append(pos.rfq_id)

    def _factor_component(self, leg: Any) -> Component:
        p = leg.p
        if p is None:
            p = 0.5
            self.flags["leg_p_missing_assumed_0.5"] += 1
        spec = leg.spec
        if spec is None:
            key = leg.position_id
            f = self.factors.get(key)
            if f is None:
                f = self.factors[key] = Factor(key, f"{leg.slug or leg.position_id[:12]}|{leg.outcome or '?'}", p)
            f.legs += 1
            return Component(FACTOR, key, 1, f.p)
        ck = contract_key(spec)
        ref = self._factor_ref.get(ck)
        if ref is None:
            self._factor_ref[ck] = spec
            f = self.factors[ck] = Factor(ck, canonical_key(spec), p)
            f.legs += 1
            return Component(FACTOR, ck, 1, f.p)
        f = self.factors[ck]
        f.legs += 1
        if spec.side == ref.side:
            return Component(FACTOR, ck, 1, f.p)
        try:
            comp_side = complement_side(ref)
        except LegSpecError:
            comp_side = None
        if spec.side == comp_side:
            return Component(FACTOR, ck, -1, 1.0 - f.p)
        key = canonical_key(spec)                      # third outcome of a 3-way market: its own factor
        self.flags["three_way_side_as_own_factor"] += 1
        g = self.factors.get(key)
        if g is None:
            g = self.factors[key] = Factor(key, key, p)
        g.legs += 1
        return Component(FACTOR, key, 1, g.p)

    def _aggregate(self) -> None:
        for term in self.terms.values():
            for comp in term.comps:
                p_rest = term.p_rest(comp)
                if comp.kind == GAME:
                    ind = term.inds[comp.key]
                    v = self.game_vec.get(comp.key)
                    if v is None:
                        v = self.game_vec[comp.key] = np.zeros(ind.shape[0], dtype=np.float64)
                    v += ind.astype(np.float64) * (term.a * p_rest)
                    v -= term.c
                    self.game_positions[comp.key] += term.n
                else:
                    self.factor_delta[comp.key] = self.factor_delta.get(comp.key, 0.0) + term.a * p_rest * comp.orient
                    self.factor_positions[comp.key] += term.n

    def derive(self, positions: Iterable[Position]) -> "Exposure":
        """Exposure of other positions (a candidate) on this book's models, draws and factors."""
        return Exposure(None, None, positions=positions, base=self)

    # -- per game ------------------------------------------------------------------------
    def games(self) -> list[str]:
        return list(self.game_vec)

    def delta(self, prefix: str, spec: LegSpec) -> float | None:
        """E[V_g | spec pays] - E[V_g | it does not] (dollars)."""
        v = self.game_vec.get(prefix)
        model = self.pool.get(prefix)
        if v is None or model is None:
            return None
        try:
            o = model.indicator(spec)
        except (ValueError, LegSpecError):
            return None
        if o is None:
            return None
        return delta_on(v, o)

    def game_distribution(self, prefix: str) -> dict[str, float]:
        v = self.game_vec[prefix]
        return {"mean": float(v.mean()), "sd": float(v.std()), "worst": float(v.min()), "best": float(v.max()),
                "q01": float(np.quantile(v, 0.01)), "q05": float(np.quantile(v, 0.05))}

    def canonical_deltas(self, prefix: str) -> list["OutcomeDelta"]:
        model = self.pool.get(prefix)
        if model is None or prefix not in self.game_vec:
            return []
        outs, O, m = outcome_matrix(model)
        ds = deltas_on(self.game_vec[prefix], O, m)
        return [OutcomeDelta(prefix, label, canonical_key(spec), float(m[i] / O.shape[1]), float(ds[i]))
                for i, (label, spec) in enumerate(outs) if not np.isnan(ds[i])]

    # -- portfolio -------------------------------------------------------------------------
    def pnl_draws(self) -> np.ndarray:
        """Exact portfolio P&L per Monte Carlo draw (independent games, independent factors)."""
        return self.monte_carlo()["pnl"]

    def monte_carlo(self) -> dict[str, Any]:
        if self._mc is not None:
            return self._mc
        t0 = time.perf_counter()
        K = self.draws.n
        total = np.zeros(K, dtype=np.float64)
        for term in self.terms.values():
            I = np.ones(K, dtype=np.float32)
            for comp in term.comps:
                if comp.kind == GAME:
                    idx = self.draws.game(comp.key, term.inds[comp.key].shape[0])
                    I = I * term.inds[comp.key][idx]
                else:
                    f = self.factors[comp.key]
                    b = self.draws.factor(comp.key) < f.p
                    I = I * (b if comp.orient > 0 else ~b)
            total += term.a * I.astype(np.float64) - term.c
        # first-order attribution per game / factor (exact for single-component positions)
        contrib: dict[str, np.ndarray] = {}
        for g, v in self.game_vec.items():
            idx = self.draws.game(g, v.shape[0])
            contrib["game:" + g] = v[idx] - v.mean()
        for k, d in self.factor_delta.items():
            f = self.factors[k]
            contrib["factor:" + k] = d * ((self.draws.factor(k) < f.p).astype(np.float64) - f.p)
        self._mc = {"pnl": total, "contrib": contrib, "seconds": time.perf_counter() - t0}
        return self._mc

    def tail_index(self, level: float = 0.01) -> np.ndarray:
        """Draw indices of the worst ``level`` share of the portfolio (cached until the P&L changes)."""
        mc = self.monte_carlo()
        key = f"tail{level}"
        if key not in mc:
            k = max(1, int(round(level * self.draws.n)))
            mc[key] = np.argpartition(mc["pnl"], k - 1)[:k]
        return mc[key]

    def absorb(self, cand: "Exposure", size: float) -> None:
        """Add ``size`` shares of a derived per-share exposure to this one in place (vectors, factor
        deltas and the Monte Carlo P&L; linear, exact). Used between full rebuilds so pending quotes
        count immediately. Terms are not merged, so ``report()`` attribution is stale until a rebuild."""
        for g, vc in cand.game_vec.items():
            v = self.game_vec.get(g)
            if v is None:
                self.game_vec[g] = vc * size
            else:
                v += vc * size
            self.game_positions[g] += cand.game_positions.get(g, 0)
        for k, d in cand.factor_delta.items():
            self.factor_delta[k] = self.factor_delta.get(k, 0.0) + d * size
            self.factor_positions[k] += cand.factor_positions.get(k, 0)
            if k not in self.factors:
                self.factors[k] = cand.factors[k]
        for ck, ref in cand._factor_ref.items():
            self._factor_ref.setdefault(ck, ref)
        if self._mc is not None:
            self._mc = {"pnl": self._mc["pnl"] + size * cand.pnl_draws(), "contrib": self._mc["contrib"], "seconds": 0.0}
        self.positions.extend(cand.positions)

    def portfolio(self, top: int = 10) -> "PortfolioDist":
        mc = self.monte_carlo()
        pnl = mc["pnl"]
        return portfolio_dist(pnl, mc["contrib"], top=top, seconds=mc["seconds"])

    # -- report ----------------------------------------------------------------------------
    def report(self, *, top: int = 10, outcomes: Sequence[tuple[str, LegSpec]] = ()) -> "ExposureReport":
        t0 = time.perf_counter()
        games = []
        for g in self.game_vec:
            model = self.pool.get(g)
            games.append(GameExposure(g, getattr(model, "game_key", None), self.game_positions[g], self.canonical_deltas(g),
                                      **self.game_distribution(g)))
        extra = []
        for g in self.game_vec:
            for label, spec in outcomes:
                d = self.delta(g, spec)
                if d is not None:
                    extra.append(OutcomeDelta(g, label, canonical_key(spec), self.pool.get(g).p_leg(spec), d))
        facs = [FactorDelta(k, self.factors[k].label, self.factors[k].p, d, self.factor_positions[k]) for k, d in self.factor_delta.items()]
        port = self.portfolio(top=top)
        self.timings["report"] = time.perf_counter() - t0
        return ExposureReport(n_positions=len(self.positions), n_terms=len(self.terms), games=games, factors=facs, extra=extra,
                              portfolio=port, model_errors=dict(self.pool.errors), flags=dict(self.flags),
                              timings={**self.timings, "model_build": sum(self.pool.build_s.values())})


# ----------------------------------------------------------------------------- outcomes
def delta_on(v: np.ndarray, o: np.ndarray) -> float | None:
    o64 = o.astype(np.float64)
    m = o64.sum()
    n = o64.shape[0]
    if m <= 0 or m >= n:
        return None
    return float((v @ o64) / m - (v @ (1.0 - o64)) / (n - m))


def outcome_matrix(model: Any) -> tuple[list[tuple[str, LegSpec]], np.ndarray, np.ndarray]:
    """Canonical outcomes of a model as one float64 matrix O (k x n) with the per-outcome scenario
    counts m, cached on the model: deltas of any vector v to all k outcomes are one matvec."""
    cached = getattr(model, "_outcome_matrix", None)
    if cached is not None:
        return cached
    outs = canonical_outcomes(model)
    rows, keep = [], []
    for label, spec in outs:
        o = model.indicator(spec)
        if o is None:
            continue
        rows.append(o.astype(np.float64))
        keep.append((label, spec))
    O = np.stack(rows) if rows else np.zeros((0, model.scen.n), dtype=np.float64)
    m = O.sum(axis=1)
    res = (keep, O, m)
    try:
        model._outcome_matrix = res
    except AttributeError:  # pragma: no cover
        pass
    return res


def deltas_on(v: np.ndarray, O: np.ndarray, m: np.ndarray) -> np.ndarray:
    """E[v | O_k] - E[v | not O_k] for every row of O (nan where an outcome never / always happens)."""
    n = v.shape[0]
    vo = O @ v
    tot = float(v.sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        out = vo / m - (tot - vo) / (n - m)
    out[(m <= 0) | (m >= n)] = np.nan
    return out


def canonical_outcomes(model: Any) -> list[tuple[str, LegSpec]]:
    """Each team's win, over/under at the main total, each team's cover at the main spread, each
    team total over/under at its main line (main = the quoted line with p closest to 0.5)."""
    cached = getattr(model, "_canonical_outcomes", None)
    if cached is not None:
        return cached
    specs = [l.spec for l in model.legs]
    if not specs:
        return []
    s0 = specs[0]
    base = dict(venue="engine", sport=s0.sport, away=s0.away, home=s0.home, game_date=s0.game_date, period="FG")
    out: list[tuple[str, LegSpec]] = []
    for team in (s0.away, s0.home):
        out.append((f"{team} win", LegSpec(family="moneyline", side=team, **base)))

    def main(cands: list[LegSpec]) -> LegSpec | None:
        best, bd = None, 9.0
        for s in cands:
            p = model.p_leg(s)
            if p is None:
                continue
            d = abs(p - 0.5)
            if d < bd - 1e-12 or (abs(d - bd) <= 1e-12 and best is not None and (s.line or 0) < (best.line or 0)):
                best, bd = s, d
        return best

    tot = main([s for s in specs if s.family == "total" and s.period == "FG" and s.line is not None])
    if tot is not None:
        for side in ("over", "under"):
            out.append((f"total {side} {tot.line:g}", LegSpec(family="total", side=side, line=tot.line, **base)))
    spr = main([s for s in specs if s.family == "spread" and s.period == "FG" and s.line is not None])
    if spr is not None:
        other = s0.home if spr.team == s0.away else s0.away
        for side in (spr.team, other):
            lbl = f"{spr.team} {spr.line:+g} {'covers' if side == spr.team else 'fails'}"
            out.append((lbl, LegSpec(family="spread", side=side, team=spr.team, line=spr.line, **base)))
    for team in (s0.away, s0.home):
        tt = main([s for s in specs if s.family == "team_total" and s.period == "FG" and s.team == team and s.line is not None])
        if tt is not None:
            for side in ("over", "under"):
                out.append((f"{team} total {side} {tt.line:g}", LegSpec(family="team_total", side=side, team=team, line=tt.line, **base)))
    try:
        model._canonical_outcomes = out
    except AttributeError:  # pragma: no cover
        pass
    return out


# ----------------------------------------------------------------------------- report types
@dataclass
class OutcomeDelta:
    game: str
    label: str
    key: str
    p: float | None
    delta: float                 # dollars: E[P&L | O] - E[P&L | not O]

    def as_dict(self) -> dict[str, Any]:
        return {"game": self.game, "label": self.label, "key": self.key, "p": self.p, "delta": self.delta}


@dataclass
class FactorDelta:
    key: str
    label: str
    p: float
    delta: float
    n_positions: int

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "p": self.p, "delta": self.delta, "n_positions": self.n_positions}


@dataclass
class GameExposure:
    game: str
    game_key: str | None
    n_positions: int
    deltas: list[OutcomeDelta]
    mean: float
    sd: float
    worst: float
    best: float
    q01: float
    q05: float

    def as_dict(self) -> dict[str, Any]:
        return {"game": self.game, "game_key": self.game_key, "n_positions": self.n_positions,
                "deltas": [d.as_dict() for d in self.deltas], "mean": self.mean, "sd": self.sd, "worst": self.worst,
                "best": self.best, "q01": self.q01, "q05": self.q05}


@dataclass
class PortfolioDist:
    n_draws: int
    mean: float
    sd: float
    worst: float
    q01: float
    q05: float
    es1: float                   # expected shortfall at 1%: minus the mean P&L of the worst 1% of draws (a loss, >= 0 usually)
    es5: float
    top_contributors: list[tuple[str, float]]   # (game:prefix | factor:key, mean contribution in the 1% tail)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"n_draws": self.n_draws, "mean": self.mean, "sd": self.sd, "worst": self.worst, "q01": self.q01,
                "q05": self.q05, "es1": self.es1, "es5": self.es5, "top_contributors": list(self.top_contributors),
                "seconds": self.seconds}


@dataclass
class ExposureReport:
    n_positions: int
    n_terms: int
    games: list[GameExposure]
    factors: list[FactorDelta]
    extra: list[OutcomeDelta]
    portfolio: PortfolioDist
    model_errors: dict[str, str]
    flags: dict[str, int]
    timings: dict[str, float]

    def all_deltas(self) -> list[tuple[str, float, str]]:
        """(label, dollar delta, kind) over game outcomes and factors."""
        out = [(f"{d.game} {d.label}", d.delta, "outcome") for g in self.games for d in g.deltas]
        out += [(f.label, f.delta, "factor") for f in self.factors]
        return out

    def top_deltas(self, n: int = 10) -> list[tuple[str, float, str]]:
        return sorted(self.all_deltas(), key=lambda t: -abs(t[1]))[:n]

    def worst_game(self) -> GameExposure | None:
        return min(self.games, key=lambda g: g.worst) if self.games else None

    def as_dict(self) -> dict[str, Any]:
        return {"n_positions": self.n_positions, "n_terms": self.n_terms, "games": [g.as_dict() for g in self.games],
                "factors": [f.as_dict() for f in self.factors], "extra": [d.as_dict() for d in self.extra],
                "portfolio": self.portfolio.as_dict(), "model_errors": dict(self.model_errors), "flags": dict(self.flags),
                "timings": dict(self.timings)}


def es_at(pnl: np.ndarray, level: float) -> float:
    """Expected shortfall as a loss: minus the mean of the worst ``level`` share of draws."""
    k = max(1, int(round(level * pnl.shape[0])))
    tail = np.partition(pnl, k - 1)[:k]
    return float(-tail.mean())


def portfolio_dist(pnl: np.ndarray, contrib: Mapping[str, np.ndarray] | None = None, *, top: int = 10,
                   seconds: float = 0.0) -> PortfolioDist:
    K = pnl.shape[0]
    k1 = max(1, int(round(0.01 * K)))
    tail_idx = np.argpartition(pnl, k1 - 1)[:k1]
    tops: list[tuple[str, float]] = []
    if contrib:
        tops = sorted(((k, float(v[tail_idx].mean())) for k, v in contrib.items()), key=lambda t: t[1])[:top]
    return PortfolioDist(K, float(pnl.mean()), float(pnl.std()), float(pnl.min()), float(np.quantile(pnl, 0.01)),
                         float(np.quantile(pnl, 0.05)), float(-pnl[tail_idx].mean()), es_at(pnl, 0.05), tops, seconds)
