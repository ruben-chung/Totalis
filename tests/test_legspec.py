import pytest

from rfqbot.sports import legspec as L


def mk(**kw):
    base = dict(venue="polymarket", sport="nfl", away="CIN", home="BAL", game_date="2026-10-25", family="total",
                period="FG", side="over", line=24.5)
    base.update(kw)
    return L.LegSpec(**base)


def test_canonical_key_and_complement():
    s = mk(family="spread", team="BAL", line=-7.5, side="BAL")
    assert L.canonical_key(s) == "nfl|2026-10-25|CIN@BAL|FG|spread|BAL|-7.5|BAL"
    assert L.contract_key(s) == "nfl|2026-10-25|CIN@BAL|FG|spread|BAL|-7.5"
    assert L.complement(s).side == "CIN" and L.complement(L.complement(s)) == s
    assert L.complement(mk()).side == "under"
    assert L.complement(mk(family="safety", line=None, side="no")).side == "yes"
    em = mk(family="exact_margin", line=None, team="CIN", bucket="1-6", side="yes")
    assert L.canonical_key(em) == "nfl|2026-10-25|CIN@BAL|FG|exact_margin|CIN:1-6|-|yes"
    assert L.canonical_key(mk(family="exact_margin", line=None, bucket="tie", side="yes")).endswith("|exact_margin|tie|-|yes")


def test_moneyline_complement_depends_on_tie_rule():
    two_way = mk(family="moneyline", line=None, side="CIN", rules={"tie": "50-50"})
    assert L.complement(two_way).side == "BAL"
    three_way = mk(venue="kalshi", family="moneyline", period="1H", line=None, side="CIN", rules={"tie": "tie_strike"})
    assert L.complement(three_way).side == "not_CIN"
    assert L.complement(L.complement(three_way)).side == "CIN"
    assert L.complement(mk(venue="kalshi", family="moneyline", period="1H", line=None, side="tie", rules={"tie": "tie_strike"})).side == "not_tie"


def test_validation():
    with pytest.raises(L.LegSpecError):
        mk(period="5Q")
    with pytest.raises(L.LegSpecError):
        mk(family="nope")
    with pytest.raises(L.LegSpecError):
        mk(side="yes")
    with pytest.raises(L.LegSpecError):
        mk(family="spread", team="BAL", line=7.5, side="BAL")  # spread line must be negative
    with pytest.raises(L.LegSpecError):
        mk(family="spread", team="PHI", line=-7.5, side="PHI")  # team not in game
    with pytest.raises(L.LegSpecError):
        mk(home="CIN")


def test_game_key_and_dates():
    s = mk(game_date_basis="utc_slug", game_date="2026-11-03", away="CHI", home="SEA")
    assert s.game_key == "nfl|2026-11-03|CHI@SEA"
    assert L.et_date_candidates(s) == ("2026-11-03", "2026-11-02")
    r = s.with_game_date("2026-11-02")
    assert r.game_date_basis == "et" and L.et_date_candidates(r) == ("2026-11-02",)
    assert r.other_team is None and mk(family="team_total", team="CIN").other_team == "BAL"


def test_register_family_hook():
    L.register_family("odd_even_test", ("odd", "even"))
    L.register_family("odd_even_test", ("odd", "even"))  # idempotent
    with pytest.raises(ValueError):
        L.register_family("odd_even_test", "yes_no")
    with pytest.raises(ValueError):
        L.register_family("bad", "nah")
    s = mk(family="odd_even_test", line=None, side="odd")
    assert L.complement(s).side == "even" and "odd_even_test" in L.FAMILIES
    with pytest.raises(L.LegSpecError):
        mk(family="odd_even_test", line=None, side="over")
    L.register_family("player_pts_test", "over_under")
    assert "player_pts_test" in L.OVER_UNDER_FAMILIES and "total" in L.OVER_UNDER_FAMILIES
    p = mk(family="player_pts_test", bucket="some-player", line=25.5)
    assert L.canonical_key(p) == "nfl|2026-10-25|CIN@BAL|FG|player_pts_test|some-player|25.5|over"
    L.register_family("first_score_test", "team")
    assert L.complement(mk(family="first_score_test", line=None, side="BAL")).side == "CIN"
