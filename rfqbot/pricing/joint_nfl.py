"""NFL joint score distribution: ladders -> pmfs -> IPF table per period -> scenarios.

Conventions: H = home points, A = away points, M = H - A, T = H + A. Score grid H, A in
[0, 80] (``GRID``), so M in [-80, 80] and T in [0, 160].

Per period (FG, 1H):
  1. ``collect_observations`` orients every parsed leg with a fair probability onto one of the
     four variables: a spread "team X -7.5 / side X" is P(M_X > 7.5); the other outcome and the
     mirror market ("other team -7.5") are observations of the same survival curve; totals ->
     T, team totals -> H or A; a moneyline is an observation of S(0) (see ladders_fit).
  2. each variable with at least ``min_lines`` distinct lines is fitted to a pmf; otherwise it
     is "missing" and the prior carries it (noted in ``PeriodModel.notes``).
  3. the prior is a discretised bivariate normal on (H, A) derived from the margin/total
     normal with scales ``margin_sd`` / ``total_sd`` (halved by ``half_scale`` for 1H) and
     correlation ``prior_rho_mt`` between M and T (default 0), centred on the ladder means.
  4. IPF rescales the table to the fitted 1-D targets in turn until the L1 change of the
     table is below ``ipf_tol``. If the targets are mutually inconsistent (e.g. the team-total
     ladders imply a different mean margin than the spread ladder) IPF cannot satisfy all of
     them; the residual L1 distance per target is reported and flagged above ``ipf_flag_l1``.

Periods are coupled per component with a second IPF: for X in {H, A} a table P(X1, X) on the
81 x 81 grid with structural zeros where X1 > X (a half cannot exceed the game), fitted to the
1H marginal of X1 and the FG marginal of X. The prior is independent increments with the
**2H table's marginal** (``prior[x1, x] = p1[x1] p2[x - x1]``) when any 2H ladder was fitted,
so the coupling is data-driven; otherwise a discretised bivariate normal with correlation
``sd(X1) / sd(X)`` (the 2H table is then the FG-scaled prior, and the flags say so). Both period marginals are then exact by construction; the mass the IPF cannot
place (``halves_residual``) is the honest measure of 1H/FG ladder disagreement (nonzero only
when X1 is not stochastically below X). Scenarios: (H, A) from the FG table, H1 | H from the
(H1, H) table, A1 | (A, H1) from the product of the (A1, A) conditional and the 1H table's
A1 | H1 conditional (so the 1H table's own dependence is kept), which also enforces A1 <= A.
Overtime: FG ladders include it, so the FG table is the final score including OT and
"2H" = FG - 1H includes OT, matching both venues' 2H rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..sports.legspec import LegSpec
from .ladders_fit import ML_EXTRA_ERR, Bins, Obs, Pmf, bins_from_fit, fit_survival, norm_ppf, pmf_from_survival, split_tie_bucket
from .quotes import Fair

GRID = 81
_H_IDX, _A_IDX = np.meshgrid(np.arange(GRID), np.arange(GRID), indexing="ij")
M_IDX = (_H_IDX - _A_IDX + (GRID - 1)).ravel()     # 0 .. 160  <-> M = -80 .. 80
T_IDX = (_H_IDX + _A_IDX).ravel()                  # 0 .. 160
VAR_RANGE = {"M": (-(GRID - 1), GRID - 1), "T": (0, 2 * (GRID - 1)), "H": (0, GRID - 1), "A": (0, GRID - 1)}
FIT_FAMILIES = frozenset({"spread", "total", "team_total", "moneyline"})


@dataclass
class EngineConfig:
    margin_sd: float = 13.5
    total_sd: float = 10.5
    prior_rho_mt: float = 0.0
    tie_prob: float | None = 0.004
    k_flag: float = 3.0
    min_lines: int = 2
    thin_lines: int = 4             # team-total ladders with fewer lines are "thin"
    ipf_tol: float = 1e-6
    ipf_max_sweeps: int = 500
    ipf_omega: float = 1.6          # over-relaxed IPF (factor ** omega): the four targets are linearly dependent
                                    # (T = H + A, M = H - A), which makes plain IPF converge slowly; 1.6 halves the sweeps
    ipf_flag_l1: float = 0.02
    bin_floor_eps: float = 1e-4     # per integer: an IPF target bin is floored at eps x n_integers x the prior's mass in it,
                                    # so a flat PAVA block never makes an integer (an exact margin) structurally impossible
    halves_max_sweeps: int = 300
    sd_band: tuple[float, float] = (0.6, 1.6)   # a ladder sd outside this band x the default is clipped (and flagged)
    cov_cap: float = 0.9            # |corr(H, A)| of the prior is capped here (flagged when it binds)
    replica_scenarios: int = 10_000 # scenarios per bootstrap replica (interval only; the base model keeps n_scenarios)
    half_scale: float = 1 / math.sqrt(2)
    n_scenarios: int = 50_000
    halves_corr_shift: float = 0.0
    clamp_flag: float = 0.02
    halves_flag: float = 0.01        # halves IPF residual above this = 1H/FG ladders disagree
    halves_sd_ratio_flag: float = 0.80       # independent equal halves imply ~0.71


@dataclass(frozen=True)
class Leg:
    spec: LegSpec
    fair: Fair | None
    ref: str


def collect_observations(legs: list[Leg], period: str, *, rng: np.random.Generator | None = None
                         ) -> dict[str, list[Obs]]:
    """Orient legs of one period onto M / T / H / A survival observations. ``rng`` perturbs
    each p by N(0, err) (parametric bootstrap)."""
    out: dict[str, list[Obs]] = {"M": [], "T": [], "H": [], "A": []}
    for leg in legs:
        s, f = leg.spec, leg.fair
        if s.period != period or f is None or s.family not in FIT_FAMILIES:
            continue
        p = f.p if rng is None else float(np.clip(f.p + rng.normal(0, f.err), 0, 1))
        if s.family == "spread":
            assert s.line is not None and s.team is not None
            x_team = -s.line                      # P(team margin > x_team) for side == team
            p_cover = p if s.side == s.team else 1 - p
            if s.team == s.home:
                out["M"].append(Obs(x_team, p_cover, f.err, leg.ref))
            else:                                 # P(A - H > x) = P(M < -x) = 1 - P(M > -x)
                out["M"].append(Obs(-x_team, 1 - p_cover, f.err, leg.ref))
        elif s.family == "total":
            assert s.line is not None
            out["T"].append(Obs(s.line, p if s.side == "over" else 1 - p, f.err, leg.ref))
        elif s.family == "team_total":
            assert s.line is not None and s.team is not None
            var = "H" if s.team == s.home else "A"
            out[var].append(Obs(s.line, p if s.side == "over" else 1 - p, f.err, leg.ref))
        elif s.family == "moneyline":
            if s.side == s.home:
                out["M"].append(Obs(0.0, p, f.err + ML_EXTRA_ERR, leg.ref))
            elif s.side == s.away:
                out["M"].append(Obs(0.0, 1 - p, f.err + ML_EXTRA_ERR, leg.ref))
    return out


@dataclass
class PeriodModel:
    period: str
    table: np.ndarray                       # GRID x GRID, [h, a]
    pmfs: dict[str, Pmf]                    # fitted targets (subset of M T H A)
    prior_pmfs: dict[str, Pmf]              # prior-derived pmfs for missing variables
    means: dict[str, float]
    ipf_sweeps: int = 0
    ipf_change: float = 0.0
    ipf_residual: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    violations: list[tuple[str, float]] = field(default_factory=list)   # (leg ref, residual in err units)
    prior: np.ndarray | None = None          # the table IPF started from

    def marginal(self, var: str) -> np.ndarray:
        return table_marginal(self.table, var)

    def moments(self, var: str) -> tuple[float, float]:
        lo, _ = VAR_RANGE[var]
        p = self.marginal(var)
        k = np.arange(len(p)) + lo
        m = float(np.dot(k, p))
        return m, float(math.sqrt(max(np.dot((k - m) ** 2, p), 1e-9)))

    def corr(self, a: str, b: str) -> float:
        va = _var_values(a); vb = _var_values(b)
        w = self.table.ravel()
        ma, mb = np.dot(va, w), np.dot(vb, w)
        cov = np.dot((va - ma) * (vb - mb), w)
        return float(cov / math.sqrt(max(np.dot((va - ma) ** 2, w) * np.dot((vb - mb) ** 2, w), 1e-18)))

    def p_tie(self) -> float:
        return float(self.marginal("M")[GRID - 1])


def _var_values(var: str) -> np.ndarray:
    return {"H": _H_IDX.ravel(), "A": _A_IDX.ravel(), "M": M_IDX - (GRID - 1), "T": T_IDX}[var].astype(float)


def table_marginal(table: np.ndarray, var: str) -> np.ndarray:
    if var == "H":
        return table.sum(axis=1)
    if var == "A":
        return table.sum(axis=0)
    idx = M_IDX if var == "M" else T_IDX
    return np.bincount(idx, weights=table.ravel(), minlength=2 * GRID - 1)


DET_FLOOR = 1e-9
DET_FLOOR_CORR = 0.95      # |corr| the covariance is pulled back to when the determinant floor binds


def bivariate_normal_table(mu_h: float, mu_a: float, var_h: float, var_a: float, cov: float) -> tuple[np.ndarray, bool]:
    """Discretised bivariate normal on the grid; the flag says the determinant floor bound (|corr| -> 0.95)."""
    det = var_h * var_a - cov * cov
    clipped = det <= DET_FLOOR
    if clipped:
        cov = math.copysign(DET_FLOOR_CORR * math.sqrt(var_h * var_a), cov)
        det = var_h * var_a - cov * cov
    dh = _H_IDX - mu_h
    da = _A_IDX - mu_a
    q = (var_a * dh * dh - 2 * cov * dh * da + var_h * da * da) / det
    t = np.exp(-0.5 * q)
    return t / t.sum(), clipped


def prior_table(mu_m: float, mu_t: float, sd_m: float, sd_t: float, rho_mt: float) -> tuple[np.ndarray, bool]:
    cov_mt = rho_mt * sd_m * sd_t
    var_h = (sd_t ** 2 + sd_m ** 2 + 2 * cov_mt) / 4
    var_a = (sd_t ** 2 + sd_m ** 2 - 2 * cov_mt) / 4
    cov_ha = (sd_t ** 2 - sd_m ** 2) / 4
    return bivariate_normal_table((mu_t + mu_m) / 2, (mu_t - mu_m) / 2, var_h, var_a, cov_ha)


def ipf(table: np.ndarray, targets: dict[str, Bins], *, tol: float, max_sweeps: int, floor_eps: float = 0.0,
        omega: float = 1.0, check_every: int = 4) -> tuple[np.ndarray, int, float, dict[str, float]]:
    """Iterative proportional fitting of the (H, A) table to binned 1-D targets, applied in the
    dict's order (the caller orders them by ladder precision, least precise first, so the most
    precise ladder has the last word in every sweep).

    ``floor_eps``: every target bin is floored at ``floor_eps x n_integers x prior mass of the bin``
    (then renormalised), so a bin whose quoted survival steps are flat (PAVA pooled them to zero
    mass) keeps a trace of the prior instead of becoming a structural zero that IPF can never
    refill. ``omega`` > 1 is over-relaxation (factor ** omega); it drops back to 1 if the change
    stops shrinking. Returns (table, sweeps, last change, L1 residual per target against the
    floored masses); ``sweeps == max_sweeps`` means it did not converge.
    """
    t = table.ravel().copy()
    idx = {"H": _H_IDX.ravel(), "A": _A_IDX.ravel(), "M": M_IDX, "T": T_IDX}
    cell_bin = {var: b.bin_of[idx[var]] for var, b in targets.items()}
    mass: dict[str, np.ndarray] = {}
    for var, b in targets.items():
        m = b.mass.astype(float)
        if floor_eps > 0:
            n_int = np.bincount(b.bin_of, minlength=b.n_bins)
            prior_bin = np.bincount(cell_bin[var], weights=t, minlength=b.n_bins) / max(t.sum(), 1e-300)
            m = np.maximum(m, floor_eps * n_int * prior_bin)
        mass[var] = m / m.sum()
    change = float("inf")
    prev_change = float("inf")
    sweeps = 0
    last_check = 0
    before = t.copy()
    for sweeps in range(1, max_sweeps + 1):
        for var, b in targets.items():
            cur = np.bincount(cell_bin[var], weights=t, minlength=b.n_bins)
            factor = np.where(cur > 1e-300, mass[var] / np.maximum(cur, 1e-300), 0.0)
            if omega != 1.0:
                factor = factor ** omega
            t *= factor[cell_bin[var]]
        if sweeps % check_every == 0 or sweeps == max_sweeps:
            total = t.sum()
            if total <= 0:
                raise ValueError("IPF collapsed the table to zero mass")
            t /= total
            change = float(np.abs(t - before).sum()) / (sweeps - last_check)     # L1 change per sweep
            last_check = sweeps
            if change < tol:
                break
            if change > prev_change:        # relaxation is not helping (inconsistent targets): plain IPF from here
                omega = 1.0
            prev_change = change
            before = t.copy()
    t /= t.sum()
    resid = {var: float(np.abs(np.bincount(cell_bin[var], weights=t, minlength=b.n_bins) - mass[var]).sum())
             for var, b in targets.items()}
    return t.reshape(GRID, GRID), sweeps, change, resid


def ladder_precision(obs: list[Obs]) -> float:
    """Total information of a ladder: sum of 1 / err^2 over its observations."""
    return float(sum(1.0 / max(o.err, 1e-4) ** 2 for o in obs))


def build_period(legs: list[Leg], period: str, cfg: EngineConfig, *, rng: np.random.Generator | None = None,
                 fallback_means: dict[str, float] | None = None) -> PeriodModel:
    obs = collect_observations(legs, period, rng=rng)
    scale = 1.0 if period == "FG" else cfg.half_scale
    sd_m, sd_t = cfg.margin_sd * scale, cfg.total_sd * scale
    pmfs: dict[str, Pmf] = {}
    bins: dict[str, Bins] = {}
    notes: list[str] = []
    flags: list[str] = []
    violations: list[tuple[str, float]] = []
    for var in ("M", "T", "H", "A"):
        o = obs[var]
        n_lines = len({x.x for x in o if abs(x.x - round(x.x)) > 1e-9})
        if n_lines < cfg.min_lines:
            msg = f"{period} {var}: {n_lines} half-point line(s), using {'FG-scaled ' if period != 'FG' else ''}prior"
            (flags if (period != "FG" and var in ("M", "T")) else notes).append(msg)
            continue
        fit = fit_survival(o, k_flag=cfg.k_flag)
        for i in fit.flagged:
            violations.append((o[i].ref, float(fit.resid[i])))
        lo, hi = VAR_RANGE[var]
        default = sd_m if var == "M" else sd_t if var == "T" else sd_t / math.sqrt(2)
        pmfs[var] = pmf_from_survival(fit, lo, hi, tail_sd=default)
        bins[var] = bins_from_fit(fit, lo, hi)
        if var in ("H", "A") and n_lines < cfg.thin_lines:
            flags.append(f"{period} {var}: team-total ladder thin ({n_lines} lines)")
    if violations:
        flags.append(f"{period}: {len(violations)} ladder observation(s) beyond {cfg.k_flag} err from the monotone fit (down-weighted)")

    # prior: bivariate normal from the ladders' means and sds (defaults where no ladder)
    means: dict[str, float] = {v: p.mean for v, p in pmfs.items()}
    fb = fallback_means or {}
    for var in ("M", "T", "H", "A"):      # too few lines to fit a ladder, but a quote still locates the mean
        if var not in means and obs[var]:
            o = min(obs[var], key=lambda x: abs(x.p - 0.5))
            sd = sd_m if var == "M" else sd_t if var == "T" else sd_t / math.sqrt(2)
            means[var] = float(o.x + sd * norm_ppf(min(max(o.p, 1e-4), 1 - 1e-4)))
            notes.append(f"{period} {var}: mean {means[var]:+.2f} located from a single quote ({o.ref}) with the default sd")
    if "M" not in means:
        means["M"] = means["H"] - means["A"] if {"H", "A"} <= set(means) else fb.get("M", 0.0)
    if "T" not in means:
        means["T"] = means["H"] + means["A"] if {"H", "A"} <= set(means) else fb.get("T", 45.0 * scale * scale)
    lo_b, hi_b = cfg.sd_band
    sd_m_eff = float(np.clip(pmfs["M"].sd, lo_b * sd_m, hi_b * sd_m)) if "M" in pmfs else sd_m
    sd_t_eff = float(np.clip(pmfs["T"].sd, lo_b * sd_t, hi_b * sd_t)) if "T" in pmfs else sd_t
    for var, eff, raw in (("M", sd_m_eff, pmfs["M"].sd if "M" in pmfs else None), ("T", sd_t_eff, pmfs["T"].sd if "T" in pmfs else None)):
        if raw is not None and abs(eff - raw) > 1e-9:
            flags.append(f"{period} {var}: ladder sd {raw:.1f} outside the {lo_b:g}-{hi_b:g}x band of the default, clipped to {eff:.1f} for the prior")
    if {"H", "A"} <= set(pmfs):
        # team-total ladders fitted: prior from their own means/sds; Cov(H,A) = (Var T - Var M) / 4 from the M/T ladders
        var_h, var_a = pmfs["H"].sd ** 2, pmfs["A"].sd ** 2
        cov_raw = (sd_t_eff ** 2 - sd_m_eff ** 2) / 4
        cap = cfg.cov_cap * math.sqrt(var_h * var_a)
        cov_ha = float(np.clip(cov_raw, -cap, cap))
        if abs(cov_ha - cov_raw) > 1e-9:
            flags.append(f"{period}: prior corr(H,A) {cov_raw / math.sqrt(var_h * var_a):+.2f} capped at {cfg.cov_cap:+.2f}")
        prior, det_clipped = bivariate_normal_table(pmfs["H"].mean, pmfs["A"].mean, var_h, var_a, cov_ha)
    else:
        prior, det_clipped = prior_table(means["M"], means["T"], sd_m_eff, sd_t_eff, cfg.prior_rho_mt)
    if det_clipped:
        flags.append(f"{period}: prior covariance determinant floor bound (|corr| pulled to {DET_FLOOR_CORR})")
    if "M" in bins:
        bins["M"], note = split_tie_bucket(bins["M"], pmfs["M"].fit, table_marginal(prior, "M"),
                                           cfg.tie_prob if period == "FG" else None)
        if note:
            notes.append(f"{period} {note}")
    if not ({"H", "A"} & set(pmfs)):
        notes.append(f"{period}: no team-total ladders; margin-total coupling comes from the prior (rho_mt={cfg.prior_rho_mt:+.2f})")
    if bins:
        # least precise ladder first, most precise (normally the spread) last: it has the final word each sweep
        order = sorted(bins, key=lambda v: ladder_precision(obs[v]))
        table, sweeps, change, resid = ipf(prior, {v: bins[v] for v in order}, tol=cfg.ipf_tol, max_sweeps=cfg.ipf_max_sweeps,
                                           floor_eps=cfg.bin_floor_eps, omega=cfg.ipf_omega)
        if sweeps >= cfg.ipf_max_sweeps:
            flags.append(f"{period}: IPF did not converge in {cfg.ipf_max_sweeps} sweeps (last change {change:.1e})")
    else:
        table, sweeps, change, resid = prior, 0, 0.0, {}
        notes.append(f"{period}: no ladders at all; table is the prior")
    for v, r in resid.items():
        if r > cfg.ipf_flag_l1:
            flags.append(f"{period} {v}: IPF residual L1 {r:.3f} (targets inconsistent)")
    prior_pmfs = {}
    for var in ("M", "T", "H", "A"):
        if var not in pmfs:
            lo, hi = VAR_RANGE[var]
            prior_pmfs[var] = Pmf(support=np.arange(lo, hi + 1), p=table_marginal(table, var), notes=["from table"])
    return PeriodModel(period=period, table=table, pmfs=pmfs, prior_pmfs=prior_pmfs, means=means, ipf_sweeps=sweeps,
                       ipf_change=change, ipf_residual=resid, flags=flags, notes=notes, violations=violations, prior=prior)


@dataclass
class Scenarios:
    H1: np.ndarray
    A1: np.ndarray
    H: np.ndarray
    A: np.ndarray
    clamp_rate: float           # hard clamps (0 by construction with the IPF coupling)
    corr_h: float               # copula correlation used in the (H1, H) prior
    corr_a: float
    seed: int
    halves_residual: dict[str, float] = field(default_factory=dict)   # IPF mass not placed per component
    halves_sweeps: dict[str, int] = field(default_factory=dict)
    coupling: str = "copula"          # increments (2H ladders) | copula (prior)
    coupling_note: str = ""
    flags: list[str] = field(default_factory=list)   # clips that bound (halves corr) and halves IPF non-convergence

    @property
    def n(self) -> int:
        return int(self.H.shape[0])


def sample_table(table: np.ndarray, n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    cum = np.cumsum(table.ravel())
    cum /= cum[-1]
    flat = np.searchsorted(cum, rng.random(n), side="right")
    flat = np.minimum(flat, table.size - 1)
    return flat // GRID, flat % GRID


def _sample_rows(cond: np.ndarray, rows: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Draw one column index per scenario from cond[rows[i], :] (rows of cond sum to 1)."""
    cum = np.cumsum(cond[rows], axis=1)
    u = rng.random(rows.shape[0])[:, None] * cum[:, -1:]
    return np.minimum((cum < u).sum(axis=1), cond.shape[1] - 1)


