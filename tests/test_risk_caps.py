import pytest

from rfqbot.pricing.joint import build_game
from rfqbot.pricing.joint_nfl import EngineConfig
from rfqbot.pricing.synthetic import make_truth, quotes_from_truth
from rfqbot.risk.book import Book, Position, PositionLeg
from rfqbot.risk.caps import Caps, bisect_limit, check, linear_limit, marginal_risk, usage
from rfqbot.risk.exposure import Exposure, es_at
from rfqbot.sports.legspec import LegSpec, canonical_key

G1 = "nfl-dal-phi-2026-10-27"
OFF = Caps(capital_usd=1000.0, max_outcome_delta=1e9, max_game_worst_loss=1e9, max_portfolio_es1=1e9)


@pytest.fixture(scope="module")
def models():
    m = build_game(quotes_from_truth(make_truth(), game=G1), config=EngineConfig(tie_prob=None, n_scenarios=20_000), seed=1)
    return {G1: m}


def ml(team):
    return LegSpec(venue="polymarket", sport="nfl", away="DAL", home="PHI", game_date="2026-10-27", family="moneyline",
                   period="FG", side=team)


def leg(sp, p=None):
    return PositionLeg(canonical_key(sp), canonical_key(sp), sp, p, G1)


def factor_leg(pid, p, game="nba-phi-nyk-2026-10-20"):
    return PositionLeg(pid, None, None, p, game, slug=game, outcome="X")


def pos(rid, legs, sign=1, size=1.0, price=0.5):
    return Position(rid, legs, sign, size, price, "filled")


def test_linear_and_bisect_limits():
    assert linear_limit(100, 1, 500) == 400
    assert linear_limit(100, -1, 500) == 600            # reducing through zero up to the other side
    assert linear_limit(600, 1, 500) == 0               # already breached, worsening
    assert linear_limit(600, -1, 500) == 1100           # already breached, reducing
    assert linear_limit(0, 0, 500) == float("inf")
    assert bisect_limit(lambda s: s <= 123.456, 1.0) == pytest.approx(123.456, abs=1e-6)
    assert bisect_limit(lambda s: False, 1.0) == 0.0
    assert bisect_limit(lambda s: True, 1.0) == float("inf")


def test_outcome_cap_solves_in_closed_form(models):
    book = Book([pos("b1", [leg(ml("DAL"))], 1, 100.0, 0.5)])           # delta +100 to DAL win
    caps = Caps(capital_usd=1000.0, max_outcome_delta=0.5, max_game_worst_loss=1e9, max_portfolio_es1=1e9)
    cand = pos("c", [leg(ml("DAL"))], 1, 1000.0, 0.5)
    r = check(book, cand, models, caps=caps)
    assert r.binding_cap == "outcome" and r.allowed_size == pytest.approx(400.0, abs=1e-6) and not r.ok
    assert r.detail["outcome"].endswith("DAL win") and r.headroom_by_cap["game"] == float("inf")
    # the opposite side reduces the book: allowed through zero to -500 (600 shares), then it binds
    r2 = check(book, Position("c", [leg(ml("DAL"))], -1, 1000.0, 0.5, "filled"), models, caps=caps)
    assert r2.allowed_size == pytest.approx(600.0, abs=1e-6)
    # within the cap: ok, no binding cap
    r3 = check(book, pos("c", [leg(ml("DAL"))], 1, 10.0, 0.5), models, caps=caps)
    assert r3.ok and r3.binding_cap is None and r3.cap_size == pytest.approx(400.0, abs=1e-6)
    # factor outcome: an unmodeled leg's delta counts the same way
    book_f = Book([pos("b2", [factor_leg("nba1", 0.6)], 1, 450.0, 0.5)])
    r4 = check(book_f, pos("c", [factor_leg("nba1", 0.6)], 1, 100.0, 0.5), models, caps=caps)
    assert r4.binding_cap == "outcome" and r4.allowed_size == pytest.approx(50.0, abs=1e-6)


