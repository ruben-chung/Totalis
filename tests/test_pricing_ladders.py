import numpy as np
import pytest

from rfqbot.pricing.ladders_fit import (Obs, bins_from_fit, fit_survival, norm_cdf, norm_ppf, pava_decreasing,
                                        pmf_from_survival, split_tie_bucket, tail_scales)
from rfqbot.pricing.quotes import Quote, fair_from_quote


def test_fair_rule():
    f = fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, 0.48, 0.52, 500, 500))
    assert abs(f.p - 0.5) < 1e-9 and 0.02 < f.err < 0.03
    thin = fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, 0.48, 0.52, 0, 0))
    assert thin.err > f.err
    assert fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, None, 0.52)) is None
    ph = fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, 0.5, 0.5))          # sizeless 0.50: placeholder
    assert ph.p == 0.5 and ph.err == 0.25
    assert fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, 0.5, 0.5, 10, 10)).err < 0.05   # sized 0.50 is a price
    assert fair_from_quote(Quote("1", "t", "s", "t", "Over", 0, 0.6, 0.5)) is None  # crossed


def test_pava_and_normal_helpers():
    y = np.array([0.9, 0.95, 0.7, 0.75, 0.3])
    f = pava_decreasing(y, np.ones(5))
    assert np.all(np.diff(f) <= 1e-12) and abs(f[0] - 0.925) < 1e-9 and abs(f[2] - 0.725) < 1e-9
    z = np.array([-2.0, -0.5, 0.0, 0.7, 2.5])
    assert np.allclose(norm_ppf(norm_cdf(z)), z, atol=1e-6)


def normal_ladder(mu, sd, lines, err=0.01):
    return [Obs(x, float(1 - norm_cdf((x - mu) / sd)), err, f"x{x}") for x in lines]


def test_fit_recovers_monotone_curve_and_flags_violation():
    obs = normal_ladder(3.0, 13.0, [1.5, 2.5, 3.5, 6.5, 7.5, 10.5, 13.5])
    fit = fit_survival(obs)
    assert fit.flagged == [] and np.all(np.diff(fit.s) <= 1e-12)
    bad = obs + [Obs(4.5, 0.75, 0.01, "stale")]          # should be ~0.45; 30 err away
    fit2 = fit_survival(bad)
    assert fit2.flagged == [len(bad) - 1]
    assert abs(fit2.resid[-1]) > 3
    # down-weighted, not smoothed into the fit: neighbours stay near truth
    assert abs(fit2.at(3.5) - obs[2].p) < 0.02


def test_pmf_and_bins_from_normal_ladder():
    mu, sd = 3.0, 13.0
    lines = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 13.5, 14.5, 16.5, 17.5, 20.5]
    obs = normal_ladder(mu, sd, lines + [-x for x in lines], err=0.005)
    fit = fit_survival(obs)
    pmf = pmf_from_survival(fit, -80, 80, tail_sd=13.5)
    assert abs(pmf.mean - mu) < 0.3 and abs(pmf.sd - sd) < 0.6
    lo, hi = tail_scales(fit, 13.5)
    assert 11 < lo < 15 and 11 < hi < 15                 # slope recovers the true scale
    b = bins_from_fit(fit, -80, 80)
    assert abs(b.mass.sum() - 1) < 1e-12 and b.n_bins == len(fit.xs) + 1
    # value 2 is its own bin (lines 1.5 and 2.5); values 11,12,13 share one
    assert (b.bin_of[b.support == 2] != b.bin_of[b.support == 3]).all()
    assert b.bin_of[b.support == 11][0] == b.bin_of[b.support == 13][0]
    truth = np.array([norm_cdf((k + 0.5 - mu) / sd) - norm_cdf((k - 0.5 - mu) / sd) for k in range(-80, 81)])
    assert np.abs(pmf.p - truth).sum() < 0.03


def test_tie_bucket_split_uses_moneyline():
    mu, sd = 0.0, 10.0
    lines = [1.5, 2.5, 3.5, 6.5, 10.5]
    obs = normal_ladder(mu, sd, lines + [-x for x in lines], err=0.005)
    s_ml = 0.5                                            # P(M>0) + 0.5 P(M=0) for a symmetric margin
    obs.append(Obs(0.0, s_ml, 0.01, "ml"))
    fit = fit_survival(obs)
    bins = bins_from_fit(fit, -80, 80)
    prior = np.array([norm_cdf((k + 0.5) / sd) - norm_cdf((k - 0.5) / sd) for k in range(-80, 81)])
    nb, note = split_tie_bucket(bins, fit, prior, 0.004)
    assert nb.n_bins == bins.n_bins + 2 and "tie_prob" in note
    i0 = nb.bin_of[nb.support == 0][0]
    assert abs(nb.mass[i0] - 0.004) < 1e-9
    assert abs(nb.mass[i0 - 1] - nb.mass[i0 + 1]) < 0.005         # symmetric by the moneyline
    nb2, note2 = split_tie_bucket(bins, fit, prior, None)
    assert "prior share" in note2 and nb2.mass[nb2.bin_of[nb2.support == 0][0]] > 0.02