HALVES_CORR_BAND = (0.05, 0.95)   # the Gaussian coupling correlation sd(X1)/sd(X) (+ shift) is clipped here


def halves_table(p1: np.ndarray, p: np.ndarray, m1: float, s1: float, m: float, sd: float, r: float,
                 *, tol: float, max_sweeps: int, p2: np.ndarray | None = None) -> tuple[np.ndarray, int, float]:
    """P(X1, X) on the grid with X1 <= X, fitted to the 1H marginal p1 and the FG marginal p.
    Prior: independent increments with the fitted 2H marginal, prior[x1, x] = p1[x1] p2[x - x1]
    (``p2`` given); otherwise a bivariate normal with correlation r (sd1/sd under independent
    increments). Returns (table[x1, x], sweeps, residual L1 = mass the IPF could not place)."""
    if p2 is not None:
        prior = np.zeros((GRID, GRID))
        for x1 in range(GRID):
            prior[x1, x1:] = p1[x1] * p2[: GRID - x1]
    else:
        r = float(np.clip(r, HALVES_CORR_BAND[0], HALVES_CORR_BAND[1]))
        prior, _ = bivariate_normal_table(m1, m, s1 * s1, sd * sd, r * s1 * sd)   # [x1, x]
        prior = prior * (_H_IDX <= _A_IDX)                                        # x1 <= x
    prior = prior * (p1[:, None] > 0) * (p[None, :] > 0)
    t = prior / max(prior.sum(), 1e-300)
    change = float("inf")
    sweeps = 0
    for sweeps in range(1, max_sweeps + 1):
        before = t.copy()
        rs = t.sum(axis=1)
        t *= np.where(rs > 1e-300, p1 / np.maximum(rs, 1e-300), 0.0)[:, None]
        cs = t.sum(axis=0)
        t *= np.where(cs > 1e-300, p / np.maximum(cs, 1e-300), 0.0)[None, :]
        tot = t.sum()
        if tot <= 0:
            raise ValueError("halves IPF collapsed")
        t /= tot
        change = float(np.abs(t - before).sum())
        if change < tol:
            break
    resid = float(max(np.abs(t.sum(axis=1) - p1).sum(), np.abs(t.sum(axis=0) - p).sum()))
    return t, sweeps, resid


