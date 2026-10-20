"""Kalshi NFL game-market parser: public-API market fields -> LegSpec.

Ticker grammar (verified on 3,222 open markets across 40 KXNFL* series, 2026-10-20):
    {SERIES}-{YY}{MON}{DD}{AWAY}{HOME}-{SUFFIX}
  * the date is the US/Eastern game date (Bears at Seahawks, Mon Nov 2 8:15pm ET, is
    ``26NOV02CHISEA``; Polymarket's slug for the same game is ``nfl-chi-sea-2026-11-03``).
  * team order is away-home, same as Polymarket (all 28 live pairs agree; rules text says
    "Chicago vs Seattle"). Codes are concatenated without a separator and are 2-3 letters,
    so the pair is split by trying every cut and requiring exactly one valid split.
  * Kalshi codes differ from Poly for JAC (Jaguars), LAR (Rams), LAC (Chargers).
  * suffix: ``{TEAM}`` for winners, ``{TEAM}{N}`` for spreads / team totals / team yards,
    ``{N}`` for totals, ``TIE`` for the tie strike of 3-way period winners, ``Y`` for yes/no
    props. ``N`` is ``ceil(floor_strike)`` (7.5 -> 8); the line itself is ``floor_strike``.
  * spreads are one-sided: ``PHI8`` YES = "Philadelphia wins by more than 7.5". There is a
    separate ``DAL8`` market; it is a different contract, not the complement. The NO side
    of ``PHI8`` is Dallas +7.5. Both teams get a ladder of lines; Kalshi skips 11.5, 12.5,
    15.5, 18.5, 19.5 (ladder is 1.5..10.5, 13.5, 14.5, 16.5, 17.5, 20.5, 23.5, 27.5).

The parser returns the **YES** side; ``legspec.complement`` gives NO. Every structural
claim is cross-checked against the title / yes_sub_title / rules_primary text (team city,
line, period wording, game date, "{Away} vs {Home}") and any disagreement raises.

Settlement rules (from rules_primary / rules_secondary, 2026-10-20):
  * KXNFLGAME: tie -> $0.50 each side (same as Poly 50-50). Postponed: open if the game
    starts within 48 h, else "resolves to a fair price" (Poly: stays open indefinitely,
    cancellation -> 50-50).
  * period winners (KXNFL1H, KXNFL2H, KXNFL1Q..4Q): THREE-WAY. Team strikes resolve No on a
    tie and the TIE strike Yes. Poly period moneylines resolve 50-50 on a tie. Not the
    same contract.
  * OT: 1H "only points scored during the 1st half"; 2H "including overtime"; 1Q-3Q quarter
    only; 4Q "excluding overtime" (explicit). Full-game spreads/totals/team totals do not
    mention OT. Safety: "Safeties recorded in overtime count".
  * spreads / totals / team totals carry no postponement or cancellation text at all.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from . import nfl_teams
from .legspec import LegSpec


class KalshiParseError(ValueError):
    pass


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_MONTH_WORDS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
_TICKER = re.compile(r"^(?P<series>KXNFL[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<pair>[A-Z]+)-(?P<suffix>[A-Z0-9]+)$")

# series -> (family, period, suffix kind). kinds: team | team_line | line | team_or_tie | yes
_P = {"1H": "1H", "2H": "2H", "1Q": "1Q", "2Q": "2Q", "3Q": "3Q", "4Q": "4Q"}
SERIES: dict[str, tuple[str, str, str]] = {
    "KXNFLGAME": ("moneyline", "FG", "team"),
    "KXNFLSPREAD": ("spread", "FG", "team_line"),
    "KXNFLTOTAL": ("total", "FG", "line"),
    "KXNFLTEAMTOTAL": ("team_total", "FG", "team_line"),
    "KXNFLTIE": ("exact_margin", "FG", "yes"),
    "KXNFLSFTY": ("safety", "FG", "yes"),
    "KXNFLSAFETY": ("safety", "FG", "yes"),
    "KXNFLTEAMYDS": ("team_off_yards", "FG", "team_line"),
    "KXNFLTEAMTD": ("team_tds", "FG", "team_line"),
    "KXNFL2PTCONV": ("two_pt_conversions", "FG", "line"),
}
for _p in _P:
    SERIES[f"KXNFL{_p}SPREAD"] = ("spread", _p, "team_line")
    SERIES[f"KXNFL{_p}TOTAL"] = ("total", _p, "line")
    SERIES[f"KXNFL{_p}TEAMTOTAL"] = ("team_total", _p, "team_line")
    SERIES[f"KXNFL{_p}"] = ("moneyline", _p, "team_or_tie")
    SERIES[f"KXNFL{_p}WINNER"] = ("moneyline", _p, "team_or_tie")
for _q in ("1Q", "2Q", "3Q", "4Q"):
    SERIES[f"KXNFL{_q}BTTS"] = ("btts", _q, "yes")

#: Kalshi game-level NFL series that exist but have no Polymarket family (not synced).
SERIES_WITHOUT_POLY_FAMILY: tuple[str, ...] = (
    "KXNFL1HFT", "KXNFLOT", "KXNFLOTWIN", "KXNFLRACE", "KXNFLBOTH", "KXNFLEQBTTS", "KXNFLNOSCOREQ",
    "KXNFLCOMEBACK", "KXNFLLARGELEAD", "KXNFLLARGESTLEAD", "KXNFLGAMEFG", "KXNFLTEAMFG", "KXNFLGAMETD",
    "KXNFLGAMETO", "KXNFLTOTALTD", "KXNFLFIRSTTDTEAM", "KXNFLLEADCHANGE", "KXNFLHIGHSCOREQ",
)
#: Poly families with no Kalshi series at all (as of 2026-10-20): exact margin bands,
#: highest scoring quarter (series KXNFLHIGHSCOREQ exists with unknown ticker grammar and 0 open markets).

_PERIOD_RULES_WORDS = {
    "FG": None,
    "1H": re.compile(r"\b1st half\b", re.I),
    "2H": re.compile(r"\b2nd half \(including overtime\)", re.I),
    "1Q": re.compile(r"\b1st quarter\b", re.I),
    "2Q": re.compile(r"\b2nd quarter\b", re.I),
    "3Q": re.compile(r"\b3rd quarter\b", re.I),
    "4Q": re.compile(r"\b4th quarter \(excluding overtime\)", re.I),
}
_OT_BY_PERIOD = {"FG": "unstated", "1H": "excluded", "2H": "included",
                 "1Q": "excluded", "2Q": "excluded", "3Q": "excluded", "4Q": "excluded"}
_CANCEL_48H = frozenset({"moneyline", "exact_margin", "safety", "btts", "team_off_yards"})


def kalshi_rules(family: str, period: str, rules_secondary: str | None = None) -> dict[str, str]:
    ot = _OT_BY_PERIOD[period]
    if family == "safety":
        ot = "included"
    sec = rules_secondary or ""
    tie = "n/a"
    if family == "moneyline":
        tie = "50-50" if period == "FG" else "tie_strike"
    if "fair price" in sec:
        cancel, postpone = "fair_price_if_not_started_48h", "open_if_started_within_48h"
    elif family in _CANCEL_48H:
        cancel, postpone = "fair_price_if_not_started_48h", "open_if_started_within_48h"
    else:
        cancel, postpone = "unstated", "unstated"
    r = {"ot": ot, "tie": tie, "cancel": cancel, "postpone": postpone}
    if tie == "tie_strike":
        r["group"] = "three_way_with_tie_strike"
    return r


def _ot_flag(rules: dict[str, str]) -> bool | None:
    return {"included": True, "excluded": False}.get(rules["ot"])


def split_team_pair(pair: str) -> tuple[nfl_teams.Team, nfl_teams.Team]:
    """Split e.g. 'LACLAR' into (LAC, LAR). Exactly one valid cut must exist."""
    cuts = []
    for i in range(2, len(pair) - 1):
        a, b = pair[:i], pair[i:]
        if a in nfl_teams.BY_KALSHI_CODE and b in nfl_teams.BY_KALSHI_CODE and a != b:
            cuts.append((nfl_teams.BY_KALSHI_CODE[a], nfl_teams.BY_KALSHI_CODE[b]))
    if len(cuts) != 1:
        raise KalshiParseError(f"team pair {pair!r} has {len(cuts)} valid splits")
    return cuts[0]


def parse_ticker(ticker: str) -> tuple[str, str, nfl_teams.Team, nfl_teams.Team, str]:
    """-> (series, game_date_iso, away, home, suffix)."""
    m = _TICKER.match(ticker or "")
    if not m:
        raise KalshiParseError(f"ticker not in grammar: {ticker!r}")
    mon = _MONTHS.get(m.group("mon"))
    if mon is None:
        raise KalshiParseError(f"bad month in ticker {ticker!r}")
    date = f"20{m.group('yy')}-{mon:02d}-{m.group('dd')}"
    datetime.strptime(date, "%Y-%m-%d")  # validates day
    away, home = split_team_pair(m.group("pair"))
    return m.group("series"), date, away, home, m.group("suffix")


def _check_game_text(text: str, away: nfl_teams.Team, home: nfl_teams.Team, date: str, ticker: str) -> None:
    """rules_primary names the game as '{Away city} vs {Home city} ... originally scheduled for Mon D, YYYY'."""
    if f"{away.kalshi_city} vs {home.kalshi_city}" not in text:
        raise KalshiParseError(f"rules text does not name '{away.kalshi_city} vs {home.kalshi_city}': {ticker}")
    y, mo, d = date.split("-")
    expected = f"{_MONTH_WORDS[int(mo)]} {int(d)}, {y}"
    if expected not in text:
        raise KalshiParseError(f"rules text date != ticker date {expected}: {ticker}")


def parse_kalshi_nfl(ticker: str, title: str, floor_strike: float | None, strike_type: str | None,
                     rules_primary: str, expected_expiration_time: str | None = None, *,
                     yes_sub_title: str | None = None, rules_secondary: str | None = None) -> LegSpec:
    """Parse the YES side of one Kalshi market. Raises KalshiParseError on anything outside
    the grammar or on any ticker/title/rules disagreement."""
    series, date, away, home, suffix = parse_ticker(ticker)
    if series not in SERIES:
        raise KalshiParseError(f"no parse rule for series {series}: {ticker}")
    family, period, kind = SERIES[series]
    rules_primary = rules_primary or ""
    _check_game_text(rules_primary, away, home, date, ticker)
    pw = _PERIOD_RULES_WORDS[period]
    if pw is not None and not pw.search(rules_primary):
        raise KalshiParseError(f"rules text lacks period wording for {period}: {ticker}")

    team: str | None = None
    line: float | None = None
    bucket: str | None = None
    side: str

    def team_from(code: str) -> nfl_teams.Team:
        t = nfl_teams.BY_KALSHI_CODE.get(code)
        if t is None or t not in (away, home):
            raise KalshiParseError(f"suffix team {code!r} not in game {away.code}@{home.code}: {ticker}")
        if not (title or "").startswith(t.kalshi_city) and not (title or "").startswith(f"Will {t.kalshi_city} "):
            raise KalshiParseError(f"title {title!r} does not start with {t.kalshi_city!r}: {ticker}")
        return t

    def strike_line() -> float:
        if floor_strike is None or strike_type != "greater":
            raise KalshiParseError(f"expected floor_strike with strike_type=greater, got {floor_strike} {strike_type}: {ticker}")
        fs = float(floor_strike)
        if fs != math.floor(fs) + 0.5:
            raise KalshiParseError(f"floor_strike {fs} is not a half point: {ticker}")
        n = re.search(r"(\d+)$", suffix)
        if not n or int(n.group(1)) != math.ceil(fs):
            raise KalshiParseError(f"suffix {suffix!r} != ceil(floor_strike {fs}): {ticker}")
        if f"{fs:g}" not in (title or "") and f"{math.ceil(fs)}+" not in (title or ""):
            raise KalshiParseError(f"title {title!r} does not state line {fs:g}: {ticker}")
        return fs

    if kind == "team":
        t = team_from(suffix)
        team, side = None, t.code
        if f"{t.kalshi_city} wins" not in rules_primary:
            raise KalshiParseError(f"rules text lacks '{t.kalshi_city} wins': {ticker}")
    elif kind == "team_or_tie":
        if suffix == "TIE":
            side = "tie"
            if "neither team wins" not in rules_primary:
                raise KalshiParseError(f"TIE strike rules lack 'neither team wins': {ticker}")
        else:
            t = team_from(suffix)
            side = t.code
            if f"{t.kalshi_city} wins" not in rules_primary:
                raise KalshiParseError(f"rules text lacks '{t.kalshi_city} wins': {ticker}")
    elif kind == "team_line":
        code = re.match(r"^([A-Z]+)\d+$", suffix)
        if not code:
            raise KalshiParseError(f"suffix {suffix!r} is not TEAM+int: {ticker}")
        t = team_from(code.group(1))
        fs = strike_line()
        team = t.code
        if family == "spread":
            line, side = -fs, t.code
            if f"{t.kalshi_city} wins by more than {fs:g} points" not in rules_primary:
                raise KalshiParseError(f"rules text lacks spread wording: {ticker}")
        else:
            line, side = fs, "over"
            if family == "team_total" and f"{t.kalshi_city} scores over {fs:g} points" not in rules_primary:
                raise KalshiParseError(f"rules text lacks team total wording: {ticker}")
            if family == "team_off_yards" and f"{math.ceil(fs)}+ total yards" not in rules_primary:
                raise KalshiParseError(f"rules text lacks team yards wording: {ticker}")
    elif kind == "line":
        if not re.match(r"^\d+$", suffix):
            raise KalshiParseError(f"suffix {suffix!r} is not an int: {ticker}")
        fs = strike_line()
        line, side = fs, "over"
        if family == "total" and f"collectively score more than {fs:g} points" not in rules_primary:
            raise KalshiParseError(f"rules text lacks total wording: {ticker}")
    elif kind == "yes":
        if suffix != "Y":
            raise KalshiParseError(f"expected suffix Y for {series}, got {suffix!r}: {ticker}")
        side = "yes"
        if family == "exact_margin":
            bucket = "tie"
            if "ends in a tie" not in rules_primary:
                raise KalshiParseError(f"tie rules wording missing: {ticker}")
        elif family == "safety" and "safety" not in rules_primary.lower():
            raise KalshiParseError(f"safety rules wording missing: {ticker}")
        elif family == "btts" and "both score points" not in rules_primary:
            raise KalshiParseError(f"btts rules wording missing: {ticker}")
    else:  # pragma: no cover
        raise KalshiParseError(f"unhandled kind {kind}")

    rules = kalshi_rules(family, period, rules_secondary)
    ids = {"ticker": ticker, "series": series}
    kickoff = None
    if expected_expiration_time:
        try:
            kickoff = datetime.fromisoformat(expected_expiration_time.replace("Z", "+00:00"))
        except ValueError:
            raise KalshiParseError(f"bad expected_expiration_time {expected_expiration_time!r}: {ticker}") from None
    text = rules_primary if not rules_secondary else f"{rules_primary}\n\n{rules_secondary}"
    return LegSpec(venue="kalshi", sport="nfl", away=away.code, home=home.code, game_date=date, game_date_basis="et",
                   kickoff_utc=None, family=family, period=period, team=team, line=line, bucket=bucket, side=side,
                   includes_ot=_ot_flag(rules), venue_ids=ids, rules=rules, rules_text=text)
