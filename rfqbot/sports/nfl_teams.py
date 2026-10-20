"""The 32 NFL teams and their venue-specific codes. Exact lookups only; unknown input raises.

Canonical code = the common NFL abbreviation (LAR for the Rams, JAX for the Jaguars).
Verified 2026-10-20 against the Polymarket catalog (all 32 slug codes present) and live
Kalshi tickers / titles (all 32 Kalshi codes seen in KXNFL* game markets).

Differences between venues:
  * Rams:     Poly slug ``la``  / Kalshi ``LAR`` / Kalshi title "Los Angeles R"
  * Chargers: Poly slug ``lac`` / Kalshi ``LAC`` / Kalshi title "Los Angeles C"
  * Jaguars:  Poly slug ``jax`` / Kalshi ``JAC``
  * Giants / Jets: Kalshi titles "New York G" / "New York J"
Everything else is the same three-letter code in lowercase (Poly) and uppercase (Kalshi).
"""
from __future__ import annotations

from dataclasses import dataclass


class UnknownTeam(KeyError):
    pass


@dataclass(frozen=True)
class Team:
    code: str          # canonical
    city: str
    nickname: str      # Polymarket titles use only this ("Vikings", "49ers")
    poly_slug: str     # lowercase code in Polymarket slugs
    kalshi_code: str   # uppercase code in Kalshi tickers
    kalshi_city: str   # city string as used in Kalshi titles and rules text

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.nickname}"


_T = Team
TEAMS: tuple[Team, ...] = (
    _T("ARI", "Arizona", "Cardinals", "ari", "ARI", "Arizona"),
    _T("ATL", "Atlanta", "Falcons", "atl", "ATL", "Atlanta"),
    _T("BAL", "Baltimore", "Ravens", "bal", "BAL", "Baltimore"),
    _T("BUF", "Buffalo", "Bills", "buf", "BUF", "Buffalo"),
    _T("CAR", "Carolina", "Panthers", "car", "CAR", "Carolina"),
    _T("CHI", "Chicago", "Bears", "chi", "CHI", "Chicago"),
    _T("CIN", "Cincinnati", "Bengals", "cin", "CIN", "Cincinnati"),
    _T("CLE", "Cleveland", "Browns", "cle", "CLE", "Cleveland"),
    _T("DAL", "Dallas", "Cowboys", "dal", "DAL", "Dallas"),
    _T("DEN", "Denver", "Broncos", "den", "DEN", "Denver"),
    _T("DET", "Detroit", "Lions", "det", "DET", "Detroit"),
    _T("GB", "Green Bay", "Packers", "gb", "GB", "Green Bay"),
    _T("HOU", "Houston", "Texans", "hou", "HOU", "Houston"),
    _T("IND", "Indianapolis", "Colts", "ind", "IND", "Indianapolis"),
    _T("JAX", "Jacksonville", "Jaguars", "jax", "JAC", "Jacksonville"),
    _T("KC", "Kansas City", "Chiefs", "kc", "KC", "Kansas City"),
    _T("LAR", "Los Angeles", "Rams", "la", "LAR", "Los Angeles R"),
    _T("LAC", "Los Angeles", "Chargers", "lac", "LAC", "Los Angeles C"),
    _T("LV", "Las Vegas", "Raiders", "lv", "LV", "Las Vegas"),
    _T("MIA", "Miami", "Dolphins", "mia", "MIA", "Miami"),
    _T("MIN", "Minnesota", "Vikings", "min", "MIN", "Minnesota"),
    _T("NE", "New England", "Patriots", "ne", "NE", "New England"),
    _T("NO", "New Orleans", "Saints", "no", "NO", "New Orleans"),
    _T("NYG", "New York", "Giants", "nyg", "NYG", "New York G"),
    _T("NYJ", "New York", "Jets", "nyj", "NYJ", "New York J"),
    _T("PHI", "Philadelphia", "Eagles", "phi", "PHI", "Philadelphia"),
    _T("PIT", "Pittsburgh", "Steelers", "pit", "PIT", "Pittsburgh"),
    _T("SEA", "Seattle", "Seahawks", "sea", "SEA", "Seattle"),
    _T("SF", "San Francisco", "49ers", "sf", "SF", "San Francisco"),
    _T("TB", "Tampa Bay", "Buccaneers", "tb", "TB", "Tampa Bay"),
    _T("TEN", "Tennessee", "Titans", "ten", "TEN", "Tennessee"),
    _T("WAS", "Washington", "Commanders", "was", "WAS", "Washington"),
)
assert len(TEAMS) == 32

BY_CODE: dict[str, Team] = {t.code: t for t in TEAMS}
BY_POLY_SLUG: dict[str, Team] = {t.poly_slug: t for t in TEAMS}
BY_KALSHI_CODE: dict[str, Team] = {t.kalshi_code: t for t in TEAMS}
BY_NICKNAME: dict[str, Team] = {t.nickname: t for t in TEAMS}
BY_KALSHI_CITY: dict[str, Team] = {t.kalshi_city: t for t in TEAMS}
for _m in (BY_CODE, BY_POLY_SLUG, BY_KALSHI_CODE, BY_NICKNAME, BY_KALSHI_CITY):
    assert len(_m) == 32, "team lookup keys must be unique"


def _lookup(table: dict[str, Team], key: str, what: str) -> Team:
    try:
        return table[key]
    except KeyError:
        raise UnknownTeam(f"unknown NFL {what}: {key!r}") from None


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
