"""Winner's curse on our own estimation error.

Model (brief): our fair is p_fair = truth + e, e ~ N(0, sigma^2) with sigma = u_m + u_d; the best
competitor quotes truth + delta (delta = its gap over the truth in our win direction). We quote
truth + e + m and win iff e <= delta - m, so
    E[e | win] = -sigma * phi(z) / Phi(z),   z = (delta - m) / sigma.
In maker terms the conditional error is a loss of the same size for both directions (BUY: we sold
YES below its value; SELL: we bought above), so ``model_curse`` returns the positive expected loss
``sigma * lambda(z)``, lambda = phi/Phi, and the net markup given a win is ``m - curse``.

Unconditionally E[print - p_fair] = delta over printed decisions (E[e] = 0), so ``delta`` is fitted
as the mean competitor gap of the node's printed decisions. The measured curse is the mean
adverse move of the later fair (model / Polymarket / Kalshi reference) among the decisions that
would have won at m, i.e. the empirical E[-e | win at m] with the later fair standing in for truth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

SQRT2 = math.sqrt(2.0)


def phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def Phi(z: float) -> float:
    return 0.5 * math.erfc(-z / SQRT2)


def lam(z: float) -> float:
    """Inverse Mills ratio phi(z)/Phi(z); asymptotically -z for z << 0."""
    if z < -30:
        return -z + 1.0 / (-z) if z != 0 else 0.0
    p = Phi(z)
    if p <= 0:
        return -z
    return phi(z) / p


def model_curse(sigma: float, delta: float, m: float) -> float:
    """Expected adverse error (maker loss, >= 0) given a win at markup m."""
    if sigma <= 0:
        return 0.0
    return sigma * lam((delta - m) / sigma)


def adverse_move(direction: str, p_fair: float, later_fair: float) -> float:
    """Loss in maker terms when the fair moves from p_fair to later_fair after we fill."""
    return (later_fair - p_fair) if direction == "BUY" else (p_fair - later_fair)


@dataclass
class CurseEstimate:
    segment: str
    horizon_min: int
    ref: str                                  # model | poly | kalshi
    sigma_mean: float
    delta_mean: float | None
    n_printed: int
    n_with_later: int
    grid: list[float]
    model: list[float]                        # model curse at each grid m (sigma_mean, delta_mean)
    measured: list[float | None]              # mean adverse move among would-win-at-m decisions
    measured_n: list[int]
    by_undercut: list[dict] = field(default_factory=list)   # at the actual quote: mean move per undercut bin

    def as_dict(self) -> dict:
        return {"segment": self.segment, "horizon_min": self.horizon_min, "ref": self.ref,
                "sigma_mean": round(self.sigma_mean, 5),
                "delta_mean": None if self.delta_mean is None else round(self.delta_mean, 5),
                "n_printed": self.n_printed, "n_with_later": self.n_with_later, "grid": self.grid,
                "model": [round(x, 5) for x in self.model],
                "measured": [None if x is None else round(x, 5) for x in self.measured],
                "measured_n": self.measured_n, "by_undercut": self.by_undercut}


UNDERCUT_BINS = ((0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 10.0))


def estimate(segment: str, horizon_min: int, ref: str, grid: Sequence[float], sigma_mean: float,
             delta_mean: float | None, n_printed: int,
             obs: Sequence[tuple[float, float | None, float | None]]) -> CurseEstimate:
    """``obs``: per printed decision (delta, adverse move of the later fair or None, undercut at the
    actual quote or None)."""
    model = [model_curse(sigma_mean, delta_mean, m) if delta_mean is not None else 0.0 for m in grid]
    measured: list[float | None] = []
    measured_n: list[int] = []
    with_later = [(d, mv, u) for d, mv, u in obs if mv is not None]
    for m in grid:
        hits = [mv for d, mv, _ in with_later if m <= d + 1e-12]
        measured.append(sum(hits) / len(hits) if hits else None)
        measured_n.append(len(hits))
    by_u: list[dict] = []
    for lo, hi in UNDERCUT_BINS:
        hits = [mv for _, mv, u in with_later if u is not None and lo <= u < hi]
        by_u.append({"lo": lo, "hi": hi, "n": len(hits), "mean_move": (sum(hits) / len(hits)) if hits else None})
    return CurseEstimate(segment=segment, horizon_min=horizon_min, ref=ref, sigma_mean=sigma_mean, delta_mean=delta_mean,
                         n_printed=n_printed, n_with_later=len(with_later), grid=list(grid), model=model,
                         measured=measured, measured_n=measured_n, by_undercut=by_u)
