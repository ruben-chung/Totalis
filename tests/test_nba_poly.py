import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rfqbot.sports.legspec import canonical_key, complement
from rfqbot.sports.nba_poly import GAMMA_TYPES, PolyParseError, parse_poly_nba, poly_rules, slug_family_token

FIX = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIX / "nba_poly_rows.json").read_text())
BY_SLUG: dict[str, list[dict]] = {}
for _r in ROWS:
    BY_SLUG.setdefault(_r["slug"], []).append(_r)


def _parse(row):
    return parse_poly_nba(row["slug"], row["title"], row["outcome"], row["outcome_index"], position_id=row["position_id"])


def test_fixture_covers_every_family_and_period_with_both_outcomes():
    fams = set()
    for slug, rows in BY_SLUG.items():
        assert [r["outcome_index"] for r in rows] == [0, 1], slug
        s0, s1 = _parse(rows[0]), _parse(rows[1])
        assert s0.side != s1.side and canonical_key(s0) != canonical_key(s1)
        assert complement(s0).side == s1.side and complement(s1).side == s0.side
        fams.add((s0.family, s0.period))
    assert fams >= {("moneyline", "FG"), ("moneyline", "1H"), ("spread", "FG"), ("spread", "1H"), ("total", "FG"), ("total", "1H"),
                    ("odd_even", "FG"), ("team_to_score_first", "FG"), ("player_points", "FG"), ("player_rebounds", "FG"), ("player_assists", "FG")}
    assert set(GAMMA_TYPES.values()) == fams


def test_moneyline_bare_slug_and_et_date():
    s = _parse(BY_SLUG["nba-gsw-lal-2026-10-21"][0])
    assert (s.away, s.home, s.game_date, s.game_date_basis) == ("GSW", "LAL", "2026-10-21", "et")
    assert s.game_key == "nba|2026-10-21|GSW@LAL"
    assert s.side == "GSW" and s.family == "moneyline" and s.period == "FG" and s.line is None
    assert s.rules == {"ot": "included", "tie": "n/a", "cancel": "50-50", "postpone": "stays_open"}
    assert s.includes_ot is True
    assert s.venue_ids["outcome_label"] == "Warriors"
    # tips 2026-10-22 02:00 UTC: ET date equals the slug date, so kickoff_utc is accepted
    ko = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)
    et_slug = parse_poly_nba("nba-gsw-lal-2026-10-21", "Warriors vs. Lakers", "Lakers", 1, kickoff_utc=ko)
    assert et_slug.side == "LAL" and et_slug.game_date == "2026-10-21" and et_slug.venue_ids["slug_date"] == "et"
    # early 2025-26 convention: the slug carries the UTC date of the tip; the game is still keyed on its ET date
    utc_slug = parse_poly_nba("nba-gsw-lal-2026-10-22", "Warriors vs. Lakers", "Lakers", 1, kickoff_utc=ko)
    assert utc_slug.game_date == "2026-10-21" and utc_slug.game_date_basis == "et" and utc_slug.venue_ids["slug_date"] == "utc"
    assert canonical_key(utc_slug) == canonical_key(et_slug)
    # a real reschedule (neither date) is still an error, never a guess
    with pytest.raises(PolyParseError, match="neither the kickoff ET date"):
        parse_poly_nba("nba-gsw-lal-2026-10-20", "Warriors vs. Lakers", "Lakers", 1, kickoff_utc=ko)
    # the history's example: nba-lal-sac-2025-10-27 tipped 2025-10-27 01:00 UTC (9 pm ET on the 26th)
    h = parse_poly_nba("nba-lal-sac-2025-10-27", "Lakers vs. Kings", "Lakers", 0, kickoff_utc=datetime(2025, 10, 27, 1, 0, tzinfo=timezone.utc))
    assert h.game_date == "2025-10-26" and h.game_key == "nba|2025-10-26|LAL@SAC" and h.venue_ids["slug_date"] == "utc"
    with pytest.raises(PolyParseError, match="timezone-aware"):
        parse_poly_nba("nba-gsw-lal-2026-10-21", "Warriors vs. Lakers", "Lakers", 1, kickoff_utc=datetime(2026, 10, 22, 2))


