import json
from pathlib import Path

import pytest

from rfqbot.sports.legspec import canonical_key, complement
from rfqbot.sports.nba_kalshi import (SERIES, SERIES_WITHOUT_POLY_FAMILY, KalshiParseError, kalshi_rules,
                                      parse_kalshi_nba, parse_ticker, split_team_pair)

FIX = Path(__file__).parent / "fixtures"
MARKETS = json.loads((FIX / "nba_kalshi_markets.json").read_text())
BY_TICKER = {m["ticker"]: m for m in MARKETS}


def _parse(m, **over):
    m = {**m, **over}
    return parse_kalshi_nba(m["ticker"], m["title"], m["floor_strike"], m["strike_type"], m["rules_primary"],
                            m["expected_expiration_time"], yes_sub_title=m["yes_sub_title"], rules_secondary=m["rules_secondary"])


def test_fixture_parses_every_listed_series_and_rejects_the_rest():
    seen = set()
    for m in MARKETS:
        series = m["ticker"].split("-")[0]
        if series in SERIES:
            s = _parse(m)
            assert s.venue == "kalshi" and s.sport == "nba" and s.game_date_basis == "et"
            assert s.venue_ids["ticker"] == m["ticker"] and s.rules_text.startswith(m["rules_primary"])
            seen.add(series)
        else:
            assert series in SERIES_WITHOUT_POLY_FAMILY
            with pytest.raises(KalshiParseError, match="no parse rule"):
                _parse(m)
    assert len(seen) >= 20, sorted(seen)


def test_ticker_grammar_and_et_date():
    assert parse_ticker("KXNBAGAME-26OCT21GSWLAL-GSW")[:2] == ("KXNBAGAME", "2026-10-21")
    series, date, away, home, suffix = parse_ticker("KXNBAPTS-26OCT20PHINYK-NYKMMCBRIDE2-15")
    assert (series, date, away.code, home.code, suffix) == ("KXNBAPTS", "2026-10-20", "PHI", "NYK", "NYKMMCBRIDE2-15")
    assert split_team_pair("LACLAL")[0].code == "LAC"
    for bad in ("KXNBAGAME-26OCT21GSWLA-GSW", "KXNBAGAME-26OCT21GSWGSW-GSW", "KXNBAGAME-26OCT21GSWXXX-GSW",
                "KXNBAGAME-26OCX21GSWLAL-GSW", "KXNBAGAME-26OCT41GSWLAL-GSW", "KXNFLGAME-26OCT21GSWLAL-GSW", ""):
        with pytest.raises(KalshiParseError):
            parse_ticker(bad)


def test_moneyline_same_game_key_as_polymarket_slug():
    # nba-gsw-lal-2026-10-21 (Poly, ET slug date) == KXNBAGAME-26OCT21GSWLAL: no date offset for the NBA
    s = _parse(BY_TICKER["KXNBAGAME-26OCT21GSWLAL-GSW"])
    assert s.game_key == "nba|2026-10-21|GSW@LAL" and s.side == "GSW" and s.team is None
    assert s.rules == {"ot": "unstated", "tie": "n/a", "cancel": "fair_price_if_not_started_48h", "postpone": "open_if_started_within_48h"}
    assert complement(s).side == "LAL"
    assert _parse(BY_TICKER["KXNBAGAME-26OCT21GSWLAL-LAL"]).side == "LAL"
    with pytest.raises(KalshiParseError, match="does not start with"):
        _parse(BY_TICKER["KXNBAGAME-26OCT21GSWLAL-GSW"], title="Los Angeles L wins")
    with pytest.raises(KalshiParseError, match="rules text does not name"):
        _parse(BY_TICKER["KXNBAGAME-26OCT21GSWLAL-GSW"], rules_primary="If Golden State wins the Golden State vs Denver Pro Basketball game originally scheduled for Oct 21, 2026, then the market resolves to Yes.")


