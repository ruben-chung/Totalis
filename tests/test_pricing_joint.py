import time

import numpy as np
import pytest

from rfqbot.pricing.indicators import indicator
from rfqbot.pricing.joint import DEFAULT_SHOCKS, build_game, parse_quotes
from rfqbot.pricing.joint_nfl import EngineConfig, Scenarios, build_period, table_marginal
from rfqbot.pricing.synthetic import make_truth, quotes_from_truth
from rfqbot.sports.legspec import LegSpec

CFG = EngineConfig(tie_prob=None, n_scenarios=50_000)


@pytest.fixture(scope="module")
def truth():
    return make_truth(mu_h=24.0, mu_a=21.0, sd_h=10.0, sd_a=9.0, rho=0.3)


@pytest.fixture(scope="module")
def model(truth):
    return build_game(quotes_from_truth(truth), config=CFG, seed=0)


def test_truth_recovery_with_team_totals(truth, model):
    fg = model.periods["FG"]
    assert all(r < 1e-3 for r in fg.ipf_residual.values()), fg.ipf_residual
    assert abs(fg.corr("H", "A") - truth.corr("FG", "H", "A")) < 0.05
    assert abs(fg.corr("M", "T") - truth.corr("FG", "M", "T")) < 0.05
    for var in "MTHA":
        tm = {"H": truth.fg.sum(axis=1), "A": truth.fg.sum(axis=0)}.get(var)
        tm = table_marginal(truth.fg, var) if tm is None else tm
        assert np.abs(fg.marginal(var) - tm).max() < 0.01, var
    assert [f for f in model.flags if not f.startswith(("2H ", "halves coupling"))] == []
    assert model.scen.coupling == "copula"        # no 2H ladders in this fixture


def test_margin_and_total_ladders_alone_pin_corr_h_a(truth):
    m = build_game(quotes_from_truth(truth, team_totals=False), config=CFG)
    fg = m.periods["FG"]
    assert abs(fg.corr("H", "A") - truth.corr("FG", "H", "A")) < 0.05      # Var(T)-Var(M) = 4 Cov(H,A)
    assert any("no team-total ladders" in n for n in m.notes)
    assert abs(fg.corr("M", "T")) < 0.05                                   # prior rho carries it


def test_marginal_consistency(model):
    checks = [c for c in model.marginal_check() if c.diff is not None]
    assert len(checks) == 264
    assert max(c.diff for c in checks) < 0.01
    assert not any(c.flagged for c in checks)


def test_violation_detection(truth):
    qs = quotes_from_truth(truth)
    q = next(x for x in qs if x.slug.endswith("spread-home-6pt5") and x.outcome_index == 0)
    i = qs.index(q)
    qs[i] = q.__class__(**{**q.__dict__, "best_bid": q.best_bid + 0.25, "best_ask": q.best_ask + 0.25})
    m = build_game(qs, config=CFG)
    assert any("ladder observation" in f for f in m.flags)
    assert any(ref.endswith("spread-home-6pt5|Eagles") for ref, _ in m.periods["FG"].violations)
    good = [c for c in m.marginal_check() if c.diff is not None and "-6.5" not in c.key]
    assert max(c.diff for c in good) < 0.02          # the bad quote did not drag its neighbours


def test_period_coupling(truth, model):
    s = model.scen
    assert s.clamp_rate == 0.0 and max(s.halves_residual.values()) < 1e-4
    assert np.all(s.H1 <= s.H) and np.all(s.A1 <= s.A)
    ct = model.correlation_table()
    assert 0.6 < ct["scenario_corr"]["H,H1"] < 0.85                     # ~ sd(H1)/sd(H) = 0.71
    assert abs(ct["scenario_corr"]["H1,A1"] - truth.corr("1H", "H", "A")) < 0.08
    h1 = model.periods["1H"]
    truth_mean_h1 = float(np.dot(np.arange(81), truth.h1.sum(axis=1)))   # truncated at 0, so above mu_h / 2
    assert abs(h1.moments("H")[0] - truth_mean_h1) < 0.3
    # a 1H ladder wider than FG is flagged
    wide = make_truth(sd_h=10.0, sd_a=9.0)
    wide.h1 = make_truth(sd_h=12.0, sd_a=11.0, mu_h=12, mu_a=10.5).fg   # first half more dispersed than the full game
    m2 = build_game(quotes_from_truth(wide), config=CFG)
    assert any("1H H sd" in f for f in m2.flags)