def test_spread_home_and_away_sign_convention():
    home = [_parse(r) for r in BY_SLUG["nba-atl-orl-2026-10-21-spread-home-2pt5"]]
    assert home[0].team == "ORL" and home[0].line == -2.5 and home[0].side == "ORL" and home[1].side == "ATL"
    assert canonical_key(home[0]) == "nba|2026-10-21|ATL@ORL|FG|spread|ORL|-2.5|ORL"
    away = [_parse(r) for r in BY_SLUG["nba-bos-det-2026-10-20-spread-away-10pt5"]]
    assert away[0].team == "BOS" and away[0].line == -10.5 and away[0].side == "BOS" and away[1].side == "DET"
    assert home[0].rules["ot"] == "unstated"   # two description templates exist; see module docstring
    with pytest.raises(PolyParseError, match="title team"):
        parse_poly_nba("nba-atl-orl-2026-10-21-spread-away-2pt5", "Spread: Magic (-2.5)", "Magic", 0)
    with pytest.raises(PolyParseError, match="title line"):
        parse_poly_nba("nba-atl-orl-2026-10-21-spread-home-3pt5", "Spread: Magic (-2.5)", "Magic", 0)


def test_first_half_markets_and_tie_rule():
    ml = [_parse(r) for r in BY_SLUG["nba-dal-hou-2026-10-21-1h-moneyline"]]
    assert ml[0].period == "1H" and ml[0].side == "DAL" and ml[1].side == "HOU"
    assert ml[0].rules["tie"] == "50-50" and ml[0].rules["ot"] == "excluded" and ml[0].includes_ot is False
    assert complement(ml[0]).side == "HOU"       # two-outcome market because the tie splits
    sp = _parse(BY_SLUG["nba-dal-hou-2026-10-21-1h-spread-home-5pt5"][0])
    assert (sp.period, sp.team, sp.line, sp.side) == ("1H", "HOU", -5.5, "HOU")
    spa = _parse(BY_SLUG["nba-ind-nop-2026-10-21-1h-spread-away-1pt5"][0])
    assert (spa.period, spa.team, spa.line, spa.side) == ("1H", "IND", -1.5, "IND")
    tot = [_parse(r) for r in BY_SLUG["nba-dal-hou-2026-10-21-1h-total-118pt5"]]
    assert (tot[0].period, tot[0].line, tot[0].side, tot[1].side) == ("1H", 118.5, "over", "under")
    with pytest.raises(PolyParseError, match="title period"):
        parse_poly_nba("nba-dal-hou-2026-10-21-1h-total-118pt5", "Mavericks vs. Rockets: O/U 118.5", "Over", 0)
    with pytest.raises(PolyParseError, match="title period"):
        parse_poly_nba("nba-dal-hou-2026-10-21-total-118pt5", "Mavericks vs. Rockets: 1H O/U 118.5", "Over", 0)


def test_total_odd_even_and_team_to_score_first():
    tot = [_parse(r) for r in BY_SLUG["nba-atl-orl-2026-10-21-total-230pt5"]]
    assert tot[0].side == "over" and tot[1].side == "under" and tot[0].line == 230.5 and tot[0].rules["ot"] == "unstated"
    oe = [_parse(r) for r in BY_SLUG["nba-atl-cha-2026-10-23-odd-even"]]
    assert (oe[0].family, oe[0].side, oe[1].side) == ("odd_even", "odd", "even")
    assert complement(oe[0]).side == "even" and oe[0].rules["ot"] == "included"
    tf = [_parse(r) for r in BY_SLUG["nba-atl-cha-2026-10-23-team-to-score-first"]]
    assert (tf[0].family, tf[0].side, tf[1].side, tf[0].team) == ("team_to_score_first", "ATL", "CHA", None)
    assert complement(tf[0]).side == "CHA"
    assert tf[0].rules["ot"] == "n/a"


