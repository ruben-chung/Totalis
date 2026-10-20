import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rfqbot.sports.legspec import canonical_key, complement
from rfqbot.sports.nfl_poly import PolyParseError, et_date_candidates, parse_poly_nfl, player_prop_family, poly_rules, slug_family_token

FIX = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIX / "poly_nfl_rows.json").read_text())


def parse_row(r, **kw):
    return parse_poly_nfl(r["slug"], r["title"], r["outcome"], r["outcome_index"], position_id=r["position_id"], **kw)


def test_fixture_covers_every_family_and_period_and_both_outcomes_are_complements():
    by_slug = {}
    for r in ROWS:
        by_slug.setdefault(r["slug"], []).append(r)
    seen = Counter()
    for slug, rs in by_slug.items():
        assert [r["outcome_index"] for r in rs] == [0, 1], slug
        a, b = parse_row(rs[0]), parse_row(rs[1])
        assert complement(a) == b and complement(b) == a, slug
        assert a.venue_ids["position_id"] == rs[0]["position_id"]
        assert a.game_date_basis == "utc_slug" and a.sport == "nfl" and a.venue == "polymarket"
        seen[(a.family, a.period)] += 1
    assert set(seen) >= {("moneyline", "FG"), ("moneyline", "1H"), ("moneyline", "4Q"), ("spread", "FG"), ("spread", "1H"),
                         ("spread", "2H"), ("spread", "1Q"), ("spread", "4Q"), ("total", "FG"), ("total", "2H"), ("total", "3Q"),
                         ("team_total", "FG"), ("team_total", "1H"), ("team_total", "2H"), ("exact_margin", "FG"),
                         ("highest_scoring_quarter", "FG"), ("btts", "1Q"), ("btts", "4Q"), ("safety", "FG"),
                         ("team_off_yards", "FG"), ("team_tds", "FG"), ("two_pt_conversions", "FG")}


def test_spread_sign_convention_home_and_away():
    # slug nfl-ind-min-...-spread-home-7pt5, title "Spread: Vikings (-7.5)": second slug code is HOME
    s = parse_poly_nfl("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (-7.5)", "Vikings", 0)
    assert (s.away, s.home, s.team, s.line, s.side) == ("IND", "MIN", "MIN", -7.5, "MIN")
    assert canonical_key(s) == "nfl|2026-10-25|IND@MIN|FG|spread|MIN|-7.5|MIN"
    c = parse_poly_nfl("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (-7.5)", "Colts", 1)
    assert c.side == "IND" and c.team == "MIN" and c.line == -7.5 and complement(s) == c
    # spread-away: the AWAY team is the favourite; line still negative for that team
    a = parse_poly_nfl("nfl-dal-phi-2026-10-27-spread-away-7pt5", "Spread: Cowboys (-7.5)", "Cowboys", 0)
    assert (a.team, a.line, a.side) == ("DAL", -7.5, "DAL")
    h = parse_poly_nfl("nfl-dal-phi-2026-10-27-spread-home-7pt5", "Spread: Eagles (-7.5)", "Eagles", 0)
    assert (h.team, h.line, h.side) == ("PHI", -7.5, "PHI")
    assert canonical_key(a) != canonical_key(h)  # both-side spreads are different contracts
    assert s.includes_ot is True and s.rules["ot"] == "included"


def test_periods_and_ot_rules():
    q = parse_poly_nfl("nfl-cin-bal-2026-10-25-1q-spread-away-2pt5", "1Q Spread: Bengals (-2.5)", "Bengals", 0)
    assert q.period == "1Q" and q.includes_ot is False
    h2 = parse_poly_nfl("nfl-cin-bal-2026-10-25-2h-total-16pt5", "Bengals vs. Ravens: 2H O/U 16.5", "Under", 1)
    assert h2.period == "2H" and h2.includes_ot is True and h2.side == "under" and h2.line == 16.5
    fg = parse_poly_nfl("nfl-cin-bal-2026-10-25-total-24pt5", "Bengals vs. Ravens: O/U 24.5", "Over", 0)
    assert fg.includes_ot is None and fg.rules["ot"] == "unstated"
    ml = parse_poly_nfl("nfl-cin-bal-2026-10-25-1h-moneyline", "Bengals vs. Ravens: 1H Moneyline", "Ravens", 1)
    assert ml.side == "BAL" and ml.rules["tie"] == "50-50" and ml.period == "1H"
    hsq = parse_poly_nfl("nfl-cin-bal-2026-10-25-highest-scoring-quarter-other", "Bengals vs. Ravens: Highest Scoring Quarter - Other", "Yes", 0)
    assert hsq.bucket == "other" and hsq.rules["cancel"] == "other_yes" and hsq.includes_ot is False
    assert poly_rules("safety", "FG")["ot"] == "included"


def test_exact_margin_long_is_away_short_is_home():
    lo = parse_poly_nfl("nfl-cin-bal-2026-10-25-exact-margin-long-1-6", "Exact Margin: Bengals by 1-6", "Yes", 0)
    assert (lo.team, lo.bucket, lo.side) == ("CIN", "1-6", "yes")
    sh = parse_poly_nfl("nfl-cin-bal-2026-10-25-exact-margin-short-21-plus", "Exact Margin: Ravens by 21+", "No", 1)
    assert (sh.team, sh.bucket, sh.side) == ("BAL", "21+", "no")
    tie = parse_poly_nfl("nfl-cin-bal-2026-10-25-exact-margin-tie", "Exact Margin: Tie", "Yes", 0)
    assert tie.team is None and tie.bucket == "tie"