def test_second_half_ladders_drive_the_coupling(truth):
    m = build_game(quotes_from_truth(truth, second_half=True), config=CFG)
    assert m.scen.coupling == "increments", m.flags
    assert "2H" in m.periods and set(m.periods["2H"].pmfs) == {"M", "T", "H", "A"}
    checks = [c for c in m.marginal_check() if c.diff is not None]
    assert max(c.diff for c in checks) < 0.03                      # 2H legs are implied (FG - 1H), not fitted
    assert max(c.diff for c in checks if "|2H|" not in c.key) < 0.01
    assert not any(f.startswith("2H ") for f in m.flags)
    ct = m.correlation_table()
    assert 0.6 < ct["scenario_corr"]["H,H1"] < 0.85
    # a garbage 2H ladder (one wild quote per line) is rejected and the coupling falls back
    qs = quotes_from_truth(truth, second_half=True)
    bad = [q.__class__(**{**q.__dict__, "best_bid": 0.49, "best_ask": 0.51}) if "-2h-" in q.slug else q for q in qs]
    m2 = build_game(bad, config=CFG)
    assert m2.scen.coupling == "copula" and any(f.startswith("halves coupling:") for f in m2.flags)


def mk(**kw):
    base = dict(venue="polymarket", sport="nfl", away="DAL", home="PHI", game_date="2026-10-26", family="total",
                period="FG", side="over", line=44.5, rules={"tie": "50-50"})
    base.update(kw)
    return LegSpec(**base)


def hand_scen():
    # (H1, A1, H, A)
    rows = [(7, 3, 24, 20), (0, 0, 17, 17), (10, 14, 20, 31), (3, 0, 3, 0), (14, 14, 28, 28), (0, 7, 7, 10)]
    H1, A1, H, A = (np.array(c, dtype=np.int16) for c in zip(*rows))
    return Scenarios(H1=H1, A1=A1, H=H, A=A, clamp_rate=0.0, corr_h=0.7, corr_a=0.7, seed=0)


@pytest.mark.parametrize("spec,expected", [
    (mk(family="moneyline", line=None, side="PHI"), [1, .5, 0, 1, .5, 0]),
    (mk(family="moneyline", line=None, side="DAL"), [0, .5, 1, 0, .5, 1]),
    (mk(family="moneyline", line=None, side="tie"), [0, 1, 0, 0, 1, 0]),
    (mk(family="moneyline", line=None, side="not_PHI", rules={"tie": "tie_strike"}), [0, 1, 1, 0, 1, 1]),
    (mk(family="moneyline", line=None, side="PHI", period="1H"), [1, .5, 0, 1, .5, 0]),
    (mk(family="moneyline", line=None, side="DAL", period="2H"), [.5, .5, 1, .5, .5, 0]),
    (mk(family="spread", team="PHI", line=-3.5, side="PHI"), [1, 0, 0, 0, 0, 0]),
    (mk(family="spread", team="PHI", line=-3.5, side="DAL"), [0, 1, 1, 1, 1, 1]),
    (mk(family="spread", team="DAL", line=-7.5, side="DAL"), [0, 0, 1, 0, 0, 0]),
    (mk(family="spread", team="DAL", line=-2.5, side="PHI", period="1H"), [1, 1, 0, 1, 1, 0]),
    (mk(family="total", line=44.5, side="over"), [0, 0, 1, 0, 1, 0]),
    (mk(family="total", line=44.5, side="under"), [1, 1, 0, 1, 0, 1]),
    (mk(family="total", line=16.5, side="over", period="2H"), [1, 1, 1, 0, 1, 0]),
    (mk(family="team_total", team="DAL", line=20.5, side="over"), [0, 0, 1, 0, 1, 0]),
    (mk(family="team_total", team="PHI", line=6.5, side="under", period="1H"), [0, 1, 0, 1, 0, 1]),
    (mk(family="btts", line=None, side="yes", period="1H"), [1, 0, 1, 0, 1, 0]),
    (mk(family="btts", line=None, side="no", period="2H"), [0, 0, 0, 1, 0, 0]),
    (mk(family="exact_margin", line=None, team="PHI", bucket="1-6", side="yes"), [1, 0, 0, 1, 0, 0]),
    (mk(family="exact_margin", line=None, team="DAL", bucket="7-12", side="yes"), [0, 0, 1, 0, 0, 0]),
    (mk(family="exact_margin", line=None, team="DAL", bucket="25+", side="no"), [1, 1, 1, 1, 1, 1]),
    (mk(family="exact_margin", line=None, bucket="tie", side="yes"), [0, 1, 0, 0, 1, 0]),
])
def test_indicators_on_handcrafted_scenarios(spec, expected):
    out = indicator(spec, hand_scen())
    assert out is not None and out.tolist() == pytest.approx(expected)