def test_player_props_yes_no_means_over_under():
    rows = BY_SLUG["nba-okc-sas-2026-10-20-points-deaaron-fox-14pt5"]
    yes, no = _parse(rows[0]), _parse(rows[1])
    assert rows[0]["outcome"] == "Yes" and yes.side == "over" and no.side == "under"
    assert yes.family == "player_points" and yes.line == 14.5 and yes.team is None and yes.bucket == "d.fox"
    assert yes.venue_ids["player"] == "De'Aaron Fox" and yes.venue_ids["outcome_label"] == "Yes"
    assert yes.rules == {"ot": "included", "tie": "n/a", "cancel": "50-50", "postpone": "stays_open", "dnp": "no"}
    assert canonical_key(yes) == "nba|2026-10-20|OKC@SAS|FG|player_points|d.fox|14.5|over"
    # team markets use Over/Under; a player prop with Over/Under labels is an error, and vice versa
    with pytest.raises(PolyParseError, match="outcome 'Over'"):
        parse_poly_nba(rows[0]["slug"], rows[0]["title"], "Over", 0)
    with pytest.raises(PolyParseError, match="outcome 'Yes'"):
        parse_poly_nba("nba-atl-orl-2026-10-21-total-230pt5", "Hawks vs. Magic: O/U 230.5", "Yes", 0)


def test_player_name_cross_check_against_slug():
    s = _parse(BY_SLUG["nba-cha-bkn-2026-10-21-points-michael-porter-jr-21pt5"][0])
    assert s.bucket == "m.porter" and s.venue_ids["player"] == "Michael Porter Jr."
    s = _parse(BY_SLUG["nba-dal-hou-2026-10-21-points-alperen-sengun-18pt5"][0])
    assert s.bucket == "a.sengun"
    s = _parse(BY_SLUG["nba-gsw-lal-2026-10-21-rebounds-luka-doncic-7pt5"][0])
    assert s.bucket == "l.doncic" and s.family == "player_rebounds"
    with pytest.raises(PolyParseError, match="slugifies to"):
        parse_poly_nba("nba-cha-bkn-2026-10-21-points-michael-porter-jr-21pt5", "Michael Porter: Points O/U 21.5", "Yes", 0)
    with pytest.raises(PolyParseError, match="title stat"):
        parse_poly_nba("nba-cha-bkn-2026-10-21-points-michael-porter-jr-21pt5", "Michael Porter Jr.: Rebounds O/U 21.5", "Yes", 0)


@pytest.mark.parametrize("slug,title,outcome,idx,msg", [
    ("nfl-atl-cha-2026-10-23", "Hawks vs. Hornets", "Hawks", 0, "slug not in grammar"),
    ("nba-atl-xyz-2026-10-23", "Hawks vs. Hornets", "Hawks", 0, "unknown NBA poly slug code"),
    ("nba-atl-atl-2026-10-23", "Hawks vs. Hawks", "Hawks", 0, "away == home"),
    ("nba-atl-cha-2026-13-23", "Hawks vs. Hornets", "Hawks", 0, "bad date"),
    ("nba-atl-cha-2026-10-23-2h-total-100pt5", "Hawks vs. Hornets: 2H O/U 100.5", "Over", 0, "family not recognised"),
    ("nba-atl-cha-2026-10-23-total-100", "Hawks vs. Hornets: O/U 100", "Over", 0, "family not recognised"),
    ("nba-atl-cha-2026-10-23", "Hornets vs. Hawks", "Hornets", 0, "title teams"),
    ("nba-atl-cha-2026-10-23", "Hawks vs. Hornets", "Hornets", 0, "outcome 'Hornets' at index 0"),
    ("nba-atl-cha-2026-10-23", "Hawks vs. Hornets", "Hawks", 2, "outcome 'Hawks' at index 2"),
    ("nba-atl-cha-2026-10-23-odd-even", "Hawks vs. Hornets: Odd/Even", "Odd", 0, "title does not match"),
    ("nba-atl-cha-2026-10-23-points-trae-young-25pt5", "Trae Young: Points O/U 25.5 ", "Yes", 0, "title does not match"),
    ("nba-por-sac-2026-10-23", "Blazers vs. Kings", "Blazers", 0, "title teams"),
])
def test_parse_errors(slug, title, outcome, idx, msg):
    with pytest.raises(PolyParseError, match=msg):
        parse_poly_nba(slug, title, outcome, idx)


