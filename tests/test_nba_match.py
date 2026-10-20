"""Polymarket NBA legs against Kalshi NBA markets through the shared matcher."""
import json
from pathlib import Path

import pytest

from rfqbot.sports.matcher import KalshiIndex, match
from rfqbot.sports.nba_kalshi import SERIES, parse_kalshi_nba
from rfqbot.sports.nba_poly import parse_poly_nba

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def idx():
    specs = []
    for m in json.loads((FIX / "nba_kalshi_markets.json").read_text()):
        if m["ticker"].split("-")[0] in SERIES:
            specs.append(parse_kalshi_nba(m["ticker"], m["title"], m["floor_strike"], m["strike_type"], m["rules_primary"],
                                          m["expected_expiration_time"], yes_sub_title=m["yes_sub_title"], rules_secondary=m["rules_secondary"]))
    return KalshiIndex(specs)


def _poly(slug, title, outcome, i):
    return parse_poly_nba(slug, title, outcome, i)


def test_moneyline_et_date_no_offset(idx):
    # Poly slug date 2026-10-21 (ET; tips 02:00 UTC on the 22nd) == Kalshi 26OCT21: exact, no day shift needed
    m = match(_poly("nba-gsw-lal-2026-10-21", "Warriors vs. Lakers", "Warriors", 0), idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side, m.game_date) == ("exact", "KXNBAGAME-26OCT21GSWLAL-GSW", "yes", "2026-10-21")
    assert not any(n.startswith("payoff:") for n in m.notes)
    assert "info:ot: poly included, kalshi unstated" in m.notes
    assert "abnormal:cancel: poly 50-50, kalshi fair_price_if_not_started_48h" in m.notes
    m = match(_poly("nba-gsw-lal-2026-10-21", "Warriors vs. Lakers", "Lakers", 1), idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBAGAME-26OCT21GSWLAL-GSW", "no") or \
           (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBAGAME-26OCT21GSWLAL-LAL", "yes")
    # a slug dated one day later must NOT silently match the 21st
    m = match(_poly("nba-gsw-lal-2026-10-22", "Warriors vs. Lakers", "Warriors", 0), idx)
    assert m.confidence == "none"


def test_first_half_moneyline_tie_is_payoff_difference(idx):
    # Poly 1H moneyline pays 0.5 on a halftime tie; Kalshi KXNBA1H team strike pays 0 (separate TIE market)
    m = match(_poly("nba-dal-hou-2026-10-21-1h-moneyline", "Mavericks vs. Rockets: 1H Moneyline", "Mavericks", 0), idx)
    assert m.confidence == "rules_differ" and m.kalshi_ticker == "KXNBA1H-26OCT21DALHOU-DAL" and m.kalshi_side == "yes"
    assert "payoff:tie: poly 50-50, kalshi tie_strike" in m.notes
    assert "info:group: poly binary, kalshi three_way_with_tie_strike" in m.notes
    # the Rockets outcome is not the NO of the Mavericks strike (NO also pays on a tie) and there is no HOU strike in the fixture
    m = match(_poly("nba-dal-hou-2026-10-21-1h-moneyline", "Mavericks vs. Rockets: 1H Moneyline", "Rockets", 1), idx)
    assert m.confidence == "none"


