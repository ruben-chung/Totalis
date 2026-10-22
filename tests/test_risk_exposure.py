import json
import math

import numpy as np
import pytest

from rfqbot.pricing.joint import build_game
from rfqbot.pricing.joint_nfl import EngineConfig
from rfqbot.pricing.synthetic import make_truth, quotes_from_truth
from rfqbot.risk.book import Book, Position, PositionLeg
from rfqbot.risk.exposure import Draws, Exposure, ModelPool, canonical_outcomes
from rfqbot.sports.legspec import LegSpec, canonical_key

G1, G2 = "nfl-dal-phi-2026-10-27", "nfl-pit-no-2026-10-25"


@pytest.fixture(scope="module")
def models():
    cfg = EngineConfig(tie_prob=None, n_scenarios=20_000)
    m1 = build_game(quotes_from_truth(make_truth(), game=G1), config=cfg, seed=1)
    m2 = build_game(quotes_from_truth(make_truth(mu_h=20, mu_a=27), game=G2), config=cfg, seed=2)
    return {G1: m1, G2: m2}


def p_scen(model, sp):
    """Scenario-mean probability: what Exposure works with (p_leg is exact on the FG grid)."""
    return float(model.indicator(sp).mean(dtype=np.float64))


def spec(game, model, **kw):
    s0 = model.legs[0].spec
    base = dict(venue="polymarket", sport="nfl", away=s0.away, home=s0.home, game_date=s0.game_date, period="FG")
    base.update(kw)
    return LegSpec(**base)


def leg(game, sp, p=None, pid=None):
    return PositionLeg(pid or canonical_key(sp), canonical_key(sp), sp, p, game)


def factor_leg(pid, p, game="nba-phi-nyk-2026-10-20", sp=None):
    return PositionLeg(pid, canonical_key(sp) if sp else None, sp, p, game, slug=game, outcome="X")


def pos(rid, legs, sign=1, size=1.0, price=0.5):
    return Position(rid, legs, sign, size, price, "filled")


def test_moneyline_delta_is_one_to_its_own_win(models):
    m = models[G1]
    dal = spec(G1, m, family="moneyline", side="DAL")
    ex = Exposure(Book([pos("r1", [leg(G1, dal)], 1, 1.0, 0.55)]), models)
    deltas = {d.label: d.delta for d in ex.canonical_deltas(G1)}
    assert deltas["DAL win"] == pytest.approx(1.0, abs=1e-9)
    phi = spec(G1, m, family="moneyline", side="PHI")
    # not "PHI win" includes ties (neither team's win): E[I | not PHI] = P(DAL) / (1 - P(PHI))
    assert deltas["PHI win"] == pytest.approx(-p_scen(m, dal) / (1 - p_scen(m, phi)), abs=1e-9)
    # short the same combo: -1
    ex2 = Exposure(Book([pos("r1", [leg(G1, dal)], -1, 3.0, 0.55)]), models)
    assert {d.label: d.delta for d in ex2.canonical_deltas(G1)}["DAL win"] == pytest.approx(-3.0, abs=1e-9)
    # arbitrary LegSpec on request
    assert ex.delta(G1, dal) == pytest.approx(1.0, abs=1e-9)
    assert ex.delta(G1, spec(G1, m, family="total", side="over", line=44.5)) is not None


def test_cross_game_combo_factorises(models):
    m1, m2 = models[G1], models[G2]
    dal = spec(G1, m1, family="moneyline", side="DAL")
    pit = spec(G2, m2, family="moneyline", side="PIT")
    ex = Exposure(Book([pos("r1", [leg(G1, dal), leg(G2, pit)], 1, 10.0, 0.3)]), models)
    p_dal, p_pit = p_scen(m1, dal), p_scen(m2, pit)
    d1 = {d.label: d.delta for d in ex.canonical_deltas(G1)}
    d2 = {d.label: d.delta for d in ex.canonical_deltas(G2)}
    assert d1["DAL win"] == pytest.approx(10.0 * p_pit, rel=1e-9)
    assert d2["PIT win"] == pytest.approx(10.0 * p_dal, rel=1e-9)
    # with an unmodeled leg (NBA) instead: within-game delta times the Bernoulli's probability,
    # and the factor's own delta is size times the modeled leg's probability
    ex2 = Exposure(Book([pos("r2", [leg(G1, dal), factor_leg("nba1", 0.6)], 1, 10.0, 0.3)]), models)
    assert {d.label: d.delta for d in ex2.canonical_deltas(G1)}["DAL win"] == pytest.approx(6.0, rel=1e-9)
    assert ex2.factor_delta["nba1"] == pytest.approx(10.0 * p_dal, rel=1e-9)
    assert ex2.pool.errors["nba-phi-nyk-2026-10-20"] == "sport not modeled"