def test_unsupported_indicators():
    s = hand_scen()
    assert indicator(mk(family="safety", line=None, side="yes"), s) is None
    assert indicator(mk(family="highest_scoring_quarter", line=None, bucket="Q1", side="yes"), s) is None
    assert indicator(mk(family="team_tds", team="PHI", line=2.5, side="over"), s) is None
    assert indicator(mk(period="1Q"), s) is None


def test_joint_logic_and_interval(model):
    legs = {l.spec.venue_ids["slug"] + "|" + l.spec.side: l.spec for l in model.legs}
    over = legs["nfl-dal-phi-2026-10-27-total-44pt5|over"]
    under = legs["nfl-dal-phi-2026-10-27-total-44pt5|under"]
    q = model.joint([over, under])
    assert q.p_joint == 0.0 and q.lo == 0.0 and q.hi == 0.0 and q.frechet_lo == 0.0
    c35 = legs["nfl-dal-phi-2026-10-27-spread-home-3pt5|PHI"]
    c75 = legs["nfl-dal-phi-2026-10-27-spread-home-7pt5|PHI"]
    q = model.joint([c35, c75])
    assert q.p_joint == pytest.approx(min(q.p_legs), abs=1e-9) and q.hi <= q.frechet_hi + 1e-9
    q = model.joint([c35, over])
    assert q.frechet_lo - 1e-9 <= q.lo <= q.p_joint <= q.hi <= q.frechet_hi + 1e-9
    assert q.p_joint > q.p_independent          # favourite covering and over are positively related in the truth
    assert set(q.shocked) == {s.name for s in DEFAULT_SHOCKS} and q.max_marginal_error < 0.01
    assert q.n_scenarios == 50_000 and q.unsupported == []
    single = model.joint([over])
    assert single.p_joint == pytest.approx(single.p_legs[0]) and single.p_independent == pytest.approx(model._fair_by_key[q.keys[1]].p)
    unsup = model.joint([over, mk(family="safety", line=None, side="yes")])
    assert unsup.unsupported and unsup.p_legs[1] is None and unsup.p_joint == pytest.approx(single.p_joint)
    with pytest.raises(ValueError):
        model.joint([mk(away="KC", home="SEA")])


def test_build_rejects_mixed_games_and_times_well(truth):
    qs = quotes_from_truth(truth) + quotes_from_truth(truth, game="nfl-kc-sea-2026-10-26")
    with pytest.raises(ValueError):
        build_game(qs, config=CFG)
    t0 = time.perf_counter()
    m = build_game(quotes_from_truth(truth), config=CFG)
    assert time.perf_counter() - t0 < 3.0
    legs, errs = parse_quotes(quotes_from_truth(truth))
    assert errs == [] and len(legs) == 264
    pm = build_period(legs, "FG", CFG)
    assert pm.ipf_sweeps > 0 and pm.ipf_change < CFG.ipf_tol
