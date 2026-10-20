import pytest

from rfqbot.sports import nba_teams

BRIEF_CODES = "atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop nyk okc orl phi phx por sac sas tor uta was".split()
BRIEF_NICKS = ["Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers", "Mavericks", "Nuggets", "Pistons", "Warriors",
               "Rockets", "Pacers", "Clippers", "Lakers", "Grizzlies", "Heat", "Bucks", "Timberwolves", "Pelicans", "Knicks",
               "Thunder", "Magic", "76ers", "Suns", "Trail Blazers", "Kings", "Spurs", "Raptors", "Jazz", "Wizards"]
BRIEF_KALSHI_CITIES = ["Atlanta", "Brooklyn", "Boston", "Charlotte", "Chicago", "Cleveland", "Dallas", "Denver", "Detroit",
                       "Golden State", "Houston", "Indiana", "Los Angeles C", "Los Angeles L", "Memphis", "Miami", "Milwaukee",
                       "Minnesota", "New Orleans", "New York", "Oklahoma City", "Orlando", "Philadelphia", "Phoenix", "Portland",
                       "Sacramento", "San Antonio", "Toronto", "Utah", "Washington"]


def test_thirty_teams_and_unique_keys():
    assert len(nba_teams.TEAMS) == 30
    assert sorted(nba_teams.BY_POLY_SLUG) == sorted(BRIEF_CODES)
    assert sorted(nba_teams.BY_NICKNAME) == sorted(BRIEF_NICKS)
    assert sorted(nba_teams.BY_KALSHI_CITY) == sorted(BRIEF_KALSHI_CITIES)
    assert {t.kalshi_code for t in nba_teams.TEAMS} == {c.upper() for c in BRIEF_CODES}
    assert all(len(t.kalshi_code) == 3 for t in nba_teams.TEAMS)


def test_lookups_and_specials():
    assert nba_teams.by_poly_slug("lal").kalshi_city == "Los Angeles L"
    assert nba_teams.by_poly_slug("lac").kalshi_city == "Los Angeles C"
    assert nba_teams.by_kalshi_city("New York").nickname == "Knicks"
    assert nba_teams.by_nickname("Trail Blazers").code == "POR"
    assert nba_teams.by_nickname("76ers").kalshi_city == "Philadelphia"
    assert nba_teams.by_kalshi_code("GSW").full_name == "Golden State Warriors"
    assert nba_teams.by_code("NOP").poly_slug == "nop"


@pytest.mark.parametrize("fn,key", [
    (nba_teams.by_code, "LA"), (nba_teams.by_poly_slug, "LAL"), (nba_teams.by_kalshi_code, "lal"),
    (nba_teams.by_nickname, "Blazers"), (nba_teams.by_kalshi_city, "Los Angeles"), (nba_teams.by_nickname, "Lakers "),
])
def test_unknown_raises(fn, key):
    with pytest.raises(nba_teams.UnknownTeam):
        fn(key)
