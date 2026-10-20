"""The 30 NBA teams and their venue-specific codes. Exact lookups only; unknown input raises.

Canonical code = the Polymarket slug code uppercased, which is also the Kalshi ticker code for
all 30 teams (verified 2026-10-20: all 30 Poly slug codes present in the catalog's 52 games,
all 30 Kalshi codes seen in open KXNBA* tickers).

Kalshi titles / rules use a city string that is NOT always the team's city:
  * Clippers "Los Angeles C", Lakers "Los Angeles L" (same pattern as the NFL Rams/Chargers)
  * Knicks "New York" (only one NBA team there, so no suffix)
  * Warriors "Golden State", Trail Blazers "Portland"
Polymarket titles use only the nickname ("Trail Blazers" is two words, "76ers" starts with a digit).

The table is keyed by league so a WNBA table (``wnba-`` slug prefix, 152 catalog rows, same
grammar) can be added without touching the parsers; see ``LEAGUES`` at the bottom.
"""
from __future__ import annotations

from dataclasses import dataclass


class UnknownTeam(KeyError):
    pass


@dataclass(frozen=True)
class Team:
    code: str          # canonical (== Kalshi code)
    city: str
    nickname: str      # Polymarket titles use only this
    poly_slug: str     # lowercase code in Polymarket slugs
    kalshi_code: str   # uppercase code in Kalshi tickers
    kalshi_city: str   # city string as used in Kalshi titles and rules text

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.nickname}"


_T = Team
TEAMS: tuple[Team, ...] = (
    _T("ATL", "Atlanta", "Hawks", "atl", "ATL", "Atlanta"),
    _T("BOS", "Boston", "Celtics", "bos", "BOS", "Boston"),
    _T("BKN", "Brooklyn", "Nets", "bkn", "BKN", "Brooklyn"),
    _T("CHA", "Charlotte", "Hornets", "cha", "CHA", "Charlotte"),
    _T("CHI", "Chicago", "Bulls", "chi", "CHI", "Chicago"),
    _T("CLE", "Cleveland", "Cavaliers", "cle", "CLE", "Cleveland"),
    _T("DAL", "Dallas", "Mavericks", "dal", "DAL", "Dallas"),
    _T("DEN", "Denver", "Nuggets", "den", "DEN", "Denver"),
    _T("DET", "Detroit", "Pistons", "det", "DET", "Detroit"),
    _T("GSW", "Golden State", "Warriors", "gsw", "GSW", "Golden State"),
    _T("HOU", "Houston", "Rockets", "hou", "HOU", "Houston"),
    _T("IND", "Indiana", "Pacers", "ind", "IND", "Indiana"),
    _T("LAC", "Los Angeles", "Clippers", "lac", "LAC", "Los Angeles C"),
    _T("LAL", "Los Angeles", "Lakers", "lal", "LAL", "Los Angeles L"),
    _T("MEM", "Memphis", "Grizzlies", "mem", "MEM", "Memphis"),
    _T("MIA", "Miami", "Heat", "mia", "MIA", "Miami"),
    _T("MIL", "Milwaukee", "Bucks", "mil", "MIL", "Milwaukee"),
    _T("MIN", "Minnesota", "Timberwolves", "min", "MIN", "Minnesota"),
    _T("NOP", "New Orleans", "Pelicans", "nop", "NOP", "New Orleans"),
    _T("NYK", "New York", "Knicks", "nyk", "NYK", "New York"),
    _T("OKC", "Oklahoma City", "Thunder", "okc", "OKC", "Oklahoma City"),
    _T("ORL", "Orlando", "Magic", "orl", "ORL", "Orlando"),
    _T("PHI", "Philadelphia", "76ers", "phi", "PHI", "Philadelphia"),
    _T("PHX", "Phoenix", "Suns", "phx", "PHX", "Phoenix"),
    _T("POR", "Portland", "Trail Blazers", "por", "POR", "Portland"),
    _T("SAC", "Sacramento", "Kings", "sac", "SAC", "Sacramento"),
    _T("SAS", "San Antonio", "Spurs", "sas", "SAS", "San Antonio"),
    _T("TOR", "Toronto", "Raptors", "tor", "TOR", "Toronto"),
    _T("UTA", "Utah", "Jazz", "uta", "UTA", "Utah"),
    _T("WAS", "Washington", "Wizards", "was", "WAS", "Washington"),
)
assert len(TEAMS) == 30

BY_CODE: dict[str, Team] = {t.code: t for t in TEAMS}
BY_POLY_SLUG: dict[str, Team] = {t.poly_slug: t for t in TEAMS}
BY_KALSHI_CODE: dict[str, Team] = {t.kalshi_code: t for t in TEAMS}
BY_NICKNAME: dict[str, Team] = {t.nickname: t for t in TEAMS}
BY_KALSHI_CITY: dict[str, Team] = {t.kalshi_city: t for t in TEAMS}
for _m in (BY_CODE, BY_POLY_SLUG, BY_KALSHI_CODE, BY_NICKNAME, BY_KALSHI_CITY):
    assert len(_m) == 30, "team lookup keys must be unique"

#: slug prefix -> (sport name, lookup tables). Only the NBA is populated; the WNBA slot is
#: here so the parsers can be pointed at a second table without changing their grammar.
LEAGUES: dict[str, str] = {"nba": "nba"}


def _lookup(table: dict[str, Team], key: str, what: str) -> Team:
    try:
        return table[key]
    except KeyError:
        raise UnknownTeam(f"unknown NBA {what}: {key!r}") from None


def by_code(code: str) -> Team:
    return _lookup(BY_CODE, code, "code")


def by_poly_slug(slug_code: str) -> Team:
    return _lookup(BY_POLY_SLUG, slug_code, "poly slug code")


def by_kalshi_code(code: str) -> Team:
    return _lookup(BY_KALSHI_CODE, code, "kalshi code")


def by_nickname(nickname: str) -> Team:
    return _lookup(BY_NICKNAME, nickname, "nickname")


def by_kalshi_city(city: str) -> Team:
    return _lookup(BY_KALSHI_CITY, city, "kalshi city")