def test_game_worst_loss_cap(models):
    caps = Caps(capital_usd=1000.0, max_outcome_delta=1e9, max_game_worst_loss=0.10, max_portfolio_es1=1e9)
    cand = pos("c", [leg(ml("DAL"))], 1, 1000.0, 0.6)       # loses 0.6 per share when DAL does not win
    r = check(Book(), cand, models, caps=caps)
    assert r.binding_cap == "game" and r.allowed_size == pytest.approx(100 / 0.6, abs=1e-3)
    assert r.detail["game"] == G1
    # a book already past the cap: a worsening candidate gets nothing, a hedging one is allowed
    book = Book([pos("b", [leg(ml("DAL"))], 1, 500.0, 0.6)])             # worst -300 > limit 100
    assert check(book, cand, models, caps=caps).allowed_size == 0.0
    hedge = Position("h", [leg(ml("DAL"))], -1, 100.0, 0.6, "filled")
    assert check(book, hedge, models, caps=caps).allowed_size == pytest.approx(100.0)


def test_es1_cap_and_marginal_risk(models):
    caps = Caps(capital_usd=1000.0, max_outcome_delta=1e9, max_game_worst_loss=1e9, max_portfolio_es1=0.15)
    # a p=0.5 coin bought at 0.5: the 1% tail loses 0.5 per share -> ES1 = 0.5 s <= 150 -> s = 300
    cand = pos("c", [factor_leg("coin", 0.5)], 1, 1000.0, 0.5)
    r = check(Book(), cand, models, caps=caps, n_draws=20_000)
    assert r.binding_cap == "es1" and r.allowed_size == pytest.approx(300.0, rel=1e-3)
    # marginal risk: adding to a long book raises ES1, the hedge lowers it
    book = Book([pos("b", [leg(ml("DAL"))], 1, 100.0, 0.5)])
    ex = Exposure(book, models, n_draws=20_000)
    more = pos("m", [leg(ml("DAL"))], 1, 50.0, 0.5)
    hedge = Position("h", [leg(ml("DAL"))], -1, 50.0, 0.5, "filled")
    mr_more, mr_hedge = marginal_risk(book, more, models, exposure=ex), marginal_risk(book, hedge, models, exposure=ex)
    assert mr_more.es1_before == pytest.approx(es_at(ex.pnl_draws(), 0.01))
    assert mr_more.d_es1_per_share > 0 > mr_hedge.d_es1_per_share
    assert float(mr_more) == mr_more.d_es1_per_share
    assert mr_more.tail_mean_per_share == pytest.approx(0.5, abs=1e-9)        # in the tail DAL lost: -(0 - 0.5)
    assert mr_more.cov_by_game[G1] > 0 > mr_hedge.cov_by_game[G1]
    assert mr_more.corr_by_game[G1] == pytest.approx(1.0) and mr_hedge.corr_by_game[G1] == pytest.approx(-1.0)


def test_notional_caps_and_usage(models):
    caps = Caps(capital_usd=1000.0, max_outcome_delta=1e9, max_game_worst_loss=1e9, max_portfolio_es1=1e9,
                max_sport_notional=0.5, max_day_notional=0.2)
    book = Book([pos("b", [leg(ml("DAL"))], 1, 100.0, 0.4)])             # nfl / 2026-10-27 collateral 40
    cand = pos("c", [leg(ml("DAL"))], 1, 1000.0, 0.4)
    r = check(book, cand, models, caps=caps)
    assert r.headroom_by_cap["sport"] == pytest.approx((500 - 40) / 0.4)
    assert r.headroom_by_cap["day"] == pytest.approx((200 - 40) / 0.4) and r.binding_cap == "day"
    u = usage(Exposure(book, models), book, caps)
    assert u["sport"]["ratio"] == pytest.approx(0.08) and u["day"]["driver"] == "2026-10-27"
    assert u["outcome"]["driver"].endswith("DAL win") and u["game"]["driver"] == G1 and "es1" in u


def test_caps_from_env():
    c = Caps.from_env({"RISK_CAPITAL_USD": "20000", "RISK_MAX_OUTCOME_DELTA": "0.1", "RISK_MAX_SPORT_NOTIONAL": "0.4"})
    assert c.capital_usd == 20000 and c.max_outcome_delta == 0.1 and c.max_sport_notional == 0.4
    assert c.max_day_notional is None and c.max_game_worst_loss == 0.10 and c.max_portfolio_es1 == 0.15
    assert c.limit("outcome") == 2000 and c.limit("day") is None
    d = Caps.from_env({})
    assert d.capital_usd == 50_000 and d.limit("es1") == 7_500
