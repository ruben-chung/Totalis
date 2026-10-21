import random

import pytest

from rfqbot.edge.config import EdgeConfig
from rfqbot.edge.fillcurve import empirical_curve, fit_curves, isotonic_decreasing
from rfqbot.edge.fills import Fill
from rfqbot.edge.segments import GLOBAL, Segment, n_legs_bucket, sigma_bucket

CFG = EdgeConfig(n_min=10, grid_max=0.05, grid_step=0.01, sigma_cuts=(0.005, 0.009))


def mk(i, delta, *, direction="BUY", sport="nba", n_legs=2, same_game=False, sigma=0.006, margin=0.01, printed=True):
    return Fill(decision_id=i, rfq_id=f"r{i}", at_ms=i, direction=direction, sport=sport, n_legs=n_legs, same_game=same_game,
                p_fair=0.3, sigma=sigma, margin=margin, price=0.31, size_e6=1, requester="w", round_trip=False, printed=printed,
                delta=delta if printed else None, win_actual=bool(printed and delta is not None and margin <= delta))


def test_isotonic_decreasing_pava():
    assert isotonic_decreasing([1.0, 0.8, 0.9, 0.5]) == pytest.approx([1.0, 0.85, 0.85, 0.5])
    assert isotonic_decreasing([0.2, 0.4]) == pytest.approx([0.3, 0.3])
    assert isotonic_decreasing([]) == []
    y = isotonic_decreasing([0.5, 0.1, 0.3, 0.3, 0.0])
    assert all(a >= b for a, b in zip(y, y[1:]))


def test_empirical_curve_is_monotone_and_counts_unprinted_in_denominator():
    fills = [mk(1, 0.03), mk(2, 0.01), mk(3, 0.045), mk(4, None, printed=False)]
    c = empirical_curve(Segment.parse("nba|2|BUY|disjoint|s1"), fills, CFG.grid())
    assert c.n == 4 and c.n_printed == 3
    assert c.f == pytest.approx([0.75, 0.75, 0.5, 0.5, 0.25, 0.0])             # m = 0, .01, .02, .03, .04, .05
    assert c.f_print == pytest.approx([1.0, 1.0, 2 / 3, 2 / 3, 1 / 3, 0.0])
    assert all(a >= b for a, b in zip(c.f, c.f[1:]))
    assert abs(c.delta_mean - (0.03 + 0.01 + 0.045) / 3) < 1e-12
    assert c.n_win_actual == 3 and c.fill_frac_mean == 0.0   # win_actual set by hand, fill_frac left 0
    assert abs(c.f_at(0.015) - 0.625) < 1e-12 and c.f_at(1.0) == 0.0


def test_segments_ladder_and_buckets():
    assert [n_legs_bucket(n) for n in (1, 2, 3, 4, 5, 9)] == ["2", "2", "3-4", "3-4", "5+", "5+"]
    assert [sigma_bucket(s, (0.005, 0.009)) for s in (0.001, 0.005, 0.0089, 0.02, None)] == ["s0", "s1", "s1", "s2", "*"]
    s = Segment.of("nba", 3, "SELL", True, 0.012, (0.005, 0.009))
    keys = [x.key for x in s.ladder()]
    assert keys == ["nba|3-4|SELL|same_game|s2", "nba|3-4|SELL|same_game|*", "nba|3-4|SELL|*|*", "nba|*|SELL|*|*", "*|*|SELL|*|*", "*|*|*|*|*"]
    assert [x.level for x in s.ladder()] == [0, 1, 2, 3, 4, 5]
    assert Segment.parse("nba|*|SELL|*|*").contains(s) and not Segment.parse("nfl|*|SELL|*|*").contains(s)
    assert GLOBAL.parent() is None


def test_pooling_falls_back_until_n_min():
    rng = random.Random(1)
    fills = []
    # 12 NBA BUY s1 fills (enough), 3 NBA BUY s2 (too few -> sigma parent "nba|2|BUY|disjoint|*" has 15), 2 SELL (-> up to global)
    for i in range(12):
        fills.append(mk(i, rng.uniform(0, 0.04), sigma=0.006))
    for i in range(12, 15):
        fills.append(mk(i, 0.02, sigma=0.02))
    for i in range(15, 17):
        fills.append(mk(i, 0.01, direction="SELL"))
    t = fit_curves(fills, CFG)
    assert t.cells["nba|2|BUY|disjoint|s1"] == ("nba|2|BUY|disjoint|s1", 0)
    assert t.cells["nba|2|BUY|disjoint|s2"] == ("nba|2|BUY|disjoint|*", 1)
    assert t.cells["nba|2|SELL|disjoint|s1"] == (GLOBAL.key, 5)       # only 2 SELL fills anywhere: global, with its 17
    assert t.nodes[GLOBAL.key].n == 17 and t.nodes["nba|2|BUY|disjoint|*"].n == 15
    # an unseen cell resolves through the same ladder
    node, level = t.resolve(Segment.parse("nba|2|BUY|disjoint|s0"))
    assert node.key == "nba|2|BUY|disjoint|*" and level == 1
    node, level = t.resolve(Segment.parse("nfl|5+|SELL|same_game|s0"))
    assert node.key == GLOBAL.key and level == 5
