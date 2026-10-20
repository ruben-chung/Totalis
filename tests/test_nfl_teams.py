import pytest

from rfqbot.sports import nfl_teams as t


def test_thirty_two_unique_teams():
    assert len(t.TEAMS) == 32
    for attr in ("code", "poly_slug", "kalshi_code", "nickname", "kalshi_city"):
        assert len({getattr(x, attr) for x in t.TEAMS}) == 32


def test_lookups_and_venue_differences():
    assert t.by_poly_slug("la").code == "LAR" and t.by_poly_slug("lac").code == "LAC"
    assert t.by_kalshi_code("JAC").code == "JAX" and t.by_kalshi_code("LAR").nickname == "Rams"
    assert t.by_nickname("49ers").code == "SF"
    assert t.by_kalshi_city("Los Angeles R").code == "LAR" and t.by_kalshi_city("New York J").code == "NYJ"
    assert {x.code for x in t.TEAMS if x.poly_slug.upper() != x.kalshi_code} == {"JAX", "LAR"}
    assert t.by_code("PHI").full_name == "Philadelphia Eagles"


@pytest.mark.parametrize("fn,key", [(t.by_code, "phi"), (t.by_poly_slug, "LA"), (t.by_poly_slug, "lar"),
                                    (t.by_kalshi_code, "JAX"), (t.by_nickname, "Eagle"), (t.by_kalshi_city, "Los Angeles")])
def test_unknown_raises(fn, key):
    with pytest.raises(t.UnknownTeam):
        fn(key)