def test_unmodeled_legs_are_bernoullis_and_mc_matches_analytic(models):
    legs = [("a", 0.3, 1, 10.0, 0.25), ("b", 0.6, -1, 20.0, 0.65), ("c", 0.8, 1, 5.0, 0.7)]
    book = Book([pos(f"r{k}", [factor_leg(k, p, game=f"nba-{k}-x-2026-10-21")], sign, size, price) for k, p, sign, size, price in legs])
    ex = Exposure(book, models, n_draws=100_000, seed=3)
    for k, p, sign, size, price in legs:
        assert ex.factor_delta[k] == pytest.approx(sign * size, rel=1e-9)
    mean = sum(sign * size * (p - price) for _, p, sign, size, price in legs)
    var = sum(size * size * p * (1 - p) for _, p, size_sign, size, _ in [(k, p, sign, size, price) for k, p, sign, size, price in legs])
    pnl = ex.pnl_draws()
    assert pnl.mean() == pytest.approx(mean, abs=4 * math.sqrt(var / 100_000))
    assert pnl.std() == pytest.approx(math.sqrt(var), rel=0.03)
    port = ex.portfolio()
    # the worst atom (a loses 2.5, b wins and costs 7, c loses 3.5) has mass 0.7 x 0.6 x 0.2 = 8.4%,
    # so the whole 1% tail sits on it: ES1 is exactly 13
    assert port.es1 == pytest.approx(13.0, abs=1e-9)
    assert len(port.top_contributors) == 3


def test_same_market_complement_nets_out(models):
    models[G1]
    # one modeled game: over and under at the same line in two positions, unmodeled via a game with no model
    over = LegSpec(venue="polymarket", sport="nfl", away="DAL", home="PHI", game_date="2026-10-27", family="total",
                   period="FG", side="over", line=44.5)
    under = LegSpec(venue="polymarket", sport="nfl", away="DAL", home="PHI", game_date="2026-10-27", family="total",
                    period="FG", side="under", line=44.5)
    pool = ModelPool({}, build=False)      # no models at all -> factors
    book = Book([pos("r1", [leg("nfl-dal-phi-2026-10-27", over, 0.5)], 1, 5.0, 0.5),
                 pos("r2", [leg("nfl-dal-phi-2026-10-27", under, 0.5)], 1, 5.0, 0.5)])
    ex = Exposure(book, pool)
    assert len(ex.factors) == 1
    k = next(iter(ex.factors))
    assert ex.factor_delta[k] == pytest.approx(0.0, abs=1e-9)
    assert ex.pool.errors["nfl-dal-phi-2026-10-27"] == "no model (not built)"
    assert ex.pnl_draws().std() == pytest.approx(0.0, abs=1e-9)        # long both sides = flat


def test_terms_merge_identical_combos_and_report_serialises(models):
    m = models[G1]
    dal = spec(G1, m, family="moneyline", side="DAL")
    book = Book([pos("r1", [leg(G1, dal)], 1, 10.0, 0.5), pos("r2", [leg(G1, dal)], -1, 4.0, 0.6),
                 pos("r3", [leg(G1, dal)], 1, 1.0, 0.5)])
    ex = Exposure(book, models)
    assert len(ex.terms) == 1
    t = next(iter(ex.terms.values()))
    assert t.n == 3 and t.a == pytest.approx(7.0) and t.c == pytest.approx(10 * 0.5 - 4 * 0.6 + 0.5)
    rep = ex.report(outcomes=[("dal", dal)])
    json.dumps(rep.as_dict())
    assert rep.n_positions == 3 and rep.games[0].n_positions == 3
    assert rep.extra[0].delta == pytest.approx(7.0, abs=1e-9)
    labels = {d.label for d in rep.games[0].deltas}
    assert {"DAL win", "PHI win"} <= labels
    assert any(l.startswith("total over") for l in labels) and any("covers" in l for l in labels)
    assert any("total over" in l and l.split()[0] in ("DAL", "PHI") for l in labels)       # team totals
    dist = ex.game_distribution(G1)
    assert dist["worst"] <= dist["q01"] <= dist["q05"] <= dist["mean"] + 1e-9
    assert rep.worst_game().game == G1
    top = rep.top_deltas(3)
    assert abs(top[0][1]) >= abs(top[-1][1])


