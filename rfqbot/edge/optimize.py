"""Per-node margin optimiser.

    m* = argmax_m  F(m) * (m - curse(m))      s.t.  F_basis(m) >= min_fill_rate
F(m) is the per-request fill curve (RFQs that never trade count as unfilled, ~99% of them), so the
policy bound is applied by default to F_print(m), the fill rate among RFQs that did trade
(``min_fill_basis = print``); ``request`` applies it to F(m) itself.
curse(m) is the model E[loss | win at m] = sigma * lambda((delta - m) / sigma) with the node's mean
sigma and fitted delta (``curse_source = model``), or the measured mean adverse move among the
would-have-won decisions at m (``measured``; falls back to the model where unmeasured). F(m) is
the node's pooled empirical fill curve on the markup grid.

The result is handed to quoting as MarginTerms(base, curse_mult, info_premium, wallet_premium,
blocked) with the quote-time rule
    margin = base + curse_mult * sigma_rfq + info_premium + wallet_premium,
curse_mult = lambda(z*) (the marginal expected loss per unit of sigma at the optimum) and
base = m* - curse_mult * sigma_node, so an RFQ with the node's sigma gets exactly m* and one with a
larger sigma pays its extra expected curse. info_premium = max(0, -mean 30-min markout) for the
node's direction and segment (pooled the same way) is the direction-asymmetric informational
charge measured on the prints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from .config import EdgeConfig
from .curse import lam, model_curse
from .fillcurve import FillCurve


@dataclass
class NodeResult:
    segment: str
    level: int
    n: int
    n_printed: int
    sigma: float
    delta: float | None
    m_star: float
    f_star: float
    curse_star: float
    z_star: float | None
    curse_mult: float
    base: float
    profit_fill: float          # m* - curse(m*)
    profit_req: float           # F(m*) * profit_fill
    info_premium: float
    constraint_hit: bool        # min_fill_rate bound was active or infeasible
    source: str                 # model | measured | no_prints | no_data

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("sigma", "delta", "m_star", "f_star", "curse_star", "z_star", "curse_mult", "base", "profit_fill", "profit_req",
                  "info_premium"):
            if d[k] is not None:
                d[k] = round(d[k], 6)
        return d


def profit_curve(curve: FillCurve, sigma: float, delta: float | None, *, measured: Sequence[float | None] | None = None,
                 use_measured: bool = False) -> list[tuple[float, float, float, float]]:
    """[(m, F(m), curse(m), F(m) * (m - curse(m)))] on the curve's grid."""
    out = []
    for i, m in enumerate(curve.grid):
        c_model = model_curse(sigma, delta, m) if delta is not None else 0.0
        c = c_model
        if use_measured and measured is not None and i < len(measured) and measured[i] is not None:
            c = max(0.0, float(measured[i]))
        f = curve.f[i]
        out.append((m, f, c, f * (m - c)))
    return out


def optimize_node(curve: FillCurve, cfg: EdgeConfig, *, info_premium: float = 0.0,
                  measured: Sequence[float | None] | None = None) -> NodeResult:
    sigma = max(curve.sigma_mean, 1e-6)
    delta = curve.delta_mean
    level = curve.segment.level
    if curve.n == 0:
        return NodeResult(curve.key, level, 0, 0, sigma, None, cfg.default_edge, 0.0, 0.0, None, 0.0, cfg.default_edge,
                          cfg.default_edge, 0.0, info_premium, False, "no_data")
    if curve.n_printed < cfg.min_printed or delta is None:
        # nothing ever printed against these quotes: F == 0 everywhere, the objective is flat
        return NodeResult(curve.key, level, curve.n, curve.n_printed, sigma, delta, cfg.default_edge, 0.0, 0.0, None, 0.0,
                          cfg.default_edge, cfg.default_edge, 0.0, info_premium, False, "no_prints")
    use_meas = cfg.curse_source == "measured" and measured is not None
    pts = profit_curve(curve, sigma, delta, measured=measured, use_measured=use_meas)
    basis = curve.f if cfg.min_fill_basis == "request" else curve.f_print
    ok = [b >= cfg.min_fill_rate - 1e-12 for b in basis]
    feasible = [p for p, good in zip(pts, ok) if good]
    hit = False
    if not feasible:
        # infeasible: the most-filled point is the best we can do
        best = max(zip(pts, basis), key=lambda pb: (pb[1], pb[0][3]))[0]
        hit = True
    else:
        best = max(feasible, key=lambda p: p[3])
        # the bound is active when the unconstrained optimum would sit outside the feasible set
        unc = max(range(len(pts)), key=lambda i: pts[i][3])
        hit = not ok[unc]
    m_star, f_star, c_star, prof = best
    z_star = (delta - m_star) / sigma
    curse_mult = lam(z_star)
    base = max(0.0, m_star - curse_mult * sigma)
    return NodeResult(segment=curve.key, level=level, n=curve.n, n_printed=curve.n_printed, sigma=sigma, delta=delta,
                      m_star=m_star, f_star=f_star, curse_star=c_star, z_star=z_star, curse_mult=curse_mult, base=base,
                      profit_fill=m_star - c_star, profit_req=prof, info_premium=max(0.0, info_premium), constraint_hit=hit,
                      source="measured" if use_meas else "model")