def test_spread_one_sided_and_shifted(idx):
    # Kalshi fixture has SAS9 (-8.5) for HOU@SAS on Oct 23; Poly fixture has no such slug, build the legs directly
    cover = _poly("nba-hou-sas-2026-10-23-spread-home-8pt5", "Spread: Spurs (-8.5)", "Spurs", 0)
    m = match(cover, idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBASPREAD-26OCT23HOUSAS-SAS9", "yes")
    dog = _poly("nba-hou-sas-2026-10-23-spread-home-8pt5", "Spread: Spurs (-8.5)", "Rockets", 1)
    m = match(dog, idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBASPREAD-26OCT23HOUSAS-SAS9", "no")
    # Rockets favoured (-8.5) is a different contract: HOU9 is not in the index -> none, never SAS9
    m = match(_poly("nba-hou-sas-2026-10-23-spread-away-8pt5", "Spread: Rockets (-8.5)", "Rockets", 0), idx)
    assert m.confidence == "none" and m.kalshi_ticker is None
    # neighbouring line -> shifted_line to the nearest Kalshi strike on the same team
    m = match(_poly("nba-hou-sas-2026-10-23-spread-home-7pt5", "Spread: Spurs (-7.5)", "Spurs", 0), idx)
    assert m.confidence == "shifted_line" and m.kalshi_ticker == "KXNBASPREAD-26OCT23HOUSAS-SAS9" and m.kalshi_side == "yes"
    assert m.notes[0].startswith("line:poly -7.5, kalshi -8.5")


def test_total_exact(idx):
    m = match(_poly("nba-hou-sas-2026-10-23-total-253pt5", "Rockets vs. Spurs: O/U 253.5", "Under", 1), idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBATOTAL-26OCT23HOUSAS-254", "no")
    assert "abnormal:cancel: poly 50-50, kalshi fair_price_if_not_started_48h" in m.notes
    assert not [n for n in m.notes if n.startswith("info:ot")]      # both unstated for full-game totals


def test_first_half_total_and_spread(idx):
    m = match(_poly("nba-dal-hou-2026-10-21-1h-total-136pt5", "Mavericks vs. Rockets: 1H O/U 136.5", "Over", 0), idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("exact", "KXNBA1HTOTAL-26OCT21DALHOU-137", "yes")
    assert not any(n.startswith("payoff:") for n in m.notes)        # both halftime-only
    m = match(_poly("nba-dal-hou-2026-10-21-1h-spread-home-5pt5", "1H Spread: Rockets (-5.5)", "Mavericks", 1), idx)
    assert m.confidence == "shifted_line" and m.kalshi_ticker == "KXNBA1HSPREAD-26OCT21DALHOU-HOU8" and m.kalshi_side == "no"


def test_families_without_kalshi_counterpart(idx):
    for slug, title, outcome in [("nba-atl-cha-2026-10-23-odd-even", "Hawks vs. Hornets: Odd/Even Score", "Odd"),
                                 ("nba-atl-cha-2026-10-23-team-to-score-first", "Hawks vs. Hornets: Team to Score First", "Hawks")]:
        m = match(_poly(slug, title, outcome, 0), idx)
        assert m.confidence == "none" and m.notes[0].startswith("none:no kalshi markets for")


def test_player_prop_never_exact_because_dnp_rule_differs(idx):
    yes = _poly("nba-okc-sas-2026-10-20-points-deaaron-fox-14pt5", "De'Aaron Fox: Points O/U 14.5", "Yes", 0)
    m = match(yes, idx)
    assert (m.confidence, m.kalshi_ticker, m.kalshi_side) == ("rules_differ", "KXNBAPTS-26OCT20OKCSAS-SASDFOX4-15", "yes")
    assert "payoff:dnp: poly no, kalshi fair_price" in m.notes
    assert "info:ot: poly included, kalshi unstated" in m.notes
    no = _poly("nba-okc-sas-2026-10-20-points-deaaron-fox-14pt5", "De'Aaron Fox: Points O/U 14.5", "No", 1)
    m = match(no, idx)
    assert (m.confidence, m.kalshi_side) == ("rules_differ", "no")
    # Porter Jr. 21.5: Kalshi lists 24.5 / 29.5 -> shifted to 24.5, still carrying the dnp note
    m = match(_poly("nba-cha-bkn-2026-10-21-points-michael-porter-jr-21pt5", "Michael Porter Jr.: Points O/U 21.5", "Yes", 0), idx)
    assert m.confidence == "shifted_line" and m.kalshi_ticker == "KXNBAPTS-26OCT21CHABKN-BKNMPORTER17-25"
    assert "payoff:dnp: poly no, kalshi fair_price" in m.notes
    # a player with no Kalshi market in that game at all
    m = match(_poly("nba-cha-bkn-2026-10-21-assists-dayron-sharpe-2pt5", "Day'Ron Sharpe: Assists O/U 2.5", "Yes", 0), idx)
    assert m.confidence == "none"
