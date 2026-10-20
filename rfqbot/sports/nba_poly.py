"""Polymarket NBA market parser: catalog fields -> LegSpec. Pure and offline.

Slug grammar (verified on all 739 NBA markets / 1,478 position rows in the 2026-10-20 catalog):
    nba-{away}-{home}-{YYYY-MM-DD}[-1h][-{family}][-{player-slug}][-{N}pt5]
  * first code is the AWAY team, second is HOME (Gamma: ``nba-atl-orl-...-spread-home-2pt5``
    is "Spread: Magic (-2.5)", Magic are the home side).
  * the date is the **Eastern** game date, unlike the NFL (verified by the planning session
    on all 52 games: 25 tip after 00:00 UTC and every one carries the ET date, e.g.
    ``nba-gsw-lal-2026-10-21`` tips 2026-10-22 02:00 UTC). It therefore equals the Kalshi
    ticker date, so ``game_date_basis`` is ``"et"`` straight from the slug.
    EXCEPTION (data/history.db, Gamma gameStartTime): between 2025-10-27 and 2025-11-11, 92 of
    the season's games carry the **UTC** date of the tip (``nba-lal-sac-2025-10-27`` tipped
    2025-10-27 01:00 UTC = 9 pm ET on the 26th), mixed with ET-dated slugs in the same weeks;
    from mid-November 2025 on every slug is ET-dated. So when ``kickoff_utc`` is supplied the
    slug date may equal either the ET date or the UTC date of the tip; ``game_date`` is always
    the ET date and ``venue_ids["slug_date"]`` records which convention the slug used. Any other
    date (the two real reschedules in the history, 52 and 66 days later) is a parse error.
    Without a kickoff the slug date is taken as ET, which is right for everything after
    2025-11-11; the matcher's game resolution is exact-date for basis ``"et"``.
  * bare game slug = full-game moneyline. Period token seen: ``1h`` only.
  * ``spread-home-{N}pt5`` / ``spread-away-{N}pt5`` are different markets; the title names the
    favourite, Gamma ``line`` is negative for the favourite in both, so the sign carries no
    home/away information: only the slug token does. Lines are always ``{N}pt5``.
  * ``odd-even`` (outcomes Odd/Even), ``team-to-score-first`` (outcomes = nicknames).
  * player props: ``points|rebounds|assists-{player-slug}-{N}pt5``, title "{Name}: Points O/U
    16.5", outcomes **Yes/No** where Yes = over. Team markets use Over/Under; this parser maps
    both to side over/under and records the raw label in ``venue_ids["outcome_label"]``.
    ``bucket`` is ``nba_players.player_key`` (initial.surname, "m.porter"); the display name is
    kept in ``venue_ids["player"]``.

Title grammar is checked independently and any disagreement with the slug (nickname vs code,
period, line, player slug) is a ``PolyParseError`` rather than a choice.

Settlement rules from the Gamma descriptions read on 2026-10-20 (tests/fixtures/nba_gamma_markets.json):
  * full game moneyline, odd/even: "final score including any overtime periods".
  * full game spread: TWO templates exist ("wins by more than 2.5 points ... including any
    overtime periods" and "win the game by 11 or more points" with no OT sentence), on both
    spread-home and spread-away. The catalog row cannot tell them apart so ot is "unstated"
    here; ``nba_coverage --gamma`` reports how many descriptions do state it.
  * full game total: "combine to score 231 or more points in this game", OT not mentioned.
  * 1H markets: "score at halftime only"; 1H moneyline "tied at halftime -> 50-50".
  * team to score first: "first point of the game scored only, inclusive of free throws".
  * player props: "entire game including all overtime periods"; "listed as inactive or
    otherwise does not take the court at any point -> No" (rule key ``dnp="no"``).
  * all: postponed stays open until played; cancelled with no make-up -> 50-50.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import nba_players, nba_teams
from .legspec import LegSpec, register_family

ET = ZoneInfo("America/New_York")
SPORT = "nba"

register_family("odd_even", ("odd", "even"))
register_family("team_to_score_first", "team")
for _f in nba_players.PLAYER_FAMILIES:
    register_family(_f, "over_under")


class PolyParseError(ValueError):
    pass


_SLUG = re.compile(r"^nba-(?P<away>[a-z]+)-(?P<home>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<rest>.+))?$")
_PERIOD_TOKENS = {"1h": "1H"}
_LINE = r"(?P<line>\d+)pt5"
_PLAYER_STATS = {"points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists"}

_REST_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:(?P<period>1h)-)?moneyline$"), "moneyline"),
    (re.compile(rf"^(?:(?P<period>1h)-)?spread-(?P<side>home|away)-{_LINE}$"), "spread"),
    (re.compile(rf"^(?:(?P<period>1h)-)?total-{_LINE}$"), "total"),
    (re.compile(r"^odd-even$"), "odd_even"),
    (re.compile(r"^team-to-score-first$"), "team_to_score_first"),
    (re.compile(rf"^(?P<stat>points|rebounds|assists)-(?P<player>[a-z0-9]+(?:-[a-z0-9]+)*)-{_LINE}$"), "player"),
]

_NICK = r"(?P<{name}>[A-Za-z0-9]+(?: [A-Za-z0-9]+)*?)"     # "Trail Blazers", "76ers"
_T_VS = rf"{_NICK.format(name='t_away')} vs\. {_NICK.format(name='t_home')}"
_T_LINE = r"(?P<t_line>\d+\.5)"
_TITLE_RULES: dict[str, re.Pattern[str]] = {
    "moneyline": re.compile(rf"^{_T_VS}(?:: (?P<t_period>1H) Moneyline)?$"),
    "spread": re.compile(rf"^(?:(?P<t_period>1H) )?Spread: {_NICK.format(name='t_team')} \(-{_T_LINE}\)$"),
    "total": re.compile(rf"^{_T_VS}: (?:(?P<t_period>1H) )?O/U {_T_LINE}$"),
    "odd_even": re.compile(rf"^{_T_VS}: Odd/Even Score$"),
    "team_to_score_first": re.compile(rf"^{_T_VS}: Team to Score First$"),
    # "X: Points O/U 16.5" (current) and "X: Points Over 13.5" (Dec 2025 - Jan 2026 history); both have outcomes
    # Yes/No with Yes = over the line (Gamma description of the old form: "Yes if X scores more than 13.5")
    "player": re.compile(rf"^(?P<t_player>.+?): (?P<t_stat>Points|Rebounds|Assists) (?:O/U|Over) {_T_LINE}$"),
}

# Gamma sportsMarketType -> (family, period), for nba_coverage --gamma
GAMMA_TYPES: dict[str, tuple[str, str]] = {
    "moneyline": ("moneyline", "FG"), "spreads": ("spread", "FG"), "totals": ("total", "FG"),
    "first_half_moneyline": ("moneyline", "1H"), "first_half_spreads": ("spread", "1H"), "first_half_totals": ("total", "1H"),
    "basketball_odd_even": ("odd_even", "FG"), "basketball_team_to_score_first": ("team_to_score_first", "FG"),
    "points": ("player_points", "FG"), "rebounds": ("player_rebounds", "FG"), "assists": ("player_assists", "FG"),
}

# --- settlement rules as written in the Gamma descriptions (2026-10-20) ---------------------
# ot: included | excluded | unstated | n/a   tie: 50-50 | n/a   dnp (player props): no
# cancel: 50-50   postpone: stays_open
_OT_FG = {"moneyline": "included", "odd_even": "included", "spread": "unstated", "total": "unstated",
          "team_to_score_first": "n/a"}


def poly_rules(family: str, period: str) -> dict[str, str]:
    if period == "1H":
        ot = "excluded"
    elif family in nba_players.PLAYER_FAMILIES:
        ot = "included"
    else:
        ot = _OT_FG[family]
    tie = "50-50" if (family == "moneyline" and period == "1H") else "n/a"
    r = {"ot": ot, "tie": tie, "cancel": "50-50", "postpone": "stays_open"}
    if family in nba_players.PLAYER_FAMILIES:
        r["dnp"] = "no"
    return r


def _ot_flag(rules: dict[str, str]) -> bool | None:
    return {"included": True, "excluded": False}.get(rules["ot"])


def _half_line(n: str) -> float:
    return int(n) + 0.5


def slug_family_token(slug: str) -> str:
    """Rough family label for grouping unparsed rows (lines and player slugs elided)."""
    m = _SLUG.match(slug or "")
    rest = (m.group("rest") if m else None) or "<bare>"
    rest = re.sub(r"-\d+pt5$", "", rest)
    rest = re.sub(r"^(points|rebounds|assists)-.*$", r"\1-<player>", rest)
    return rest


def parse_poly_nba(slug: str, title: str, outcome: str, outcome_index: int, *,
                   position_id: str | None = None, kickoff_utc: datetime | None = None) -> LegSpec:
    """Parse one catalog position row. Raises PolyParseError on anything not in the grammar
    or on any slug/title/outcome disagreement."""
    m = _SLUG.match(slug or "")
    if not m:
        raise PolyParseError(f"slug not in grammar: {slug!r}")
    try:
        away = nba_teams.by_poly_slug(m.group("away"))
        home = nba_teams.by_poly_slug(m.group("home"))
    except nba_teams.UnknownTeam as e:
        raise PolyParseError(str(e)) from None
    if away is home:
        raise PolyParseError(f"away == home in {slug!r}")
    slug_date = m.group("date")
    try:
        datetime.strptime(slug_date, "%Y-%m-%d")
    except ValueError:
        raise PolyParseError(f"bad date in slug {slug!r}") from None
    rest = m.group("rest") or "moneyline"

    for rx, family in _REST_RULES:
        rm = rx.match(rest)
        if rm:
            break
    else:
        raise PolyParseError(f"slug family not recognised: {slug!r}")
    g = rm.groupdict()
    period = _PERIOD_TOKENS[g["period"]] if g.get("period") else "FG"

    tm = _TITLE_RULES[family].match(title or "")
    if not tm:
        raise PolyParseError(f"title does not match {family} grammar: {title!r} (slug {slug})")
    t = tm.groupdict()

    def check_vs() -> None:
        if t.get("t_away") != away.nickname or t.get("t_home") != home.nickname:
            raise PolyParseError(f"title teams {t.get('t_away')!r} vs {t.get('t_home')!r} != slug {away.code}@{home.code}: {slug}")

    def check_period() -> None:
        tp = t.get("t_period") or "FG"
        if tp != period:
            raise PolyParseError(f"title period {tp} != slug period {period}: {slug}")

    def check_line() -> float:
        line = _half_line(g["line"])
        if float(t["t_line"]) != line:
            raise PolyParseError(f"title line {t['t_line']} != slug line {line}: {slug}")
        return line

    def binary_side(first: str, second: str) -> str:
        expected = (first, second)[outcome_index] if outcome_index in (0, 1) else None
        if expected is None or outcome != expected:
            raise PolyParseError(f"outcome {outcome!r} at index {outcome_index} not in [{first}, {second}]: {slug}")
        return outcome

    team: str | None = None
    line: float | None = None
    bucket: str | None = None
    ids: dict[str, str] = {"slug": slug, "outcome_label": outcome}

    if family == "moneyline":
        check_vs(); check_period()
        side = {away.nickname: away.code, home.nickname: home.code}[binary_side(away.nickname, home.nickname)]
    elif family == "spread":
        check_period()
        fav = home if g["side"] == "home" else away
        other = away if fav is home else home
        if t["t_team"] != fav.nickname:
            raise PolyParseError(f"title team {t['t_team']!r} != slug {g['side']} team {fav.code}: {slug}")
        line = -check_line()
        team = fav.code
        side = {fav.nickname: fav.code, other.nickname: other.code}[binary_side(fav.nickname, other.nickname)]
    elif family == "total":
        check_vs(); check_period()
        line = check_line()
        side = binary_side("Over", "Under").lower()
    elif family == "odd_even":
        check_vs()
        side = binary_side("Odd", "Even").lower()
    elif family == "team_to_score_first":
        check_vs()
        side = {away.nickname: away.code, home.nickname: home.code}[binary_side(away.nickname, home.nickname)]
    elif family == "player":
        family = _PLAYER_STATS[g["stat"]]
        if t["t_stat"].lower() != g["stat"]:
            raise PolyParseError(f"title stat {t['t_stat']} != slug stat {g['stat']}: {slug}")
        name = t["t_player"]
        try:
            if nba_players.poly_slug(name) != g["player"]:
                raise PolyParseError(f"title player {name!r} slugifies to {nba_players.poly_slug(name)!r} != slug token {g['player']!r}: {slug}")
            bucket = nba_players.player_key(name)
        except nba_players.PlayerNameError as e:
            raise PolyParseError(str(e)) from None
        line = check_line()
        side = {"Yes": "over", "No": "under"}[binary_side("Yes", "No")]
        ids["player"] = name
    else:  # pragma: no cover
        raise PolyParseError(f"unhandled family {family}")

    game_date = slug_date
    if kickoff_utc is not None:
        if kickoff_utc.tzinfo is None:
            raise PolyParseError("kickoff_utc must be timezone-aware")
        et_date = kickoff_utc.astimezone(ET).date().isoformat()
        utc_date = kickoff_utc.astimezone(timezone.utc).date().isoformat()
        if slug_date == et_date:
            ids["slug_date"] = "et"
        elif slug_date == utc_date:
            ids["slug_date"] = "utc"        # 2025-10-27 .. 2025-11-11 convention; the game is keyed on its ET date
            game_date = et_date
        else:
            raise PolyParseError(f"slug date {slug_date} is neither the kickoff ET date {et_date} nor its UTC date {utc_date}: {slug}")
    if position_id:
        ids["position_id"] = str(position_id)
    rules = poly_rules(family, period)
    return LegSpec(venue="polymarket", sport=SPORT, away=away.code, home=home.code, game_date=game_date,
                   game_date_basis="et", kickoff_utc=kickoff_utc, family=family, period=period, team=team,
                   line=line, bucket=bucket, side=side, includes_ot=_ot_flag(rules), venue_ids=ids, rules=rules)
