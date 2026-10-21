"""Ladder -> discrete distribution.

A ladder is a set of observations ``P(V > x_i) = p_i +/- err_i`` of one integer-valued score
variable V (home margin, total, home points, away points) at half-point lines x_i (and at 0
for a moneyline, see below). Steps:

1. ``fit_survival``: weighted isotonic regression (pool-adjacent-violators, weights 1/err^2)
   of the survival function, non-increasing in x, clipped to [0, 1]. Observations further than
   ``k_flag * err`` from the monotone fit are flagged one at a time (worst first), refit with
   weight x ``downweight``; they are never dropped silently.
2. ``bins_from_fit``: the IPF targets. Between consecutive quoted half-point lines the mass
   ``S(x_i) - S(x_{i+1})`` is pinned as one bin (a single integer where lines are dense, e.g.
   (1.5, 2.5]; several where they are not, e.g. (10.5, 13.5] or totals listed every two
   points); the two tails ``1 - S(x_0)`` and ``S(x_last)`` are one bin each. Nothing inside a
   bin is invented: the prior's shape fills it. This is what makes the engine reproduce every
   quoted line exactly and keeps the margin/total parity constraint feasible.
3. ``pmf_from_survival``: a smooth pmf for diagnostics and for the prior's mean / sd. The
   survival is interpolated at every half-point in normal-quantile space (z = Phi^-1(S) is
   linear in x for a normal, so gaps get a bell-shaped split, not a flat one) and extended
   outside the ladder by a normal tail whose scale is the ladder's own z-slope over the outer
   lines (clipped to [0.5, 2] x the default scale) anchored at the outermost line.
4. Moneyline observations: a Polymarket moneyline pays 0.5 on a tie, so its fair probability
   is ``s_ml = P(M > 0) + 0.5 P(M = 0)``. It enters the isotonic fit as an observation at x = 0
   (``err + ML_EXTRA_ERR``) so the monotone fit sees it, but 0 is not a half-point so it is not
   a bin edge. Spread ladders start at 1.5, so the {-1, 0, +1} bucket is split by
   ``split_tie_bucket``: P(M = 0) is ``tie_prob`` when given (full game: 0.4%, the post-2017
   NFL rate; overtime makes ties structurally rare) or the prior's share of the bucket
   otherwise (halves, where ties are common); then P(M = 1) = s_ml - 0.5 P(M = 0) - S(1.5) and
   P(M = -1) takes the rest, clipped at zero. Engine choices, documented, not data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

ML_EXTRA_ERR = 0.005


@dataclass(frozen=True)
class Obs:
    x: float          # line; P(V > x)
    p: float
    err: float
    ref: str = ""     # caller's handle (position_id / slug|outcome)


@dataclass
class SurvivalFit:
    xs: np.ndarray          # distinct lines, ascending
    s: np.ndarray           # fitted survival at xs
    obs: list[Obs]
    resid: np.ndarray       # per observation: (p - fit) / err
    flagged: list[int]      # indices into obs with |resid| > k_flag
    n_lines: int

    def at(self, x: float) -> float:
        """Fitted survival at a line inside the ladder (z-space interpolation)."""
        return float(interp_survival(self.xs, self.s, np.array([x]))[0])


def pava_decreasing(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted isotonic regression, non-increasing. Returns fitted values (same length)."""
    # fit non-decreasing on -y then negate
    vals = list(-y.astype(float))
    wts = list(w.astype(float))
    blocks: list[tuple[float, float, int]] = []  # (mean, weight, count)
    for v, wt in zip(vals, wts):
        blocks.append((v, wt, 1))
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            m2, w2, c2 = blocks.pop()
            m1, w1, c1 = blocks.pop()
            wt_ = w1 + w2
            blocks.append(((m1 * w1 + m2 * w2) / wt_, wt_, c1 + c2))
    out = np.empty(len(vals))
    i = 0
    for m, _, c in blocks:
        out[i:i + c] = -m
        i += c
    return out


