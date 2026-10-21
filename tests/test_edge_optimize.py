import pytest

from rfqbot.edge.config import EdgeConfig
from rfqbot.edge.curse import lam, model_curse
from rfqbot.edge.fillcurve import FillCurve
from rfqbot.edge.optimize import optimize_node, profit_curve
from rfqbot.edge.segments import Segment

GRID = [round(0.0025 * i, 6) for i in range(41)]   # 0 .. 0.10


def curve(f, *, n=100, n_printed=50, sigma=0.006, delta=0.03, seg="nba|2|BUY|disjoint|s1"):
    return FillCurve(segment=Segment.parse(seg), grid=GRID, f=f, f_print=f, n=n, n_printed=n_printed, n_win_actual=10,
                     fill_frac_mean=1.0, sigma_mean=sigma, margin_mean=0.01, delta_mean=delta, delta_sd=0.01, delta_median=delta)


def test_optimum_on_a_synthetic_curve_with_a_known_answer():
    # competitor always at delta = 3pt: F(m) = 1 for m <= 3pt, 0 beyond; with no curse m* = 3pt exactly
    f = [1.0 if m <= 0.03 + 1e-12 else 0.0 for m in GRID]
    cfg = EdgeConfig(curse_source="model", min_fill_rate=0.0, default_edge=0.005)
    c = curve(f, sigma=1e-6, delta=0.03)
    r = optimize_node(c, cfg)
    assert r.m_star == pytest.approx(0.03) and r.f_star == 1.0 and r.source == "model" and not r.constraint_hit
    assert r.profit_fill == pytest.approx(0.03, abs=1e-6) and r.profit_req == pytest.approx(0.03, abs=1e-6)
    # with a real sigma the curse at m = delta is sigma * lambda(0) ~ 0.8 sigma: still worth taking (3pt >> 0.5pt)
    r = optimize_node(curve(f, sigma=0.006, delta=0.03), cfg)
    assert r.m_star == pytest.approx(0.03)
    assert r.curse_star == pytest.approx(0.006 * lam(0.0))
    assert r.curse_mult == pytest.approx(lam(0.0)) and r.base == pytest.approx(0.03 - 0.006 * lam(0.0))
    # linear fill curve F(m) = 1 - m / 0.08: without curse the optimum of m (1 - m/0.08) is m = 4pt
    f = [max(0.0, 1 - m / 0.08) for m in GRID]
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), cfg)
    assert r.m_star == pytest.approx(0.04)


def test_curse_lowers_the_optimum_and_profit():
    f = [max(0.0, 1 - m / 0.08) for m in GRID]
    cfg = EdgeConfig(curse_source="model")
    r0 = optimize_node(curve(f, sigma=1e-6, delta=0.08), cfg)
    r1 = optimize_node(curve(f, sigma=0.02, delta=0.08), cfg)
    assert r1.m_star <= r0.m_star and r1.profit_req < r0.profit_req
    pts = profit_curve(curve(f, sigma=0.02, delta=0.08), 0.02, 0.08)
    m, F, c, prof = pts[8]
    assert c == pytest.approx(model_curse(0.02, 0.08, m)) and prof == pytest.approx(F * (m - c))


def test_min_fill_rate_constraint_and_infeasible_case():
    f = [max(0.0, 1 - m / 0.08) for m in GRID]
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), EdgeConfig(min_fill_rate=0.8))
    assert r.m_star == pytest.approx(0.015) and r.constraint_hit            # F(0.015) = 0.8125 is the last feasible point
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), EdgeConfig(min_fill_rate=0.999))
    assert r.m_star == 0.0 and r.constraint_hit                            # infeasible: the most-filled point


def test_measured_curse_source_and_fallbacks():
    f = [max(0.0, 1 - m / 0.08) for m in GRID]
    measured = [0.03] * len(GRID)                   # a flat measured 3pt loss on every win
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), EdgeConfig(curse_source="measured"), measured=measured)
    assert r.source == "measured" and r.m_star == pytest.approx(0.055)       # argmax (1 - m/.08)(m - .03) -> m = 5.5pt
    # no prints at the node: the objective is flat, the default edge is returned and flagged
    r = optimize_node(curve(f, n_printed=0, delta=None), EdgeConfig(default_edge=0.007))
    assert r.source == "no_prints" and r.m_star == 0.007 and r.curse_mult == 0.0
    r = optimize_node(curve(f, n=0, n_printed=0, delta=None), EdgeConfig())
    assert r.source == "no_data"
    # info premium is carried through and clamped at zero
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), EdgeConfig(), info_premium=-0.01)
    assert r.info_premium == 0.0
    r = optimize_node(curve(f, sigma=1e-6, delta=0.08), EdgeConfig(), info_premium=0.004)
    assert r.info_premium == 0.004 and r.as_dict()["info_premium"] == 0.004
