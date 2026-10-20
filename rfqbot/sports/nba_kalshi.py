"""Kalshi NBA game-market parser: public-API market fields -> LegSpec (YES side).

Ticker grammar (verified on 4,885 open markets across 36 non-empty KXNBA* series, 2026-10-20):
    {SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{SUFFIX}
  * date is the Eastern game date, same as the Polymarket NBA slug date (no offset).
  * team codes are the 30 three-letter NBA codes, identical to Polymarket's uppercased, so the
    pair is always 6 letters and splits 3/3 (both halves must be known and distinct).
  * suffix: ``{TEAM}`` winners; ``{TEAM}{N}`` spreads / team totals; ``{N}`` totals; ``TIE`` for
    the tie strike of three-way period winners; ``{TEAM}{INITIAL}{SURNAME}{JERSEY}-{N}`` player
    props (``NYKMMCBRIDE2-15`` = Miles McBride #2, 15+ points, floor_strike 14.5). The jersey
    number is one or more digits (``ORLWCARTER34``). ``N`` is ``ceil(floor_strike)``; the line
    itself is ``floor_strike``. Surname in the suffix is the title surname with punctuation
    removed and generational suffixes dropped (``WCARTER`` for "Wendell Carter Jr.",
    ``NALEXANDERWALKER``), see nba_players.initial_surname.
  * spreads are one-sided: ``SAS9`` YES = "San Antonio wins by more than 8.5". The NO side is
    Houston +8.5. A ``HOU9`` market is a different contract.

Every structural claim is cross-checked against title / rules_primary (team city, line,
period wording, "{Away} vs {Home} Pro Basketball game originally scheduled for Mon D, YYYY",
player name vs suffix) and any disagreement raises ``KalshiParseError``.

Settlement rules (rules_primary / rules_secondary, 2026-10-20):
  * full game: no OT sentence anywhere (NBA games cannot end tied, so OT is implicit, but the
    text does not say it: recorded as "unstated"). Moneyline has no tie rule ("n/a").
  * period winners KXNBA1H/2H/1Q..4Q: THREE-WAY with a TIE strike ("neither team wins").
    Polymarket's 1H moneyline resolves 50-50 on a halftime tie: not the same contract, the
    matcher reports ``payoff:tie``.
  * OT: 1H / 1Q-3Q period-only; 2H "(including overtime)"; 4Q "(excluding overtime)".
  * player props: "If a player is inactive, or active but never takes the court, the market
    settles to a fair price" (rule key ``dnp="fair_price"``). Polymarket resolves No. Payoff
    difference, so a player prop is never ``exact``.
  * postponement: open if started within 48 h, else "all markets will resolve to a fair price"
    (moneyline, totals, team totals, period winners, player props). Period spreads/totals
    carry no such sentence: "unstated".

Series with no Polymarket family (not synced, listed for the coverage report):
KXNBAOT, KXNBARACE, KXNBAWINMARGIN, KXNBAPTSLEADER, KXNBA2D, KXNBA3D, KXNBAFIRSTBASKET.
Polymarket's odd/even and team-to-score-first have no Kalshi series (KXNBARACE is "first to
N points" with N >= 10; KXNBAFIRSTBASKET had no open markets to inspect).
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from . import nba_players, nba_teams
from .legspec import LegSpec
from .nba_poly import SPORT  # noqa: F401  (importing nba_poly registers the NBA families)


class KalshiParseError(ValueError):
    pass


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MONTH_WORDS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
_TICKER = re.compile(r"^(?P<series>KXNBA[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<pair>[A-Z]{6})-(?P<suffix>[A-Z0-9-]+)$")

# series -> (family, period, suffix kind). kinds: team | team_line | line | team_or_tie | player_line
_PERIODS = ("1H", "2H", "1Q", "2Q", "3Q", "4Q")
SERIES: dict[str, tuple[str, str, str]] = {
    "KXNBAGAME": ("moneyline", "FG", "team"),
    "KXNBASPREAD": ("spread", "FG", "team_line"),
    "KXNBATOTAL": ("total", "FG", "line"),
    "KXNBATEAMTOTAL": ("team_total", "FG", "team_line"),
    "KXNBAPTS": ("player_points", "FG", "player_line"),
    "KXNBAREB": ("player_rebounds", "FG", "player_line"),
    "KXNBAAST": ("player_assists", "FG", "player_line"),
    "KXNBA3PT": ("player_threes", "FG", "player_line"),
    "KXNBAPRA": ("player_pra", "FG", "player_line"),
    "KXNBASTL": ("player_steals", "FG", "player_line"),
    "KXNBABLK": ("player_blocks", "FG", "player_line"),
}
for _p in _PERIODS:
    SERIES[f"KXNBA{_p}SPREAD"] = ("spread", _p, "team_line")
    SERIES[f"KXNBA{_p}TOTAL"] = ("total", _p, "line")
    SERIES[f"KXNBA{_p}TEAMTOTAL"] = ("team_total", _p, "team_line")
    SERIES[f"KXNBA{_p}"] = ("moneyline", _p, "team_or_tie")

#: Kalshi game-level NBA series that exist but have no Polymarket family (not synced).
SERIES_WITHOUT_POLY_FAMILY: tuple[str, ...] = (
    "KXNBAOT", "KXNBARACE", "KXNBAWINMARGIN", "KXNBAPTSLEADER", "KXNBA2D", "KXNBA3D", "KXNBAFIRSTBASKET",
    "KXNBAPR", "KXNBAPA", "KXNBARA", "KXNBAFTM",   # player combos / free throws: listed, 0 open markets on 2026-10-20
)

_PLAYER_STAT_WORDS = {
    "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists",
    "player_threes": "made three-pointers", "player_pra": "combined points, rebounds, and assists",
    "player_steals": "steals", "player_blocks": "blocks",
}
_PLAYER_TITLE_WORDS = {
    "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists",
    "player_threes": "threes", "player_pra": "points, rebounds, and assists",
    "player_steals": "steals", "player_blocks": "blocks",
}
_PERIOD_PHRASE = {
    "1H": "1st half", "2H": "2nd half (including overtime)",
    "1Q": "1st quarter", "2Q": "2nd quarter", "3Q": "3rd quarter", "4Q": "4th quarter (excluding overtime)",
}
_OT_BY_PERIOD = {"FG": "unstated", "1H": "excluded", "2H": "included",
                 "1Q": "excluded", "2Q": "excluded", "3Q": "excluded", "4Q": "excluded"}
_DNP_FAIR = "never takes the court, the market settles to a fair price"


def kalshi_rules(family: str, period: str, rules_secondary: str | None = None) -> dict[str, str]:
    sec = rules_secondary or ""
    tie = "n/a"
    if family == "moneyline" and period != "FG":
        tie = "tie_strike"
    if "fair price" in sec and "48 hours" in sec:
        cancel, postpone = "fair_price_if_not_started_48h", "open_if_started_within_48h"
    else:
        cancel, postpone = "unstated", "unstated"
    r = {"ot": _OT_BY_PERIOD[period], "tie": tie, "cancel": cancel, "postpone": postpone}
    if tie == "tie_strike":
        r["group"] = "three_way_with_tie_strike"
    if family in nba_players.PLAYER_FAMILIES:
        r["dnp"] = "fair_price" if _DNP_FAIR in sec else "unstated"
    return r


def _ot_flag(rules: dict[str, str]) -> bool | None:
    return {"included": True, "excluded": False}.get(rules["ot"])


def split_team_pair(pair: str) -> tuple[nba_teams.Team, nba_teams.Team]:
    """Split 'GSWLAL' into (GSW, LAL). Codes are all three letters; both must be known and distinct."""
    if len(pair) != 6:
        raise KalshiParseError(f"team pair {pair!r} is not six letters")
    a, b = pair[:3], pair[3:]
    if a not in nba_teams.BY_KALSHI_CODE or b not in nba_teams.BY_KALSHI_CODE:
        raise KalshiParseError(f"team pair {pair!r} has an unknown code")
    if a == b:
        raise KalshiParseError(f"team pair {pair!r} repeats a team")
    return nba_teams.BY_KALSHI_CODE[a], nba_teams.BY_KALSHI_CODE[b]


def parse_ticker(ticker: str) -> tuple[str, str, nba_teams.Team, nba_teams.Team, str]:
    """-> (series, game_date_iso, away, home, suffix)."""
    m = _TICKER.match(ticker or "")
    if not m:
        raise KalshiParseError(f"ticker not in grammar: {ticker!r}")
    mon = _MONTHS.get(m.group("mon"))
    if mon is None:
        raise KalshiParseError(f"bad month in ticker {ticker!r}")
    date = f"20{m.group('yy')}-{mon:02d}-{m.group('dd')}"
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise KalshiParseError(f"bad date in ticker {ticker!r}") from None
    away, home = split_team_pair(m.group("pair"))
    return m.group("series"), date, away, home, m.group("suffix")


def _game_text(away: nba_teams.Team, home: nba_teams.Team, date: str) -> str:
    y, mo, d = date.split("-")
    return f"{away.kalshi_city} vs {home.kalshi_city} Pro Basketball game originally scheduled for {_MONTH_WORDS[int(mo)]} {int(d)}, {y}"


def _period_rx(period: str) -> re.Pattern[str]:
    """'in the 1st half of the {game}' / 'wins the 4th quarter (excluding overtime) of the'. Kalshi
    sometimes doubles the space before 'of'."""
    return re.compile(re.escape(_PERIOD_PHRASE[period]) + r"\s+of the ")


def parse_kalshi_nba(ticker: str, title: str, floor_strike: float | None, strike_type: str | None,
                     rules_primary: str, expected_expiration_time: str | None = None, *,
                     yes_sub_title: str | None = None, rules_secondary: str | None = None) -> LegSpec:
    """Parse the YES side of one Kalshi market. Raises KalshiParseError on anything outside
    the grammar or on any ticker/title/rules disagreement."""
    series, date, away, home, suffix = parse_ticker(ticker)
    if series not in SERIES:
        raise KalshiParseError(f"no parse rule for series {series}: {ticker}")
    family, period, kind = SERIES[series]
    rules_primary = rules_primary or ""
    title = title or ""
    game_text = _game_text(away, home, date)
    if game_text not in rules_primary:
        raise KalshiParseError(f"rules text does not name '{game_text}': {ticker}")
    if period != "FG" and not _period_rx(period).search(rules_primary):
        raise KalshiParseError(f"rules text lacks period wording for {period}: {ticker}")
    if period == "FG" and re.search(r"\b(half|quarter)\b", rules_primary):
        raise KalshiParseError(f"full-game series but rules mention a period: {ticker}")

    team: str | None = None
    line: float | None = None
    bucket: str | None = None
    side: str
    ids: dict[str, str] = {"ticker": ticker, "series": series}

    def team_from(code: str) -> nba_teams.Team:
        t = nba_teams.BY_KALSHI_CODE.get(code)
        if t is None or t not in (away, home):
            raise KalshiParseError(f"suffix team {code!r} not in game {away.code}@{home.code}: {ticker}")
        if not title.startswith(t.kalshi_city + " "):
            raise KalshiParseError(f"title {title!r} does not start with {t.kalshi_city!r}: {ticker}")
        return t

    def strike_line(int_suffix: str) -> float:
        if floor_strike is None or strike_type != "greater":
            raise KalshiParseError(f"expected floor_strike with strike_type=greater, got {floor_strike} {strike_type}: {ticker}")
        fs = float(floor_strike)
        if fs != math.floor(fs) + 0.5:
            raise KalshiParseError(f"floor_strike {fs} is not a half point: {ticker}")
        if not re.fullmatch(r"\d+", int_suffix) or int(int_suffix) != math.ceil(fs):
            raise KalshiParseError(f"suffix {int_suffix!r} != ceil(floor_strike {fs}): {ticker}")
        return fs

    if kind == "team":
        t = team_from(suffix)
        side = t.code
        if f"If {t.kalshi_city} wins the {game_text}" not in rules_primary:
            raise KalshiParseError(f"rules text lacks '{t.kalshi_city} wins the ...': {ticker}")
    elif kind == "team_or_tie":
        if suffix == "TIE":
            side = "tie"
            if not re.search(r"If neither team wins the " + re.escape(_PERIOD_PHRASE[period]), rules_primary):
                raise KalshiParseError(f"TIE strike rules lack 'neither team wins': {ticker}")
            if "tie" not in title.lower():
                raise KalshiParseError(f"TIE strike title {title!r} lacks 'tie': {ticker}")
        else:
            t = team_from(suffix)
            side = t.code
            if not re.search(rf"If {re.escape(t.kalshi_city)} wins the {re.escape(_PERIOD_PHRASE[period])}", rules_primary):
                raise KalshiParseError(f"rules text lacks '{t.kalshi_city} wins the {period}': {ticker}")
    elif kind == "team_line":
        mm = re.fullmatch(r"([A-Z]{3})(\d+)", suffix)
        if not mm:
            raise KalshiParseError(f"suffix {suffix!r} is not TEAM+int: {ticker}")
        t = team_from(mm.group(1))
        fs = strike_line(mm.group(2))
        team = t.code
        if family == "spread":
            line, side = -fs, t.code
            if f"If {t.kalshi_city} wins by more than {fs:g} points in the " not in rules_primary:
                raise KalshiParseError(f"rules text lacks spread wording: {ticker}")
            # titles: "San Antonio wins by over 8.5 points", "Houston wins the 1st half by over 7.5 points",
            # "Houston wins 1Q by over 8.5 points"
            if not re.search(rf"\bwins\b.*\bby over {re.escape(f'{fs:g}')} points$", title):
                raise KalshiParseError(f"title {title!r} lacks spread line {fs:g}: {ticker}")
        elif family == "team_total":
            line, side = fs, "over"
            if f"If {t.kalshi_city} scores more than {fs:g} points in the " not in rules_primary:
                raise KalshiParseError(f"rules text lacks team total wording: {ticker}")
            if f"over {fs:g}" not in title:
                raise KalshiParseError(f"title {title!r} lacks team total line {fs:g}: {ticker}")
        else:  # pragma: no cover
            raise KalshiParseError(f"unhandled team_line family {family}: {ticker}")
    elif kind == "line":
        fs = strike_line(suffix)
        line, side = fs, "over"
        if f"If over {fs:g} total points are scored in the " not in rules_primary:
            raise KalshiParseError(f"rules text lacks total wording: {ticker}")
        if f"Over {fs:g} points" not in title:
            raise KalshiParseError(f"title {title!r} lacks total line {fs:g}: {ticker}")
    elif kind == "player_line":
        mm = re.fullmatch(r"([A-Z]{3})([A-Z]+)(\d+)-(\d+)", suffix)
        if not mm:
            raise KalshiParseError(f"suffix {suffix!r} is not TEAM+INITIAL+SURNAME+JERSEY-N: {ticker}")
        code, name_code, jersey, n = mm.groups()
        t = nba_teams.BY_KALSHI_CODE.get(code)
        if t is None or t not in (away, home):
            raise KalshiParseError(f"suffix team {code!r} not in game {away.code}@{home.code}: {ticker}")
        fs = strike_line(n)
        tm = re.fullmatch(r"(?P<name>.+?): (?P<n>\d+)\+ (?P<stat>.+)", title)
        if not tm or tm.group("stat") != _PLAYER_TITLE_WORDS[family] or int(tm.group("n")) != math.ceil(fs):
            raise KalshiParseError(f"title {title!r} does not match player {family} grammar / line {fs:g}: {ticker}")
        name = tm.group("name")
        try:
            if nba_players.initial_surname(name) != name_code:
                raise KalshiParseError(f"title player {name!r} -> {nba_players.initial_surname(name)} != suffix {name_code}: {ticker}")
            bucket = nba_players.player_key(name)
        except nba_players.PlayerNameError as e:
            raise KalshiParseError(str(e)) from None
        if f"If {name} records {math.ceil(fs)}+ {_PLAYER_STAT_WORDS[family]} in the {game_text}" not in rules_primary:
            raise KalshiParseError(f"rules text lacks player wording for {name!r}: {ticker}")
        # team is deliberately None so the contract key agrees with Polymarket (which has no team);
        # the Kalshi team and jersey number are kept in venue_ids.
        line, side = fs, "over"
        ids.update({"player": name, "kalshi_team": t.code, "jersey": jersey})
    else:  # pragma: no cover
        raise KalshiParseError(f"unhandled kind {kind}")

    rules = kalshi_rules(family, period, rules_secondary)
    if expected_expiration_time:
        try:
            datetime.fromisoformat(expected_expiration_time.replace("Z", "+00:00"))
        except ValueError:
            raise KalshiParseError(f"bad expected_expiration_time {expected_expiration_time!r}: {ticker}") from None
    text = rules_primary if not rules_secondary else f"{rules_primary}\n\n{rules_secondary}"
    return LegSpec(venue="kalshi", sport=SPORT, away=away.code, home=home.code, game_date=date, game_date_basis="et",
                   kickoff_utc=None, family=family, period=period, team=team, line=line, bucket=bucket, side=side,
                   includes_ot=_ot_flag(rules), venue_ids=ids, rules=rules, rules_text=text)
