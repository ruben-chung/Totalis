import json
from pathlib import Path

import pytest

from rfqbot.sports.legspec import complement
from rfqbot.sports.matcher import AmbiguousGame, KalshiIndex, compare_rules, match
from rfqbot.sports.nfl_kalshi import SERIES, parse_kalshi_nfl
from rfqbot.sports.nfl_poly import parse_poly_nfl

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def index():
    idx = KalshiIndex()
    for m in json.loads((FIX / "kalshi_nfl_markets.json").read_text()):
        if m["ticker"].split("-")[0] in SERIES:
            idx.add(parse_kalshi_nfl(m["ticker"], m["title"], m["floor_strike"], m["strike_type"], m["rules_primary"],
                                     m["expected_expiration_time"], yes_sub_title=m["yes_sub_title"], rules_secondary=m["rules_secondary"]))
    return idx


def poly(slug, title, outcome, idx):
    return parse_poly_nfl(slug, title, outcome, idx)


def test_exact_spread_both_sides_and_both_side_spreads(index):
    eagles = poly("nfl-dal-phi-2026-10-27-spread-home-7pt5", "Spread: Eagles (-7.5)", "Eagles", 0)
    m = match(eagles, index)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side, m.game_date) == ("exact", "KXNFLSPREAD-26OCT26DALPHI-PHI8", "yes", "2026-10-26")
    assert not any(n.startswith("payoff:") for n in m.notes)
    assert any(n.startswith("abnormal:cancel") for n in m.notes) and any(n.startswith("info:ot") for n in m.notes)
    cowboys = poly("nfl-dal-phi-2026-10-27-spread-home-7pt5", "Spread: Eagles (-7.5)", "Cowboys", 1)
    m2 = match(cowboys, index)
    assert (m2.confidence, m2.kalshi_ticker, m2.kalshi_side) == ("exact", "KXNFLSPREAD-26OCT26DALPHI-PHI8", "no")
    assert match(complement(eagles), index).kalshi_side == "no"
    away = poly("nfl-dal-phi-2026-10-27-spread-away-7pt5", "Spread: Cowboys (-7.5)", "Cowboys", 0)
    m3 = match(away, index)
    assert (m3.confidence, m3.kalshi_ticker, m3.kalshi_side) == ("exact", "KXNFLSPREAD-26OCT26DALPHI-DAL8", "yes")


def test_exact_total_and_team_total(index):
    over = poly("nfl-dal-phi-2026-10-27-total-62pt5", "Cowboys vs. Eagles: O/U 62.5", "Over", 0)
    m = match(over, index)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNFLTOTAL-26OCT26DALPHI-63", "yes")
    under = poly("nfl-dal-phi-2026-10-27-total-62pt5", "Cowboys vs. Eagles: O/U 62.5", "Under", 1)
    assert match(under, index).kalshi_side == "no"
    assert not any(n.startswith("info:ot") for n in m.notes)  # both venues silent on OT for FG totals
    tt = poly("nfl-dal-phi-2026-10-27-team-total-phi-42pt5", "Eagles Team Total: O/U 42.5", "Over", 0)
    assert match(tt, index).kalshi_ticker == "KXNFLTEAMTOTAL-26OCT26DALPHI-PHI43"


def test_chi_sea_utc_slug_resolves_to_et_kalshi_game(index):
    sea = poly("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Seahawks", 1)
    m = match(sea, index)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side, m.game_date) == ("exact", "KXNFLGAME-26NOV02CHISEA-SEA", "yes", "2026-11-02")
    chi = poly("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Bears", 0)
    m2 = match(chi, index)
    assert (m2.kalshi_ticker, m2.kalshi_side) == ("KXNFLGAME-26NOV02CHISEA-CHI", "yes")  # prefers the YES market
    assert index.resolve_game_date(sea) == "2026-11-02"


def test_period_moneyline_is_rules_differ_against_three_way(index):
    gb = poly("nfl-gb-det-2026-10-25-1h-moneyline", "Packers vs. Lions: 1H Moneyline", "Packers", 0)
    m = match(gb, index)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("rules_differ", "KXNFL1H-26OCT25GBDET-GB", "yes")
    assert "payoff:tie: poly 50-50, kalshi tie_strike" in m.notes
    lions = poly("nfl-gb-det-2026-10-25-1h-moneyline", "Packers vs. Lions: 1H Moneyline", "Lions", 1)
    assert match(lions, index).confidence == "none"  # DET market not in fixture; GB NO is not "Lions win"


