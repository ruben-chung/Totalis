import math
import random

import pytest

from rfqbot.edge.curse import Phi, adverse_move, estimate, lam, model_curse, phi


def test_lambda_matches_numeric_and_is_stable_in_the_tails():
    for z in (-3.0, -1.0, 0.0, 0.5, 2.0):
        assert lam(z) == pytest.approx(phi(z) / Phi(z), rel=1e-12)
    assert lam(0.0) == pytest.approx(math.sqrt(2 / math.pi), rel=1e-12)
    assert lam(-40.0) == pytest.approx(40.0, rel=1e-2)       # asymptotically -z
    assert lam(8.0) < 1e-12
    assert model_curse(0.0, 0.01, 0.005) == 0.0


def test_curse_against_a_simulated_first_price_auction():
    """Known sigma and competitor gap: the mean error among our wins must match -sigma phi(z)/Phi(z)."""
    rng = random.Random(7)
    sigma, delta = 0.01, 0.02
    for m in (0.0, 0.01, 0.02, 0.03):
        errs = []
        for _ in range(200_000):
            e = rng.gauss(0.0, sigma)             # p_fair = truth + e
            if e + m <= delta:                     # we win when our ask (truth + e + m) <= competitor (truth + delta)
                errs.append(e)
        z = (delta - m) / sigma
        assert len(errs) / 200_000 == pytest.approx(Phi(z), abs=0.01)
        measured = -sum(errs) / len(errs)          # maker loss: we sold YES below its value
        assert measured == pytest.approx(model_curse(sigma, delta, m), abs=0.0006)


def test_curse_grows_as_we_undercut_less():
    # the closer m sits to the competitor, the more selected our wins are
    cs = [model_curse(0.01, 0.02, m) for m in (0.0, 0.01, 0.02, 0.03)]
    assert cs == sorted(cs)


def test_adverse_move_sign_per_direction():
    assert adverse_move("BUY", 0.30, 0.32) == pytest.approx(0.02)     # we sold at ~0.30, now worth 0.32: loss
    assert adverse_move("SELL", 0.60, 0.58) == pytest.approx(0.02)    # we bought at ~0.60, now worth 0.58: loss
    assert adverse_move("SELL", 0.60, 0.63) < 0


def test_estimate_measured_curse_by_grid_and_undercut():
    grid = [0.0, 0.01, 0.02]
    # (delta, adverse move, undercut at actual quote)
    obs = [(0.015, 0.004, 0.005), (0.025, 0.006, 0.015), (0.005, None, None), (-0.01, 0.0, None)]
    e = estimate("nba|2|BUY|disjoint|s1", 30, "model", grid, sigma_mean=0.006, delta_mean=0.01, n_printed=4, obs=obs)
    assert e.n_with_later == 3
    assert e.measured_n == [2, 2, 1]                       # delta >= m (the -0.01 print is never winnable)
    assert e.measured[0] == pytest.approx((0.004 + 0.006) / 2)
    assert e.measured[2] == pytest.approx(0.006)
    assert e.model[1] == pytest.approx(model_curse(0.006, 0.01, 0.01))
    bins = {(b["lo"], b["hi"]): b for b in e.by_undercut}
    assert bins[(0.005, 0.01)]["n"] == 1 and bins[(0.005, 0.01)]["mean_move"] == pytest.approx(0.004)
    assert bins[(0.01, 0.02)]["n"] == 1
    d = e.as_dict()
    assert d["segment"] == "nba|2|BUY|disjoint|s1" and d["horizon_min"] == 30 and len(d["measured"]) == 3
