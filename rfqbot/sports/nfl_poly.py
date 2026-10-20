"""Polymarket NFL market parser: catalog fields -> LegSpec. Pure and offline.

Slug grammar (verified on all 12,479 NFL markets in the 2026-10-20 catalog):
    nfl-{away}-{home}-{YYYY-MM-DD}[-{period}][-{family}...]
  * first code is the AWAY team, second is HOME (Gamma ``teams[].ordering`` confirms).
  * the date is the UTC date of kickoff (Gamma ``gameStartTime``), so ``game_date_basis`` is
    ``"utc_slug"`` unless ``kickoff_utc`` is passed, in which case the ET date is used.
  * lines are ``{N}pt5`` (always a half point, never an integer).
  * ``spread-home-7pt5`` / ``spread-away-7pt5`` are DIFFERENT markets ("Spread: Home (-7.5)" and
    "Spread: Away (-7.5)"), not two sides of one market. The team named in the title is the
    favourite in that market; Gamma ``line`` is ``-7.5`` for both, i.e. the sign of Gamma's
    line carries no home/away information, only the slug token does.
  * ``exact-margin-long-*`` is the AWAY team winning, ``exact-margin-short-*`` the HOME team
    (verified over all 126 band markets: long <-> away 70/70, short <-> home 56/56). Bands differ:
    away 1-6, 7-12, 13-18, 19-24, 25+; home 1-6, 7-13, 14-20, 21+.

Title grammar is checked independently and any disagreement with the slug (team, period, line,
outcome label) is a ``PolyParseError`` rather than a choice.

Settlement rules come from the Gamma descriptions read on 2026-10-20 (one per family and
period, see tests/fixtures). They are per-family constants here because the catalog row does
not carry the description; ``nfl-coverage --gamma`` re-checks them against live descriptions.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import nfl_teams
from .legspec import LegSpec, et_date_candidates  # noqa: F401  (re-exported)

ET = ZoneInfo("America/New_York")


class PolyParseError(ValueError):
    pass


_SLUG = re.compile(r"^nfl-(?P<away>[a-z]+)-(?P<home>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<rest>.+))?$")
_PERIOD_TOKENS = {"1h": "1H", "2h": "2H", "1q": "1Q", "2q": "2Q", "3q": "3Q", "4q": "4Q"}
_PERIOD_TITLE = {v: v for v in _PERIOD_TOKENS.values()}
_LINE = r"(?P<line>\d+)pt5"
_TEAM = r"(?P<team>[a-z]+)"

# (regex over the slug remainder, family). Group names: period, side, team, line, band, q
_REST_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:(?P<period>1h|2h|1q|2q|3q|4q)-)?moneyline$"), "moneyline"),
    (re.compile(rf"^(?:(?P<period>1h|2h|1q|2q|3q|4q)-)?spread-(?P<side>home|away)-{_LINE}$"), "spread"),
    (re.compile(rf"^(?:(?P<period>1h|2h|1q|2q|3q|4q)-)?total-{_LINE}$"), "total"),
    (re.compile(rf"^(?:(?P<period>1h|2h)-)?team-total-{_TEAM}-{_LINE}$"), "team_total"),
    (re.compile(rf"^team-off-yards-{_TEAM}-{_LINE}$"), "team_off_yards"),
    (re.compile(rf"^team-tds-{_TEAM}-{_LINE}$"), "team_tds"),
    (re.compile(rf"^two-pt-conversions-{_LINE}$"), "two_pt_conversions"),
    (re.compile(r"^safety$"), "safety"),
    (re.compile(r"^highest-scoring-quarter-(?P<q>q1|q2|q3|q4|other)$"), "highest_scoring_quarter"),
    (re.compile(r"^exact-margin-(?P<band>tie|(?P<who>long|short)-(?P<lo>\d+)-(?P<hi>\d+|plus))$"), "exact_margin"),
    (re.compile(r"^(?P<period>1q|2q|3q|4q)-btts-points$"), "btts"),
]

_NICK = r"(?P<{name}>[A-Za-z0-9]+)"
_T_VS = rf"{_NICK.format(name='t_away')} vs\. {_NICK.format(name='t_home')}"
_T_PER = r"(?:(?P<t_period>1H|2H|1Q|2Q|3Q|4Q) )?"
_T_LINE = r"(?P<t_line>\d+\.5)"
_TITLE_RULES: dict[str, re.Pattern[str]] = {
    "moneyline": re.compile(rf"^{_T_VS}(?:: (?P<t_period>1H|2H|1Q|2Q|3Q|4Q) Moneyline)?$"),
    "spread": re.compile(rf"^{_T_PER}Spread: {_NICK.format(name='t_team')} \(-{_T_LINE}\)$"),
    "total": re.compile(rf"^{_T_VS}: {_T_PER}O/U {_T_LINE}$"),
    "team_total": re.compile(rf"^{_NICK.format(name='t_team')} {_T_PER}Team Total: O/U {_T_LINE}$"),
    "team_off_yards": re.compile(rf"^{_NICK.format(name='t_team')} Total Offensive Yards: O/U {_T_LINE}$"),
    "team_tds": re.compile(rf"^{_NICK.format(name='t_team')} Total Touchdowns: O/U {_T_LINE}$"),
    "two_pt_conversions": re.compile(rf"^{_T_VS}: Total Two-Point Conversions O/U {_T_LINE}$"),
    "safety": re.compile(rf"^{_T_VS}: Safety\?$"),
    "highest_scoring_quarter": re.compile(rf"^{_T_VS}: Highest Scoring Quarter - (?P<t_q>Q1|Q2|Q3|Q4|Other)$"),
    "exact_margin": re.compile(rf"^Exact Margin: (?:(?P<t_tie>Tie)|{_NICK.format(name='t_team')} by (?P<t_lo>\d+)(?:-(?P<t_hi>\d+)|(?P<t_plus>\+)))$"),
    "btts": re.compile(rf"^{_T_VS}: Both Teams to Score Points - (?P<t_period>1Q|2Q|3Q|4Q)$"),
}

# --- settlement rules as written in the Gamma descriptions (2026-10-20) ---------------------
# ot: included | excluded | unstated      tie: 50-50 | n/a | other_yes
# cancel: 50-50 | other_yes               postpone: stays_open
_OT_BY_PERIOD = {"FG": "included", "1H": "excluded", "2H": "included",
                 "1Q": "excluded", "2Q": "excluded", "3Q": "excluded", "4Q": "excluded"}
_OT_UNSTATED_FG = frozenset({"total", "team_total"})      # FG descriptions say "in this game" only


def poly_rules(family: str, period: str) -> dict[str, str]:
    ot = _OT_BY_PERIOD[period]
    if period == "FG" and family in _OT_UNSTATED_FG:
        ot = "unstated"
    if family == "highest_scoring_quarter":
        return {"ot": "excluded", "tie": "other_yes", "cancel": "other_yes", "postpone": "stays_open",
                "group": "neg_risk_exactly_one_yes"}
    tie = "50-50" if family == "moneyline" else "n/a"
    return {"ot": ot, "tie": tie, "cancel": "50-50", "postpone": "stays_open"}


def _ot_flag(rules: dict[str, str]) -> bool | None:
    return {"included": True, "excluded": False}.get(rules["ot"])


def _half_line(n: str) -> float:
    return int(n) + 0.5


PLAYER_PROP_TOKENS = ("recyd", "rec", "pyd", "ptd", "ryd", "rtd", "pcomp", "patt", "pint", "lrec", "lrush", "anytime-td",
                      "2plus-td", "3plus-td", "team-first-td", "first-td-scorer", "last-td-scorer")
_PROP = re.compile(r"^nfl-[a-z]+-[a-z]+-\d{4}-\d{2}-\d{2}-(" + "|".join(re.escape(t) for t in PLAYER_PROP_TOKENS) + r")(?:-|$)")


def player_prop_family(slug: str) -> str | None:
    """The prop token (recyd, anytime-td, ...) when the slug is an NFL player prop (seen in the
    catalog from 2026-10-20 evening), else None. Props are not score-derived and are not parsed
    into LegSpecs; callers report them separately from genuine parse failures."""
    m = _PROP.match(slug or "")
    return m.group(1) if m else None


def slug_family_token(slug: str) -> str:
    """Rough family label for grouping unparsed rows (digits and team codes elided)."""
    m = _SLUG.match(slug or "")
    rest = (m.group("rest") if m else None) or "<bare>"
    rest = re.sub(r"-\d+pt5$", "", rest)
    rest = re.sub(r"-\d+(-\d+|-plus)?$", "", rest)
    return rest


def parse_poly_nfl(slug: str, title: str, outcome: str, outcome_index: int, *,
                   position_id: str | None = None, kickoff_utc: datetime | None = None) -> LegSpec:
    """Parse one catalog position row. Raises PolyParseError on anything not in the grammar
    or on any slug/title/outcome disagreement."""
    m = _SLUG.match(slug or "")
    if not m:
        raise PolyParseError(f"slug not in grammar: {slug!r}")
    try:
        away = nfl_teams.by_poly_slug(m.group("away"))
        home = nfl_teams.by_poly_slug(m.group("home"))
    except nfl_teams.UnknownTeam as e:
        raise PolyParseError(str(e)) from None
    if away is home:
        raise PolyParseError(f"away == home in {slug!r}")
    slug_date = m.group("date")
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

    def check_team() -> nfl_teams.Team:
        try:
            team = nfl_teams.by_poly_slug(g["team"])
        except nfl_teams.UnknownTeam as e:
            raise PolyParseError(str(e)) from None
        if team not in (away, home):
            raise PolyParseError(f"team {team.code} not in game {away.code}@{home.code}: {slug}")
        if t.get("t_team") != team.nickname:
            raise PolyParseError(f"title team {t.get('t_team')!r} != slug team {team.code}: {slug}")
        return team

    def binary_side(first: str, second: str) -> str:
        expected = (first, second)[outcome_index] if outcome_index in (0, 1) else None
        if expected is None or outcome != expected:
            raise PolyParseError(f"outcome {outcome!r} at index {outcome_index} not in [{first}, {second}]: {slug}")
        return outcome

    team: str | None = None
    line: float | None = None
    bucket: str | None = None

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
    elif family in ("total", "two_pt_conversions"):
        check_vs()
        if family == "total":
            check_period()
        line = check_line()
        side = binary_side("Over", "Under").lower()
    elif family in ("team_total", "team_off_yards", "team_tds"):
        if family == "team_total":
            check_period()
        team = check_team().code
        line = check_line()
        side = binary_side("Over", "Under").lower()
    elif family == "safety":
        check_vs()
        side = binary_side("Yes", "No").lower()
    elif family == "highest_scoring_quarter":
        check_vs()
        bucket = g["q"].upper() if g["q"] != "other" else "other"
        if t["t_q"] != ("Other" if bucket == "other" else bucket):
            raise PolyParseError(f"title quarter {t['t_q']} != slug {g['q']}: {slug}")
        side = binary_side("Yes", "No").lower()
    elif family == "exact_margin":
        if g["band"] == "tie":
            if not t.get("t_tie"):
                raise PolyParseError(f"slug says tie, title does not: {title!r}")
            bucket = "tie"
        else:
            winner = away if g["who"] == "long" else home
            if t.get("t_team") != winner.nickname:
                raise PolyParseError(f"title team {t.get('t_team')!r} != slug {g['who']} team {winner.code}: {slug}")
            if g["hi"] == "plus":
                if not t.get("t_plus") or t["t_lo"] != g["lo"]:
                    raise PolyParseError(f"title band != slug band: {title!r} / {slug}")
                bucket = f"{g['lo']}+"
            else:
                if t.get("t_lo") != g["lo"] or t.get("t_hi") != g["hi"]:
                    raise PolyParseError(f"title band != slug band: {title!r} / {slug}")
                bucket = f"{g['lo']}-{g['hi']}"
            team = winner.code
        side = binary_side("Yes", "No").lower()
    elif family == "btts":
        check_vs(); check_period()
        side = binary_side("Yes", "No").lower()
    else:  # pragma: no cover
        raise PolyParseError(f"unhandled family {family}")

    rules = poly_rules(family, period)
    if kickoff_utc is not None:
        if kickoff_utc.tzinfo is None:
            raise PolyParseError("kickoff_utc must be timezone-aware")
        game_date, basis = kickoff_utc.astimezone(ET).date().isoformat(), "et"
    else:
        game_date, basis = slug_date, "utc_slug"
    ids = {"slug": slug, "outcome_label": outcome}
    if position_id:
        ids["position_id"] = str(position_id)
    return LegSpec(venue="polymarket", sport="nfl", away=away.code, home=home.code, game_date=game_date,
                   game_date_basis=basis, kickoff_utc=kickoff_utc, family=family, period=period, team=team,
                   line=line, bucket=bucket, side=side, includes_ot=_ot_flag(rules), venue_ids=ids, rules=rules)


# Gamma ``sportsMarketType`` -> (family, period), used by ``coverage --gamma``.
GAMMA_TYPES: dict[str, tuple[str, str]] = {
    "moneyline": ("moneyline", "FG"), "spreads": ("spread", "FG"), "totals": ("total", "FG"),
    "team_totals": ("team_total", "FG"), "exact_margin": ("exact_margin", "FG"), "safety": ("safety", "FG"),
    "two_point_conversions": ("two_pt_conversions", "FG"), "team_offensive_yards": ("team_off_yards", "FG"),
    "team_touchdowns": ("team_tds", "FG"), "highest_scoring_quarter": ("highest_scoring_quarter", "FG"),
}
for _p, _w in (("1H", "first_half"), ("2H", "second_half"), ("1Q", "q1"), ("2Q", "q2"), ("3Q", "q3"), ("4Q", "q4")):
    GAMMA_TYPES[f"{_w}_moneyline"] = ("moneyline", _p)
    GAMMA_TYPES[f"{_w}_spreads"] = ("spread", _p)
    GAMMA_TYPES[f"{_w}_totals"] = ("total", _p)
    GAMMA_TYPES[f"{_w}_team_totals"] = ("team_total", _p)
    GAMMA_TYPES[f"{_w}_both_teams_to_score_points"] = ("btts", _p)
