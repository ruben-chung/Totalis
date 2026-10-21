import numpy as np
import pytest

from rfqbot.pricing.venue_weight import Reg, lvcf


def test_clustered_ols_recovers_slope_and_shrinks_se_with_clusters():
    rng = np.random.default_rng(0)
    reg = Reg()
    for g in range(40):
        x = rng.normal(size=500)
        shock = rng.normal() * 0.5                    # common cluster shock
        y = 0.25 + 0.6 * x + shock + rng.normal(size=500) * 0.1
        reg.add(f"g{g}", x, y)
    fit = reg.fit()
    assert fit["b"] == pytest.approx(0.6, abs=0.02) and fit["clusters"] == 40 and fit["n"] == 20_000
    assert 0 < fit["se"] < 0.02
    few = Reg()
    few.add("a", np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0]))
    assert np.isnan(few.fit()["b"])                   # one cluster: no sandwich


def test_lvcf_carries_last_value_and_is_nan_before_first():
    grid = np.array([0, 1000, 2000, 3000, 4000])
    at = np.array([1500, 3000])
    mid = np.array([0.4, 0.6])
    out = lvcf(grid, at, mid)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == 0.4 and out[3] == 0.6 and out[4] == 0.6