def fit_survival(obs: list[Obs], *, k_flag: float = 3.0, downweight: float = 0.01) -> SurvivalFit:
    if not obs:
        raise ValueError("empty ladder")
    xs_all = np.array([o.x for o in obs])
    ps = np.clip(np.array([o.p for o in obs]), 0.0, 1.0)
    errs = np.array([max(o.err, 1e-4) for o in obs])
    order = np.argsort(xs_all, kind="stable")
    w = 1.0 / errs ** 2

    def run(weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        fit_sorted = pava_decreasing(ps[order], weights[order])
        fit = np.empty_like(fit_sorted)
        fit[order] = fit_sorted
        return np.clip(fit, 0.0, 1.0), (ps - fit) / errs

    # flag one observation at a time (the worst), down-weight it and refit, so a single gross
    # violation does not drag its pooled neighbours into the flag set
    w2 = w.copy()
    flagged: list[int] = []
    for _ in range(len(obs)):
        fit, resid = run(w2)
        cand = [int(i) for i in np.argsort(-np.abs(resid)) if abs(resid[i]) > k_flag and int(i) not in flagged]
        if not cand:
            break
        flagged.append(cand[0])
        w2[cand[0]] *= downweight
    flagged.sort()
    xs = np.unique(xs_all)
    s = np.array([np.average(fit[xs_all == x]) for x in xs])
    s = np.minimum.accumulate(np.clip(s, 0, 1))
    return SurvivalFit(xs=xs, s=s, obs=list(obs), resid=resid, flagged=flagged, n_lines=len(xs))


# --- normal helpers (no scipy) ---------------------------------------------------------------
def norm_cdf(z):
    z = np.asarray(z, dtype=float)
    return 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))


def norm_sf(z):
    return 1.0 - norm_cdf(z)


_erf = np.vectorize(math.erf)


def norm_ppf(p):
    """Acklam's rational approximation with one Newton refinement; vectorized."""
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1 - 1e-12)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    x = np.empty_like(p)
    lo = p < plow
    hi = p > phigh
    mid = ~(lo | hi)
    q = np.sqrt(-2 * np.log(p[lo]))
    x[lo] = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = np.sqrt(-2 * np.log(1 - p[hi]))
    x[hi] = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p[mid] - 0.5
    r = q * q
    x[mid] = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    # Newton step
    e = norm_cdf(x) - p
    u = e * math.sqrt(2 * math.pi) * np.exp(x * x / 2)
    x = x - u / (1 + x * u / 2)
    return x


def interp_survival(xs: np.ndarray, s: np.ndarray, at: np.ndarray) -> np.ndarray:
    """Interpolate a survival curve in normal-quantile space (linear in z). Inside [xs[0], xs[-1]] only."""
    eps = 1e-6
    z = norm_ppf(np.clip(s, eps, 1 - eps))
    # ppf is decreasing in x when s decreases; np.interp needs increasing xs (they are)
    zi = np.interp(at, xs, z)
    return np.clip(norm_cdf(zi), 0.0, 1.0)


@dataclass
class Pmf:
    support: np.ndarray     # integers, ascending
    p: np.ndarray           # probabilities, sums to 1
    fit: SurvivalFit | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return float(np.dot(self.support, self.p))

    @property
    def sd(self) -> float:
        m = self.mean
        return float(math.sqrt(max(np.dot((self.support - m) ** 2, self.p), 1e-9)))

    def survival(self, x: float) -> float:
        return float(self.p[self.support > x].sum())


def _half_lines(fit: SurvivalFit) -> tuple[np.ndarray, np.ndarray]:
    """The fit restricted to half-point lines (drops a moneyline observation at 0)."""
    keep = np.abs(fit.xs - np.round(fit.xs)) > 1e-9
    return fit.xs[keep], fit.s[keep]


def tail_scales(fit: SurvivalFit, default_sd: float, *, n_outer: int = 3, clip: tuple[float, float] = (0.5, 2.0)
                ) -> tuple[float, float]:
    """(lower, upper) normal tail scales from the ladder's z-slope over its outer lines."""
    xs, s = _half_lines(fit)
    eps = 1e-6
    z = norm_ppf(np.clip(s, eps, 1 - eps))
    lo_c, hi_c = clip[0] * default_sd, clip[1] * default_sd

    def slope(x: np.ndarray, zz: np.ndarray) -> float:
        dz = zz[:-1] - zz[1:]
        dx = x[1:] - x[:-1]
        ok = dz > 1e-6
        if ok.sum() == 0:
            return default_sd
        return float(np.clip(np.median(dx[ok] / dz[ok]), lo_c, hi_c))

    if len(xs) < 3:
        return default_sd, default_sd
    k = min(n_outer + 1, len(xs))
    return slope(xs[:k], z[:k]), slope(xs[-k:], z[-k:])