def test_per_game_vector_is_conditional_expectation(models):
    """V_g = a * (ind_g * p_rest - q): mean over scenarios equals the expected P&L of the position."""
    m1, m2 = models[G1], models[G2]
    dal = spec(G1, m1, family="moneyline", side="DAL")
    pit = spec(G2, m2, family="moneyline", side="PIT")
    ex = Exposure(Book([pos("r1", [leg(G1, dal), leg(G2, pit)], -1, 10.0, 0.4)]), models)
    expected = -10.0 * (p_scen(m1, dal) * p_scen(m2, pit) - 0.4)
    assert ex.game_vec[G1].mean() == pytest.approx(expected, rel=1e-9)
    assert ex.game_vec[G2].mean() == pytest.approx(expected, rel=1e-9)
    assert ex.pnl_draws().mean() == pytest.approx(expected, abs=0.15)


def test_draws_are_common_random_numbers():
    d = Draws(1000, seed=5)
    a, b = d.game("g", 50), Draws(1000, seed=5).game("g", 50)
    assert np.array_equal(a, b) and a.max() < 50
    assert not np.array_equal(d.factor("x"), d.factor("y"))


def test_canonical_outcomes_cover_the_main_lines(models):
    outs = canonical_outcomes(models[G1])
    labels = [l for l, _ in outs]
    assert labels[:2] == ["DAL win", "PHI win"]
    assert sum(1 for l in labels if l.startswith("total")) == 2
    assert sum(1 for l in labels if "covers" in l or "fails" in l) == 2
    assert sum(1 for l in labels if " total " in l) == 4


def test_pending_quotes_in_opposite_directions_never_net(models):
    """Two pending quotes to one requester on the same combo in opposite directions: at most one
    fills, and it is the adverse one, so the book counts the worse side, never zero."""
    m = models[G1]
    dal = spec(G1, m, family="moneyline", side="DAL")
    short = Position("q1", [leg(G1, dal)], -1, 100.0, 0.6, "pending", requester="req_a", condition_id="c1")
    long_ = Position("q2", [leg(G1, dal)], 1, 100.0, 0.6, "pending", requester="req_a", condition_id="c1")
    ex = Exposure(Book([short, long_]), models)
    d = {x.label: x.delta for x in ex.canonical_deltas(G1)}
    assert d["DAL win"] == pytest.approx(100.0, abs=1e-9)           # the long side risks 60, the short 40: long kept
    assert ex.flags["pending_opposite_sides_kept_worse"] == 1 and len(ex.positions) == 1
    # a bigger short wins the comparison
    ex2 = Exposure(Book([Position("q1", [leg(G1, dal)], -1, 200.0, 0.6, "pending", requester="req_a"), long_]), models)
    assert {x.label: x.delta for x in ex2.canonical_deltas(G1)}["DAL win"] == pytest.approx(-200.0, abs=1e-9)
    # different requesters do not share the rule (both can fill), and filled positions still net
    ex3 = Exposure(Book([short, Position("q3", [leg(G1, dal)], 1, 100.0, 0.6, "pending", requester="req_b")]), models)
    assert {x.label: x.delta for x in ex3.canonical_deltas(G1)}["DAL win"] == pytest.approx(0.0, abs=1e-9)
    ex4 = Exposure(Book([Position("f1", [leg(G1, dal)], -1, 100.0, 0.6, "filled"), Position("f2", [leg(G1, dal)], 1, 100.0, 0.6, "filled")]), models)
    assert {x.label: x.delta for x in ex4.canonical_deltas(G1)}["DAL win"] == pytest.approx(0.0, abs=1e-9)
    # a pending hedge nets against a filled position (it must count, or it could be quoted without limit)
    ex5 = Exposure(Book([Position("f1", [leg(G1, dal)], -1, 100.0, 0.6, "filled"), long_]), models)
    assert {x.label: x.delta for x in ex5.canonical_deltas(G1)}["DAL win"] == pytest.approx(0.0, abs=1e-9)


def test_model_pool_follows_the_cache_rebuild(models):
    class Entry:
        def __init__(self, model):
            self.model = model

    class Cache:
        def __init__(self):
            self.entries = {}
    cache = Cache()
    cache.entries[G1] = Entry(models[G1])
    pool = ModelPool(cache=cache, build=False)
    assert pool.get(G1) is models[G1]
    cache.entries[G1] = Entry(models[G2])              # the quoting cache rebuilt the game
    assert pool.get(G1) is models[G2]
    assert pool.get("nba-x-y-2026-10-21") is None and "nba-x-y-2026-10-21" in pool.errors