def second_half_consistent(fg: PeriodModel, h1: PeriodModel, h2: PeriodModel, *, max_resid: float = 0.02,
                           mean_tol: float = 3.0, sd_ratio: float = 0.95) -> tuple[bool, str]:
    """Is the 2H table good enough to drive the halves coupling? Needs at least one fitted 2H
    ladder, small IPF residuals, 1H + 2H means within ``mean_tol`` points of the FG means, and a
    2H dispersion below the FG one (an increment cannot be wider than the whole)."""
    if not h2.pmfs:
        return False, "no 2H ladder fitted"
    worst = max(h2.ipf_residual.values(), default=0.0)
    if worst > max_resid:
        return False, f"2H IPF residual {worst:.3f} (targets inconsistent)"
    for var in ("H", "A"):
        m1, _ = h1.moments(var); m2, s2 = h2.moments(var); m, sd = fg.moments(var)
        if abs(m1 + m2 - m) > mean_tol:
            return False, f"1H + 2H mean {var} = {m1 + m2:.1f} vs FG {m:.1f}"
        if s2 > sd_ratio * sd:
            return False, f"2H {var} sd {s2:.1f} >= FG sd {sd:.1f}"
    return True, "2H ladders fitted and consistent"


def couple_periods(fg: PeriodModel, h1: PeriodModel, n: int, seed: int, *, corr_shift: float = 0.0,
                   tol: float = 1e-7, max_sweeps: int = 300, h2: PeriodModel | None = None,
                   mode: str = "auto") -> Scenarios:
    """mode: "increments" uses the 2H table's marginals as the coupling prior (data-driven when
    2H ladders were fitted), "copula" the Gaussian prior with corr sd1/sd (+ corr_shift),
    "auto" = increments when ``h2`` has at least one fitted ladder, else copula."""
    rng = np.random.default_rng(seed)
    H, A = sample_table(fg.table, n, rng)
    mH, sH = fg.moments("H"); mA, sA = fg.moments("A")
    mH1, sH1 = h1.moments("H"); mA1, sA1 = h1.moments("A")
    flags: list[str] = []
    lo_c, hi_c = HALVES_CORR_BAND
    r_h_raw, r_a_raw = sH1 / max(sH, 1e-6) + corr_shift, sA1 / max(sA, 1e-6) + corr_shift
    r_h = float(np.clip(r_h_raw, lo_c, hi_c))
    r_a = float(np.clip(r_a_raw, lo_c, hi_c))
    for comp, raw, used in (("H", r_h_raw, r_h), ("A", r_a_raw, r_a)):
        if abs(raw - used) > 1e-9:
            flags.append(f"halves {comp}: coupling corr {raw:.2f} clipped to {used:.2f}")
    if mode == "auto":
        ok, why = second_half_consistent(fg, h1, h2) if h2 is not None else (False, "no 2H table")
        mode = "increments" if ok else "copula"
    else:
        why = ""
    p2H = h2.marginal("H") if (mode == "increments" and h2 is not None) else None
    p2A = h2.marginal("A") if (mode == "increments" and h2 is not None) else None
    tH, swH, resH = halves_table(h1.marginal("H"), fg.marginal("H"), mH1, sH1, mH, sH, r_h, tol=tol, max_sweeps=max_sweeps, p2=p2H)
    tA, swA, resA = halves_table(h1.marginal("A"), fg.marginal("A"), mA1, sA1, mA, sA, r_a, tol=tol, max_sweeps=max_sweeps, p2=p2A)
    for comp, sw in (("H", swH), ("A", swA)):
        if sw >= max_sweeps:
            flags.append(f"halves {comp}: coupling IPF did not converge in {max_sweeps} sweeps")
    # conditionals: P(X1 | X) = column-normalised, as [x, x1]
    condH = (tH / np.maximum(tH.sum(axis=0, keepdims=True), 1e-300)).T
    condA = (tA / np.maximum(tA.sum(axis=0, keepdims=True), 1e-300)).T
    H1 = _sample_rows(condH, H, rng)
    # A1 | (A, H1) ∝ P(A1 | A) * P_1H(A1 | H1) / P_1H(A1)
    rows_1h = h1.table / np.maximum(h1.table.sum(axis=1, keepdims=True), 1e-300)     # [h1, a1]
    marg_a1 = np.maximum(h1.marginal("A"), 1e-300)
    w = condA[A] * (rows_1h[H1] / marg_a1[None, :])
    bad = w.sum(axis=1) <= 0
    if bad.any():                       # fall back to the (A1, A) conditional alone
        w[bad] = condA[A[bad]]
    w /= w.sum(axis=1, keepdims=True)
    cum = np.cumsum(w, axis=1)
    A1 = np.minimum((cum < rng.random(n)[:, None] * cum[:, -1:]).sum(axis=1), GRID - 1)
    clamped = (H1 > H) | (A1 > A)
    H1 = np.minimum(H1, H); A1 = np.minimum(A1, A)
    return Scenarios(H1=H1.astype(np.int16), A1=A1.astype(np.int16), H=H.astype(np.int16), A=A.astype(np.int16),
                     clamp_rate=float(clamped.mean()), corr_h=r_h, corr_a=r_a, seed=seed,
                     halves_residual={"H": resH, "A": resA}, halves_sweeps={"H": swH, "A": swA}, coupling=mode,
                     coupling_note=why, flags=flags)