def pmf_from_survival(fit: SurvivalFit, lo: int, hi: int, *, tail_sd: float) -> Pmf:
    """Smooth integer pmf on [lo, hi]: interior half-points by z-interpolation, exterior by
    normal tails scaled from the ladder's own slope (``tail_scales``) anchored at the outer lines."""
    support = np.arange(lo, hi + 1)
    half = np.arange(lo - 0.5, hi + 1.0, 1.0)      # lo-0.5 ... hi+0.5
    xs, sv = _half_lines(fit)
    if len(xs) == 0:
        raise ValueError("no half-point lines in ladder")
    x0, x1 = float(xs[0]), float(xs[-1])
    s0, s1 = float(np.clip(sv[0], 1e-6, 1 - 1e-6)), float(np.clip(sv[-1], 1e-6, 1 - 1e-6))
    sd_lo, sd_hi = tail_scales(fit, tail_sd)
    S = np.empty_like(half)
    inside = (half >= x0) & (half <= x1)
    S[inside] = interp_survival(xs, sv, half[inside])
    mu_hi = x1 + sd_hi * float(norm_ppf(s1))     # sf((x1 - mu)/sd) = s1
    S[half > x1] = norm_sf((half[half > x1] - mu_hi) / sd_hi)
    mu_lo = x0 + sd_lo * float(norm_ppf(s0))
    S[half < x0] = norm_sf((half[half < x0] - mu_lo) / sd_lo)
    S = np.minimum.accumulate(np.clip(S, 0, 1))
    p = np.clip(S[:-1] - S[1:], 0, None)
    total = p.sum()
    if total <= 0:
        raise ValueError("degenerate pmf")
    return Pmf(support=support, p=p / total, fit=fit, notes=[f"tail sd lo={sd_lo:.1f} hi={sd_hi:.1f}"])


@dataclass
class Bins:
    """IPF target: integer support split into bins with pinned masses."""
    support: np.ndarray        # integers lo..hi
    bin_of: np.ndarray         # bin index per support value
    mass: np.ndarray           # per bin, sums to 1
    edges: list[float]         # half-point lines used as edges (ascending)

    @property
    def n_bins(self) -> int:
        return int(self.mass.shape[0])


def bins_from_fit(fit: SurvivalFit, lo: int, hi: int) -> Bins:
    xs, sv = _half_lines(fit)
    xs = [float(x) for x in xs if lo - 0.5 < x < hi + 0.5]
    sv = [float(v) for x, v in zip(_half_lines(fit)[0], sv) if lo - 0.5 < x < hi + 0.5]
    if not xs:
        raise ValueError("no half-point lines inside the support")
    support = np.arange(lo, hi + 1)
    bin_of = np.searchsorted(np.array(xs), support, side="left")   # value k goes to bin #(lines below k)
    mass = np.empty(len(xs) + 1)
    mass[0] = 1 - sv[0]
    for i in range(1, len(xs)):
        mass[i] = sv[i - 1] - sv[i]
    mass[-1] = sv[-1]
    mass = np.clip(mass, 0, None)
    mass /= mass.sum()
    return Bins(support=support, bin_of=bin_of, mass=mass, edges=xs)


def split_tie_bucket(bins: Bins, fit: SurvivalFit, prior_pmf: np.ndarray, tie_prob: float | None) -> tuple[Bins, str | None]:
    """Split the (-1.5, 1.5] bin of a margin ladder into {-1}, {0}, {+1} using the moneyline
    observation at 0 (if any) and ``tie_prob`` / the prior's share for P(M = 0). Returns the
    new bins and a note, or the input unchanged when the bucket is not exactly {-1, 0, 1}."""
    if -1.5 not in bins.edges or 1.5 not in bins.edges:
        return bins, None
    b = int(bins.bin_of[np.where(bins.support == 0)[0][0]])
    bucket = float(bins.mass[b])
    s15 = float(bins.mass[b + 1:].sum())            # S(1.5)
    sel = (bins.support >= -1) & (bins.support <= 1)
    prior_share = prior_pmf[sel] / max(prior_pmf[sel].sum(), 1e-12)
    p0 = min(tie_prob, bucket) if tie_prob is not None else bucket * float(prior_share[1])
    has_ml = bool(np.any(np.abs(fit.xs) < 1e-9))
    if has_ml:
        s_ml = float(fit.s[np.argmin(np.abs(fit.xs))])
        p1 = float(np.clip(s_ml - 0.5 * p0 - s15, 0, bucket - p0))
        src = "moneyline"
    else:
        p1 = (bucket - p0) * float(prior_share[2] / max(prior_share[0] + prior_share[2], 1e-12))
        src = "prior"
    pm1 = max(bucket - p0 - p1, 0.0)
    new_bin = bins.bin_of.copy()
    new_bin[bins.support >= 0] += 1
    new_bin[bins.support >= 1] += 1
    mass = np.concatenate([bins.mass[:b], [pm1, p0, p1], bins.mass[b + 1:]])
    mass = np.clip(mass, 0, None); mass /= mass.sum()
    note = (f"tie bucket: P(M=0)={p0:.4f} ({'tie_prob' if tie_prob is not None else 'prior share'}), "
            f"P(M=1)={p1:.4f} from {src}, P(M=-1)={pm1:.4f}")
    return Bins(support=bins.support, bin_of=new_bin, mass=mass, edges=bins.edges), note