def test_shifted_line_neighbors(index):
    s = poly("nfl-dal-phi-2026-10-27-spread-home-8pt5", "Spread: Eagles (-8.5)", "Eagles", 0)
    m = match(s, index)
    assert m.confidence == "shifted_line"
    assert (m.below.kalshi_ticker, m.below.line, m.below.kalshi_side) == ("KXNFLSPREAD-26OCT26DALPHI-PHI10", -9.5, "yes")
    assert (m.above.kalshi_ticker, m.above.line) == ("KXNFLSPREAD-26OCT26DALPHI-PHI8", -7.5)
    assert m.kalshi_ticker in (m.below.kalshi_ticker, m.above.kalshi_ticker)
    assert m.notes[0].startswith("line:poly -8.5")
    c = poly("nfl-dal-phi-2026-10-27-spread-home-8pt5", "Spread: Eagles (-8.5)", "Cowboys", 1)
    assert match(c, index).below.kalshi_side == "no"
    far = poly("nfl-dal-phi-2026-10-27-spread-home-20pt5", "Spread: Eagles (-20.5)", "Eagles", 0)
    mf = match(far, index)
    assert mf.confidence == "shifted_line" and mf.below is None and mf.above.line == -9.5  # nearest line above -20.5
    # other-team ladder is not used for a team's own spread
    dal = poly("nfl-dal-phi-2026-10-27-spread-away-9pt5", "Spread: Cowboys (-9.5)", "Cowboys", 0)
    assert match(dal, index).above.kalshi_ticker == "KXNFLSPREAD-26OCT26DALPHI-DAL8"


def test_none_reasons(index):
    hsq = poly("nfl-dal-phi-2026-10-27-highest-scoring-quarter-q1", "Cowboys vs. Eagles: Highest Scoring Quarter - Q1", "Yes", 0)
    m = match(hsq, index)
    assert m.confidence == "none" and m.kalshi_ticker is None and "no kalshi markets" in m.notes[0]
    other_game = poly("nfl-kc-sea-2026-10-26-total-44pt5", "Chiefs vs. Seahawks: O/U 44.5", "Over", 0)
    assert "no kalshi game" in match(other_game, index).notes[0]
    tt = poly("nfl-dal-phi-2026-10-27-team-total-dal-20pt5", "Cowboys Team Total: O/U 20.5", "Over", 0)
    assert "only on ['PHI']" in match(tt, index).notes[0]


def test_ambiguous_game_raises():
    idx = KalshiIndex()
    base = json.loads((FIX / "kalshi_nfl_markets.json").read_text())
    m = next(x for x in base if x["ticker"] == "KXNFLGAME-26NOV02CHISEA-SEA")
    idx.add(parse_kalshi_nfl(m["ticker"], m["title"], None, m["strike_type"], m["rules_primary"], None))
    m2 = {**m, "ticker": "KXNFLGAME-26NOV03CHISEA-SEA", "rules_primary": m["rules_primary"].replace("Nov 2, 2026", "Nov 3, 2026")}
    idx.add(parse_kalshi_nfl(m2["ticker"], m2["title"], None, m2["strike_type"], m2["rules_primary"], None))
    with pytest.raises(AmbiguousGame):
        match(poly("nfl-chi-sea-2026-11-03", "Bears vs. Seahawks", "Seahawks", 1), idx)


def test_compare_rules_generic_keys():
    from rfqbot.sports.legspec import LegSpec
    a = LegSpec(venue="polymarket", sport="nfl", away="A1", home="B1", game_date="2026-01-01", family="total", period="FG",
                side="over", line=1.5, rules={"ot": "included", "dnp": "void"})
    b = LegSpec(venue="kalshi", sport="nfl", away="A1", home="B1", game_date="2026-01-01", family="total", period="FG",
                side="over", line=1.5, rules={"ot": "excluded", "dnp": "no"})
    notes = compare_rules(a, b)
    assert "payoff:ot: poly included, kalshi excluded" in notes and "payoff:dnp: poly void, kalshi no" in notes