def test_spread_total_team_total_lines():
    sp = _parse(BY_TICKER["KXNBASPREAD-26OCT23HOUSAS-SAS9"])
    assert (sp.family, sp.team, sp.line, sp.side) == ("spread", "SAS", -8.5, "SAS")
    assert canonical_key(sp) == "nba|2026-10-23|HOU@SAS|FG|spread|SAS|-8.5|SAS"
    assert complement(sp).side == "HOU"
    tot = _parse(BY_TICKER["KXNBATOTAL-26OCT23HOUSAS-254"])
    assert (tot.family, tot.line, tot.side, tot.team) == ("total", 253.5, "over", None)
    tt = _parse(BY_TICKER["KXNBATEAMTOTAL-26OCT21SACLAC-SAC99"])
    assert (tt.family, tt.team, tt.line, tt.side) == ("team_total", "SAC", 98.5, "over")
    with pytest.raises(KalshiParseError, match="ceil"):
        _parse(BY_TICKER["KXNBASPREAD-26OCT23HOUSAS-SAS9"], floor_strike=7.5)
    with pytest.raises(KalshiParseError, match="half point"):
        _parse(BY_TICKER["KXNBASPREAD-26OCT23HOUSAS-SAS9"], floor_strike=9.0)
    with pytest.raises(KalshiParseError, match="strike_type"):
        _parse(BY_TICKER["KXNBATOTAL-26OCT23HOUSAS-254"], strike_type="less")
    with pytest.raises(KalshiParseError, match="lacks spread line"):
        _parse(BY_TICKER["KXNBASPREAD-26OCT23HOUSAS-SAS9"], title="San Antonio wins by over 9.5 points")


@pytest.mark.parametrize("ticker,period,ot,family", [
    ("KXNBA1HSPREAD-26OCT21DALHOU-HOU8", "1H", "excluded", "spread"),
    ("KXNBA1HTOTAL-26OCT21DALHOU-137", "1H", "excluded", "total"),
    ("KXNBA1HTEAMTOTAL-26OCT20OKCSAS-SAS67", "1H", "excluded", "team_total"),
    ("KXNBA2HSPREAD-26OCT20OKCSAS-SAS8", "2H", "included", "spread"),
    ("KXNBA2HTOTAL-26OCT20OKCSAS-137", "2H", "included", "total"),
    ("KXNBA1QSPREAD-26OCT21DALHOU-HOU9", "1Q", "excluded", "spread"),
    ("KXNBA1QTOTAL-26OCT21DALHOU-73", "1Q", "excluded", "total"),
    ("KXNBA2QSPREAD-26OCT21DALHOU-HOU9", "2Q", "excluded", "spread"),
    ("KXNBA3QTOTAL-26OCT21DALHOU-69", "3Q", "excluded", "total"),
    ("KXNBA4QSPREAD-26OCT21MINMIA-MIN9", "4Q", "excluded", "spread"),
    ("KXNBA4QTOTAL-26OCT21INDNOP-73", "4Q", "excluded", "total"),
])
def test_period_markets(ticker, period, ot, family):
    s = _parse(BY_TICKER[ticker])
    assert (s.period, s.rules["ot"], s.family) == (period, ot, family)
    assert s.includes_ot is (ot == "included")
    with pytest.raises(KalshiParseError, match="period wording"):
        _parse(BY_TICKER[ticker], rules_primary=BY_TICKER[ticker]["rules_primary"].replace("1st", "9th").replace("2nd", "9th").replace("3rd", "9th").replace("4th", "9th"))


def test_period_winners_are_three_way_with_tie_strike():
    tie = _parse(BY_TICKER["KXNBA1H-26OCT21SACLAC-TIE"])
    sac = _parse(BY_TICKER["KXNBA1H-26OCT21SACLAC-SAC"])
    assert (tie.family, tie.period, tie.side) == ("moneyline", "1H", "tie")
    assert sac.side == "SAC" and sac.rules["tie"] == "tie_strike" and sac.rules["group"] == "three_way_with_tie_strike"
    assert complement(sac).side == "not_SAC"       # not the other team: a tie pays neither
    assert complement(tie).side == "not_tie"
    assert _parse(BY_TICKER["KXNBA2H-26OCT21SACLAC-TIE"]).rules["ot"] == "included"
    assert _parse(BY_TICKER["KXNBA4Q-26OCT21SACLAC-TIE"]).rules["ot"] == "excluded"
    assert _parse(BY_TICKER["KXNBA1Q-26OCT21SACLAC-SAC"]).period == "1Q"
    with pytest.raises(KalshiParseError, match="neither team"):
        _parse(BY_TICKER["KXNBA1H-26OCT21SACLAC-TIE"], rules_primary=BY_TICKER["KXNBA1H-26OCT21SACLAC-SAC"]["rules_primary"])


