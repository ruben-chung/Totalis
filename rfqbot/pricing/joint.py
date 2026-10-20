"""Public API of the joint engine: ``build_game(quotes) -> GameModel``.

``GameModel.joint(specs)`` evaluates the legs' payoffs over the scenario set and returns a
``JointQuote``. The interval ``[lo, hi]`` is the min/max of ``p_joint`` under a small fixed
shock set (``DEFAULT_SHOCKS``), clipped to the Fréchet bounds:
  * ``halves_corr`` +/- 0.15: the Gaussian-copula coupling with a shifted correlation instead of
    the 2H-ladder increments prior (re-sampling only),
  * ``prior_rho`` -0.2 / +0.2 on the prior margin-total correlation (matters when team-total
    ladders are missing or thin; otherwise the IPF targets pin it and the shock is a no-op),
  * ``bootstrap`` seeds 1 and 2: every ladder observation redrawn as N(p, err) and the whole
    model rebuilt.
Shocked models are built lazily and cached on the GameModel, so evaluating many leg sets costs
one scenario pass each.

``p_cal`` is the calibrated probability (``calibrate.py``: per family pair, shrunk toward its
group, lambda on the departure from the engine-marginal product plus tail maps), fitted on
resolved games; ``calibration_level`` says whether the pair's own parameters, the group fallback
or nothing was applied; the interval is shifted with it and widened by the out-of-sample model
error. Without a calibration file ``p_cal == p_joint`` and ``calibrated`` is False.

``p_legs`` are the engine's own marginals and ``max_marginal_error`` is the largest distance
from the input fair probabilities over the legs of the request; ``marginal_check()`` does the
same for every leg of the game. The engine must reproduce its inputs; that number is the proof.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from ..sports.legspec import LegSpec, canonical_key
from ..sports.nfl_poly import parse_poly_nfl
from .calibrate import Calibration, default_calibration, family_pair_key, group_of
from .indicators import indicator
from .joint_nfl import FIT_FAMILIES, EngineConfig, Leg, PeriodModel, Scenarios, build_period, couple_periods
from .quotes import Fair, fair_from_quote


@dataclass(frozen=True)
class Shock:
    kind: str       # halves_corr | prior_rho | bootstrap
    value: float

    @property
    def name(self) -> str:
        return f"{self.kind}{self.value:+g}" if self.kind != "bootstrap" else f"bootstrap#{int(self.value)}"


DEFAULT_SHOCKS: tuple[Shock, ...] = (
    Shock("halves_corr", +0.15), Shock("halves_corr", -0.15),
    Shock("prior_rho", -0.2), Shock("prior_rho", +0.2),
    Shock("bootstrap", 1), Shock("bootstrap", 2),
)


@dataclass
class JointQuote:
    p_joint: float                     # raw engine probability
    p_independent: float               # product of the input fair probabilities (engine marginals where missing)
    frechet_lo: float
    frechet_hi: float
    lo: float                          # interval: shocks (input uncertainty) widened by the group's model error
    hi: float
    p_legs: list[float | None]
    max_marginal_error: float | None
    unsupported: list[str]
    flags: list[str]
    n_scenarios: int
    built_at: float
    shocked: dict[str, float] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    p_cal: float | None = None         # calibrated probability (calibrate.Calibration); what quote assembly uses
    group: str | None = None           # within_FG | within_half | cross
    family_pair: str | None = None     # "family/period x family/period" (two legs) used for the calibration lookup
    model_error: float = 0.0           # residual model error of the pair (group fallback), added to the interval
    calibrated: bool = False           # False when running without a calibration file (p_cal == p_joint)
    calibration_level: str = "none"    # pair | group | none


INFORMATIVE_ERR = 0.05      # legs with a wider error (book spread > ~0.1) are placeholders, reported separately


@dataclass
class MarginalCheck:
    key: str
    fair: float | None
    engine: float | None
    diff: float | None
    flagged: bool
    informative: bool = True     # two-sided quote with err <= INFORMATIVE_ERR
    fitted: bool = False         # family/period used in the ladder fit


class GameModel:
    def __init__(self, game_key: str, legs: list[Leg], periods: dict[str, PeriodModel], scen: Scenarios,
                 cfg: EngineConfig, seed: int, flags: list[str], notes: list[str], quotes: list[Any],
                 timings: dict[str, float], parse_errors: list[str]):
        self.game_key = game_key
        self.legs = legs
        self.periods = periods
        self.scen = scen
        self.cfg = cfg
        self.seed = seed
        self.flags = flags
        self.notes = notes
        self.quotes = quotes
        self.timings = timings
        self.parse_errors = parse_errors
        self.built_at = time.time()
        ats = [int(getattr(q, "at_ms", 0) or 0) for q in quotes]
        self.input_at_ms = (min(ats), max(ats)) if ats else (0, 0)
        self._shocked: dict[str, "GameModel"] = {}
        self._fair_by_key: dict[str, Fair] = {canonical_key(l.spec): l.fair for l in legs if l.fair is not None}
        self._ind_cache: dict[str, np.ndarray | None] = {}

    # -- per leg -------------------------------------------------------------------------
    def indicator(self, spec: LegSpec) -> np.ndarray | None:
        k = canonical_key(spec) + "|" + spec.rules.get("tie", "")
        if k not in self._ind_cache:
            if spec.sport != "nfl" or f"{spec.away}@{spec.home}" not in self.game_key:
                raise ValueError(f"{canonical_key(spec)} is not a leg of {self.game_key}")
            self._ind_cache[k] = indicator(spec, self.scen)
        return self._ind_cache[k]

    def p_leg(self, spec: LegSpec) -> float | None:
        ind = self.indicator(spec)
        return None if ind is None else float(ind.mean(dtype=np.float64))

    def p_tie(self, period: str = "FG") -> float:
        return self.periods[period].p_tie() if period in self.periods else float(self.indicator(
            LegSpec(venue="engine", sport="nfl", away=self.legs[0].spec.away, home=self.legs[0].spec.home,
                    game_date=self.legs[0].spec.game_date, family="moneyline", period=period, side="tie")).mean())

    def marginal_check(self) -> list[MarginalCheck]:
        out = []
        for leg in self.legs:
            e = self.p_leg(leg.spec)
            f = leg.fair.p if leg.fair else None
            d = None if (e is None or f is None) else abs(e - f)
            flagged = d is not None and leg.fair is not None and d > max(3 * leg.fair.err, 0.01)
            informative = leg.fair is not None and leg.fair.err <= INFORMATIVE_ERR
            fitted = leg.spec.family in FIT_FAMILIES and leg.spec.period in self.periods
            out.append(MarginalCheck(canonical_key(leg.spec), f, e, d, flagged, informative, fitted))
        return out

    # -- joint ---------------------------------------------------------------------------
    def _p_joint(self, specs: Sequence[LegSpec]) -> tuple[float, list[float | None], list[str]]:
        acc = np.ones(self.scen.n, dtype=np.float32)
        p_legs: list[float | None] = []
        unsupported: list[str] = []
        for s in specs:
            ind = self.indicator(s)
            if ind is None:
                unsupported.append(canonical_key(s))
                p_legs.append(None)
                continue
            acc = acc * ind
            p_legs.append(float(ind.mean(dtype=np.float64)))
        return float(acc.mean(dtype=np.float64)), p_legs, unsupported

    def joint(self, specs: Sequence[LegSpec], shocks: Iterable[Shock] = DEFAULT_SHOCKS,
              calibration: Calibration | None = None) -> JointQuote:
        specs = list(specs)
        p, p_legs, unsupported = self._p_joint(specs)
        supported = [x for x in p_legs if x is not None]
        fr_lo = max(0.0, sum(supported) - (len(supported) - 1)) if supported else 0.0
        fr_lo = 0.0 if fr_lo < 1e-6 else fr_lo
        fr_hi = min(supported) if supported else 1.0
        fairs = [self._fair_by_key.get(canonical_key(s)) for s in specs]
        p_ind = 1.0
        errs = []
        for f, pl in zip(fairs, p_legs):
            if pl is None:
                continue
            p_ind *= f.p if f else pl
            if f:
                errs.append(abs(f.p - pl))
        shocked: dict[str, float] = {}
        for sh in shocks:
            m = self.shocked(sh)
            shocked[sh.name], _, _ = m._p_joint(specs)
        # calibration: lambda per group on (p_joint - product of engine marginals), tail map, model error
        cal = calibration if calibration is not None else default_calibration()
        sup_specs = [s for s, pl in zip(specs, p_legs) if pl is not None]
        periods = sorted({s.period for s in sup_specs})
        group = group_of(periods[0], periods[-1]) if periods else "within_FG"
        fp = family_pair_key(sup_specs[0].family, sup_specs[0].period, sup_specs[1].family, sup_specs[1].period) if len(sup_specs) == 2 else None
        p_eng_ind = 1.0
        for pl in supported:
            p_eng_ind *= pl
        logical = p <= fr_lo + 1e-9 or p >= fr_hi - 1e-9     # impossible / nested combos are exact, not calibrated
        if len(supported) > 1 and not logical:
            p_cal, level = cal.apply(fp, group, p, p_eng_ind)
            model_err = cal.model_error(fp, group)
        else:
            p_cal, level, model_err = p, "none", 0.0
        shift = p_cal - p
        vals = [v + shift for v in (p, *shocked.values())]
        lo, hi = min(vals) - model_err, max(vals) + model_err
        lo, hi = max(fr_lo, min(lo, p_cal)), min(fr_hi, max(hi, p_cal))
        return JointQuote(p_joint=p, p_independent=p_ind, frechet_lo=fr_lo, frechet_hi=fr_hi, lo=lo, hi=hi,
                          p_legs=p_legs, max_marginal_error=max(errs) if errs else None, unsupported=unsupported,
                          flags=list(self.flags), n_scenarios=self.scen.n, built_at=self.built_at, shocked=shocked,
                          keys=[canonical_key(s) for s in specs], p_cal=float(min(max(p_cal, 0.0), 1.0)), group=group,
                          family_pair=fp, model_error=model_err, calibrated=not cal.identity, calibration_level=level)

    def shocked(self, shock: Shock) -> "GameModel":
        if shock.name not in self._shocked:
            if shock.kind == "halves_corr":   # alternative coupling: Gaussian prior with a shifted correlation
                scen = couple_periods(self.periods["FG"], self.periods["1H"], self.scen.n, self.seed, corr_shift=shock.value,
                                      h2=self.periods.get("2H"), mode="copula")
                m = GameModel(self.game_key, self.legs, self.periods, scen, self.cfg, self.seed, self.flags, self.notes,
                              self.quotes, {}, self.parse_errors)
            elif shock.kind == "prior_rho":
                cfg = EngineConfig(**{**self.cfg.__dict__, "prior_rho_mt": shock.value})
                m = _build(self.quotes, cfg, self.seed, legs=self.legs)
            elif shock.kind == "bootstrap":
                m = _build(self.quotes, self.cfg, self.seed, legs=self.legs, bootstrap_seed=int(shock.value))
            else:
                raise ValueError(f"unknown shock {shock.kind}")
            self._shocked[shock.name] = m
        return self._shocked[shock.name]

    # -- diagnostics ---------------------------------------------------------------------
    def correlation_table(self) -> dict[str, Any]:
        s = self.scen
        H, A, H1, A1 = (x.astype(float) for x in (s.H, s.A, s.H1, s.A1))
        vars_ = {"H": H, "A": A, "M": H - A, "T": H + A, "H1": H1, "A1": A1, "M1": H1 - A1, "T1": H1 + A1,
                 "M2": (H - H1) - (A - A1), "T2": (H - H1) + (A - A1)}
        names = list(vars_)
        mat = np.corrcoef(np.vstack([vars_[n] for n in names]))
        out: dict[str, Any] = {"scenario_corr": {f"{a},{b}": float(mat[i, j]) for i, a in enumerate(names) for j, b in enumerate(names) if i < j}}
        out["table_corr"] = {per: {"M,T": pm.corr("M", "T"), "H,A": pm.corr("H", "A")} for per, pm in self.periods.items()}
        out["means"] = {per: {v: pm.moments(v)[0] for v in "MTHA"} for per, pm in self.periods.items()}
        out["sds"] = {per: {v: pm.moments(v)[1] for v in "MTHA"} for per, pm in self.periods.items()}
        out["halves_copula"] = {"H": s.corr_h, "A": s.corr_a}
        out["clamp_rate"] = s.clamp_rate
        out["halves_residual"] = dict(s.halves_residual)
        out["p_tie"] = {per: pm.p_tie() for per, pm in self.periods.items()}
        return out


def parse_quotes(quotes: Iterable[Any]) -> tuple[list[Leg], list[str]]:
    legs, errors = [], []
    for q in quotes:
        try:
            spec = parse_poly_nfl(q.slug, q.title, q.outcome, q.outcome_index, position_id=getattr(q, "position_id", None))
        except ValueError as e:
            errors.append(f"{q.slug}|{q.outcome}: {e}")
            continue
        legs.append(Leg(spec=spec, fair=fair_from_quote(q), ref=f"{q.slug}|{q.outcome}"))
    return legs, errors


def _build(quotes: list[Any], cfg: EngineConfig, seed: int, *, legs: list[Leg] | None = None,
           bootstrap_seed: int | None = None) -> GameModel:
    t0 = time.perf_counter()
    parse_errors: list[str] = []
    if legs is None:
        legs, parse_errors = parse_quotes(quotes)
    if not legs:
        raise ValueError("no parseable legs")
    keys = {l.spec.game_key for l in legs}
    if len(keys) != 1:
        raise ValueError(f"quotes span several games: {sorted(keys)}")
    game_key = keys.pop()
    rng = np.random.default_rng(1000 + bootstrap_seed) if bootstrap_seed is not None else None
    t1 = time.perf_counter()
    fg = build_period(legs, "FG", cfg, rng=rng)
    half_means = {"M": fg.means["M"] / 2, "T": fg.means["T"] / 2}
    h1 = build_period(legs, "1H", cfg, rng=rng, fallback_means=half_means)
    h2 = build_period(legs, "2H", cfg, rng=rng, fallback_means=half_means)
    t2 = time.perf_counter()
    scen = couple_periods(fg, h1, cfg.n_scenarios, seed, corr_shift=cfg.halves_corr_shift, h2=h2)
    t3 = time.perf_counter()
    flags = [*fg.flags, *h1.flags, *h2.flags]
    if scen.coupling == "copula":
        flags.append(f"halves coupling: {scen.coupling_note}; Gaussian prior with corr sd(1H)/sd(FG)")
    if scen.clamp_rate > cfg.clamp_flag:
        flags.append(f"halves clamp rate {scen.clamp_rate:.1%} (> {cfg.clamp_flag:.0%})")
    for comp, r in scen.halves_residual.items():
        if r > cfg.halves_flag:
            flags.append(f"halves {comp}: {r:.3f} of mass cannot satisfy both the 1H and FG ladders (1H not stochastically below FG)")
    for comp in ("H", "A"):
        ratio = h1.moments(comp)[1] / max(fg.moments(comp)[1], 1e-6)
        if ratio > cfg.halves_sd_ratio_flag and comp in h1.pmfs:
            flags.append(f"1H {comp} sd is {ratio:.2f}x the FG sd (independent increments imply ~0.7): far 1H lines are noisy or stale")
    n_nofair = sum(1 for l in legs if l.fair is None)
    if n_nofair:
        flags.append(f"{n_nofair} leg(s) without a two-sided quote (not used in fitting)")
    if parse_errors:
        flags.append(f"{len(parse_errors)} quote(s) failed to parse")
    notes = [*fg.notes, *h1.notes, *h2.notes, *(n for p in (fg, h1, h2) for pm in p.pmfs.values() for n in pm.notes)]
    if scen.coupling == "increments":
        notes.append(f"halves coupling: independent increments from the 2H ladders ({scen.coupling_note})")
    timings = {"parse": t1 - t0, "fit_ipf": t2 - t1, "scenarios": t3 - t2, "total": t3 - t0}
    return GameModel(game_key, legs, {"FG": fg, "1H": h1, "2H": h2}, scen, cfg, seed, flags, notes, list(quotes), timings, parse_errors)


def build_game(quotes: Iterable[Any], *, config: EngineConfig | None = None, seed: int = 0) -> GameModel:
    """Build the joint model of one game from its market quotes (any objects with the
    ``rfqbot.ladders.MarketQuote`` fields)."""
    return _build(list(quotes), config or EngineConfig(), seed)