def test_multiword_and_numeric_nicknames():
    s = parse_poly_nba("nba-por-sac-2026-10-23", "Trail Blazers vs. Kings", "Trail Blazers", 0)
    assert s.side == "POR"
    s = parse_poly_nba("nba-phi-nyk-2026-10-20-spread-away-1pt5", "Spread: 76ers (-1.5)", "Knicks", 1)
    assert s.team == "PHI" and s.side == "NYK"
    s = parse_poly_nba("nba-por-sac-2026-10-23-total-220pt5", "Trail Blazers vs. Kings: O/U 220.5", "Under", 1)
    assert s.side == "under"


def test_rules_table_and_family_token():
    assert poly_rules("moneyline", "1H")["tie"] == "50-50"
    assert poly_rules("moneyline", "FG")["tie"] == "n/a"
    assert poly_rules("player_assists", "FG")["dnp"] == "no"
    assert "dnp" not in poly_rules("total", "FG")
    assert slug_family_token("nba-atl-orl-2026-10-21-points-cj-mccollum-3pt5") == "points-<player>"
    assert slug_family_token("nba-atl-orl-2026-10-21-1h-spread-home-5pt5") == "1h-spread-home"
    assert slug_family_token("nba-atl-orl-2026-10-21") == "<bare>"
    assert slug_family_token("garbage") == "<bare>"


HIST = json.loads((FIX / "nba_poly_hist_rows.json").read_text())


def test_old_player_prop_title_grammar_from_history():
    """Dec 2025 - Jan 2026 player props were titled "X: Points Over 13.5" (outcomes still Yes/No, 7,697 rows
    in data/history.db, none titled "Under"). They must parse to the same LegSpec as the current "O/U" form."""
    assert len(HIST) >= 5
    for r in HIST:
        outs = json.loads(r["outcomes"])
        assert outs == ["Yes", "No"] and " Over " in r["title"]
        yes = parse_poly_nba(r["slug"], r["title"], "Yes", 0)
        no = parse_poly_nba(r["slug"], r["title"], "No", 1)
        assert (yes.side, no.side) == ("over", "under") and yes.line == float(r["line"]) and yes.family.startswith("player_")
        new_title = r["title"].replace(" Over ", " O/U ")
        assert canonical_key(parse_poly_nba(r["slug"], new_title, "Yes", 0)) == canonical_key(yes)
    s = parse_poly_nba("nba-nop-orl-2026-01-11-points-wendell-carter-jr-13pt5", "Wendell Carter Jr.: Points Over 13.5", "No", 1)
    assert s.bucket == "w.carter" and s.venue_ids["player"] == "Wendell Carter Jr." and s.game_key == "nba|2026-01-11|NOP@ORL"
    # labels other than Yes/No, an "Under" title, or an O/U-style label on this grammar are still errors
    with pytest.raises(PolyParseError, match="outcome 'Over'"):
        parse_poly_nba(HIST[0]["slug"], HIST[0]["title"], "Over", 0)
    with pytest.raises(PolyParseError, match="title does not match"):
        parse_poly_nba(HIST[0]["slug"], HIST[0]["title"].replace(" Over ", " Under "), "Yes", 0)