def test_player_props_suffix_name_line_and_dnp_rule():
    s = _parse(BY_TICKER["KXNBAPTS-26OCT20PHINYK-NYKMMCBRIDE2-15"])
    assert (s.family, s.line, s.side, s.team, s.bucket) == ("player_points", 14.5, "over", None, "m.mcbride")
    assert s.venue_ids == {"ticker": "KXNBAPTS-26OCT20PHINYK-NYKMMCBRIDE2-15", "series": "KXNBAPTS", "player": "Miles McBride",
                           "kalshi_team": "NYK", "jersey": "2"}
    assert s.rules["dnp"] == "fair_price" and s.rules["ot"] == "unstated"
    assert complement(s).side == "under"
    s = _parse(BY_TICKER["KXNBAREB-26OCT21ATLORL-ORLWCARTER34-12"])   # multi-digit jersey, "Jr." in the title
    assert s.bucket == "w.carter" and s.venue_ids["jersey"] == "34" and s.line == 11.5 and s.family == "player_rebounds"
    s = _parse(BY_TICKER["KXNBAREB-26OCT21ATLORL-ATLNALEXANDERWALKER7-8"])
    assert s.bucket == "n.alexanderwalker"
    s = _parse(BY_TICKER["KXNBABLK-26OCT20OKCSAS-OKCSGILGEOUSALEXANDER2-2"])
    assert s.bucket == "s.gilgeousalexander" and s.family == "player_blocks" and s.line == 1.5
    assert _parse(BY_TICKER["KXNBA3PT-26OCT20OKCSAS-OKCJWILLIAMS8-5"]).family == "player_threes"
    assert _parse(BY_TICKER["KXNBAPRA-26OCT21CHABKN-BKNJRANDLE30-25"]).family == "player_pra"
    assert _parse(BY_TICKER["KXNBASTL-26OCT21INDNOP-INDANEMBHARD2-3"]).family == "player_steals"
    m = BY_TICKER["KXNBAPTS-26OCT20PHINYK-NYKMMCBRIDE2-15"]
    with pytest.raises(KalshiParseError, match="!= suffix"):
        _parse(m, title="Miles McBridge: 15+ points", rules_primary=m["rules_primary"].replace("McBride", "McBridge"))
    with pytest.raises(KalshiParseError, match="grammar / line"):
        _parse(m, title="Miles McBride: 15+ rebounds")
    with pytest.raises(KalshiParseError, match="player wording"):
        _parse(m, rules_primary=m["rules_primary"].replace("15+", "16+"))
    assert _parse(m, rules_secondary="").rules["dnp"] == "unstated"


def test_full_game_series_rejects_period_wording():
    m = BY_TICKER["KXNBATOTAL-26OCT23HOUSAS-254"]
    with pytest.raises(KalshiParseError, match="mention a period"):
        _parse(m, rules_primary=m["rules_primary"].replace("in the Houston", "in the 1st half of the Houston"))


def test_rules_table():
    assert kalshi_rules("moneyline", "FG", "")["cancel"] == "unstated"
    assert kalshi_rules("moneyline", "1H", "... resolve to a fair price ... 48 hours ...")["tie"] == "tie_strike"
    assert "dnp" not in kalshi_rules("spread", "FG", "")
    assert kalshi_rules("player_points", "FG", "never takes the court, the market settles to a fair price")["dnp"] == "fair_price"
    assert set(SERIES) >= {"KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL", "KXNBA1H", "KXNBA4QTOTAL", "KXNBAPTS", "KXNBA3PT"}
    assert not set(SERIES) & set(SERIES_WITHOUT_POLY_FAMILY)