def test_chi_sea_utc_slug_vs_et_kickoff():
    s = parse_poly_nfl("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Bears", 0)
    assert s.game_date == "2026-11-03" and s.game_date_basis == "utc_slug"
    assert et_date_candidates(s) == ("2026-11-03", "2026-11-02")
    ko = datetime(2026, 11, 3, 1, 15, tzinfo=timezone.utc)
    e = parse_poly_nfl("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Bears", 0, kickoff_utc=ko)
    assert e.game_date == "2026-11-02" and e.game_date_basis == "et" and e.game_key == "nfl|2026-11-02|CHI@SEA"
    assert et_date_candidates(e) == ("2026-11-02",)
    day = parse_poly_nfl("nfl-cin-bal-2026-10-25", "Bengals vs. Ravens", "Bengals", 0, kickoff_utc=datetime(2026, 10, 25, 17, tzinfo=timezone.utc))
    assert day.game_date == "2026-10-25"
    with pytest.raises(PolyParseError):
        parse_poly_nfl("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Bears", 0, kickoff_utc=datetime(2026, 11, 3, 1, 15))


@pytest.mark.parametrize("slug,title,outcome,idx", [
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Colts (-7.5)", "Colts", 0),       # title team != slug side
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (-8.5)", "Vikings", 0),   # line mismatch
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "1H Spread: Vikings (-7.5)", "Vikings", 0),  # period mismatch
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (+7.5)", "Vikings", 0),   # positive line not in grammar
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (-7.5)", "Colts", 0),     # outcome at wrong index
    ("nfl-ind-min-2026-10-25-spread-home-7pt5", "Spread: Vikings (-7.5)", "Vikings", 2),
    ("nfl-ind-min-2026-10-25-total-44pt5", "Vikings vs. Colts: O/U 44.5", "Over", 0),      # title order != slug order
    ("nfl-ind-min-2026-10-25-spread-home-7", "Spread: Vikings (-7)", "Vikings", 0),         # integer line
    ("nfl-ind-lar-2026-10-25", "Colts vs. Rams", "Colts", 0),                               # 'lar' is not a Poly slug code
    ("nfl-ind-min-2026-10-25-team-total-phi-20pt5", "Eagles Team Total: O/U 20.5", "Over", 0),  # team not in game
    ("nfl-ind-min-2026-10-25-player-props", "Colts vs. Vikings", "Colts", 0),               # unknown family
    ("nba-ind-min-2026-10-25", "Pacers vs. Timberwolves", "Pacers", 0),
    ("nfl-cin-bal-2026-10-25-exact-margin-long-1-6", "Exact Margin: Ravens by 1-6", "Yes", 0),  # long must be away
    ("nfl-cin-bal-2026-10-25-highest-scoring-quarter-q1", "Bengals vs. Ravens: Highest Scoring Quarter - Q2", "Yes", 0),
])
def test_disagreements_are_errors_not_guesses(slug, title, outcome, idx):
    with pytest.raises(PolyParseError):
        parse_poly_nfl(slug, title, outcome, idx)


def test_player_prop_detection():
    assert player_prop_family("nfl-tb-car-2026-10-25-anytime-td-emeka-egbuka") == "anytime-td"
    assert player_prop_family("nfl-cin-bal-2026-10-25-ryd-chase-brown-29pt5") == "ryd"
    assert player_prop_family("nfl-cle-ten-2026-10-25-rec-denzel-boston-2pt5") == "rec"
    assert player_prop_family("nfl-cin-bal-2026-10-25-team-total-cin-10pt5") is None
    assert player_prop_family("nfl-cin-bal-2026-10-25") is None
    with pytest.raises(PolyParseError):
        parse_poly_nfl("nfl-tb-car-2026-10-25-anytime-td-emeka-egbuka", "Emeka Egbuka: Anytime Touchdown", "Yes", 0)


def test_slug_family_token():
    assert slug_family_token("nfl-ari-dal-2026-11-01-1h-spread-away-10pt5") == "1h-spread-away"
    assert slug_family_token("nfl-cin-bal-2026-10-25-exact-margin-long-1-6") == "exact-margin-long"
    assert slug_family_token("nfl-cin-bal-2026-10-25") == "<bare>"


DB = Path(__file__).resolve().parent.parent / "data" / "rfq.db"


@pytest.mark.skipif(not DB.exists(), reason="data/rfq.db not present")
def test_every_nfl_row_in_catalog_parses():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    rows = conn.execute("SELECT position_id, slug, title, outcome, outcome_index FROM markets WHERE slug LIKE 'nfl-%'").fetchall()
    conn.close()
    assert rows, "no NFL rows in catalog"
    failures = []
    keys = set()
    props = 0
    for pid, slug, title, outcome, idx in rows:
        if player_prop_family(slug):          # player props are not modeled, not a parse failure
            props += 1
            continue
        try:
            s = parse_poly_nfl(slug, title, outcome, idx, position_id=pid)
            keys.add(canonical_key(s))
        except PolyParseError as e:
            failures.append((slug, title, outcome, str(e)))
    assert not failures, failures[:10]
    assert len(keys) == len(rows) - props, "canonical keys must be unique per position row"
