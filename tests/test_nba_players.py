import json
from pathlib import Path

import pytest

from rfqbot.sports import nba_players
from rfqbot.sports.legspec import LegSpec
from rfqbot.sports.nba_kalshi import parse_kalshi_nba
from rfqbot.sports.nba_poly import parse_poly_nba

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name,key,init_sur,slug", [
    ("Shai Gilgeous-Alexander", "shai gilgeousalexander", "SGILGEOUSALEXANDER", "shai-gilgeous-alexander"),
    ("Nickeil Alexander-Walker", "nickeil alexanderwalker", "NALEXANDERWALKER", "nickeil-alexander-walker"),
    ("Trey Murphy III", "trey murphy", "TMURPHY", "trey-murphy-iii"),
    ("De'Aaron Fox", "deaaron fox", "DFOX", "deaaron-fox"),
    ("Michael Porter Jr.", "michael porter", "MPORTER", "michael-porter-jr"),
    ("Michael Porter Jr", "michael porter", "MPORTER", "michael-porter-jr"),
    ("Wendell Carter Jr.", "wendell carter", "WCARTER", "wendell-carter-jr"),
    ("Karl-Anthony Towns", "karlanthony towns", "KTOWNS", "karl-anthony-towns"),
    ("P.J. Washington", "pj washington", "PWASHINGTON", "pj-washington"),
    ("Alperen Sengün", "alperen sengun", "ASENGUN", "alperen-sengun"),
    ("Luka Dončić", "luka doncic", "LDONCIC", "luka-doncic"),
    ("Day'Ron Sharpe", "dayron sharpe", "DSHARPE", "dayron-sharpe"),
    ("Fred VanVleet", "fred vanvleet", "FVANVLEET", "fred-vanvleet"),
    ("OG Anunoby", "og anunoby", "OANUNOBY", "og-anunoby"),
    ("Miles McBride", "miles mcbride", "MMCBRIDE", "miles-mcbride"),
])
def test_normalization(name, key, init_sur, slug):
    assert nba_players.name_key(name) == key
    assert nba_players.initial_surname(name) == init_sur
    assert nba_players.poly_slug(name) == slug


def test_suffix_only_stripped_when_trailing():
    assert nba_players.name_tokens("Jr Smith") == ["jr", "smith"]      # leading token kept
    assert nba_players.name_tokens("V") == ["v"]                       # single token never stripped
    with pytest.raises(nba_players.PlayerNameError):
        nba_players.name_key("...")
    with pytest.raises(nba_players.PlayerNameError):
        nba_players.poly_slug("")


def test_single_candidate_rule():
    pool = ["Jalen Williams", "Jaylin Williams", "Kevin Durant"]
    assert nba_players.single_candidate("Kevin Durant", pool, str) == "Kevin Durant"
    # two players with the same initial+surname in the pool -> none, never a pick
    assert nba_players.single_candidate("Jalen Williams", pool, str) is None
    assert nba_players.single_candidate("Nobody Here", pool, str) is None


def _kalshi_specs():
    out = []
    for m in json.loads((FIX / "nba_kalshi_markets.json").read_text()):
        try:
            out.append(parse_kalshi_nba(m["ticker"], m["title"], m["floor_strike"], m["strike_type"], m["rules_primary"],
                                        m["expected_expiration_time"], yes_sub_title=m["yes_sub_title"], rules_secondary=m["rules_secondary"]))
        except ValueError:
            pass   # no-counterpart series in the fixture are expected to raise
    return out


def _poly(slug, title, outcome, idx):
    return parse_poly_nba(slug, title, outcome, idx)


def test_match_player_prop_exact_line():
    ks = _kalshi_specs()
    p = _poly("nba-okc-sas-2026-10-20-points-deaaron-fox-14pt5", "De'Aaron Fox: Points O/U 14.5", "Yes", 0)
    k, side = nba_players.match_player_prop(p, ks)
    assert k.venue_ids["ticker"] == "KXNBAPTS-26OCT20OKCSAS-SASDFOX4-15"
    assert side == "yes" and k.line == 14.5 and k.side == "over"
    p_no = _poly("nba-okc-sas-2026-10-20-points-deaaron-fox-14pt5", "De'Aaron Fox: Points O/U 14.5", "No", 1)
    assert nba_players.match_player_prop(p_no, ks)[1] == "no"


def test_match_player_prop_hyphen_and_suffix_names():
    ks = _kalshi_specs()
    p = _poly("nba-okc-sas-2026-10-20-assists-shai-gilgeous-alexander-5pt5", "Shai Gilgeous-Alexander: Assists O/U 5.5", "Yes", 0)
    assert nba_players.match_player_prop(p, ks)[0].venue_ids["ticker"] == "KXNBAAST-26OCT20OKCSAS-OKCSGILGEOUSALEXANDER2-6"
    p = _poly("nba-ind-nop-2026-10-21-assists-trey-murphy-iii-3pt5", "Trey Murphy III: Assists O/U 3.5", "Yes", 0)
    assert nba_players.match_player_prop(p, ks)[0].venue_ids["ticker"] == "KXNBAAST-26OCT21INDNOP-NOPTMURPHY25-4"
    p = _poly("nba-atl-orl-2026-10-21-assists-nickeil-alexander-walker-3pt5", "Nickeil Alexander-Walker: Assists O/U 3.5", "Yes", 0)
    assert nba_players.match_player_prop(p, ks)[0].venue_ids["ticker"] == "KXNBAAST-26OCT21ATLORL-ATLNALEXANDERWALKER7-4"


def test_match_player_prop_none_when_line_or_stat_differs():
    ks = _kalshi_specs()
    # Porter Jr. points 21.5 on Poly; Kalshi has 24.5 and 29.5 only -> none (shifted line is the matcher's job)
    p = _poly("nba-cha-bkn-2026-10-21-points-michael-porter-jr-21pt5", "Michael Porter Jr.: Points O/U 21.5", "Yes", 0)
    assert nba_players.match_player_prop(p, ks) is None
    # Fox 14.5 points matches, Fox 14.5 rebounds does not
    p = _poly("nba-okc-sas-2026-10-20-rebounds-deaaron-fox-14pt5", "De'Aaron Fox: Rebounds O/U 14.5", "Yes", 0)
    assert nba_players.match_player_prop(p, ks) is None


def test_match_player_prop_rejects_non_props():
    p = _poly("nba-atl-cha-2026-10-23", "Hawks vs. Hornets", "Hawks", 0)
    with pytest.raises(nba_players.PlayerNameError):
        nba_players.match_player_prop(p, [])


def test_player_key_and_collisions():
    assert nba_players.player_key("Michael Porter Jr.") == "m.porter"
    assert nba_players.player_key("Alexandre Sarr") == nba_players.player_key("Alex Sarr") == "a.sarr"
    base = dict(venue="kalshi", sport="nba", away="OKC", home="SAS", game_date="2026-10-20", family="player_points",
                period="FG", side="over", line=14.5, bucket="j.williams")
    k1 = LegSpec(**base, venue_ids={"ticker": "A", "player": "Jalen Williams"})
    k2 = LegSpec(**base, venue_ids={"ticker": "B", "player": "Jaylin Williams"})
    assert nba_players.bucket_collisions([k1]) == {}
    assert nba_players.bucket_collisions([k1, k2]) == {("nba|2026-10-20|OKC@SAS", "j.williams"): {"Jalen Williams", "Jaylin Williams"}}


def test_match_player_prop_two_candidates_is_none():
    base = dict(venue="kalshi", sport="nba", away="OKC", home="SAS", game_date="2026-10-20", family="player_points",
                period="FG", side="over", line=14.5)
    k1 = LegSpec(**base, bucket="j.williams", venue_ids={"ticker": "A", "player": "Jalen Williams"})
    k2 = LegSpec(**base, bucket="j.williams", venue_ids={"ticker": "B", "player": "Jaylin Williams"})
    p = LegSpec(venue="polymarket", sport="nba", away="OKC", home="SAS", game_date="2026-10-20", family="player_points",
                period="FG", side="over", line=14.5, bucket="j.williams", venue_ids={"player": "Jalen Williams"})
    assert nba_players.match_player_prop(p, [k1, k2]) is None
    assert nba_players.match_player_prop(p, [k1])[0] is k1
