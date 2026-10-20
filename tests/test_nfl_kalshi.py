import json
from pathlib import Path

import pytest

from rfqbot.sports.legspec import canonical_key, complement
from rfqbot.sports.nfl_kalshi import SERIES, KalshiParseError, parse_kalshi_nfl, parse_ticker, split_team_pair

FIX = Path(__file__).parent / "fixtures"
MARKETS = json.loads((FIX / "kalshi_nfl_markets.json").read_text())
BY_TICKER = {m["ticker"]: m for m in MARKETS}


def parse(m):
    return parse_kalshi_nfl(m["ticker"], m["title"], m["floor_strike"], m["strike_type"], m["rules_primary"],
                            m["expected_expiration_time"], yes_sub_title=m["yes_sub_title"], rules_secondary=m["rules_secondary"])


def test_fixture_markets_in_supported_series_parse_and_unsupported_raise():
    n = 0
    for m in MARKETS:
        series = m["ticker"].split("-")[0]
        if series in SERIES:
            s = parse(m)
            assert s.venue == "kalshi" and s.game_date_basis == "et" and s.venue_ids["ticker"] == m["ticker"]
            complement(s)
            n += 1
        else:
            with pytest.raises(KalshiParseError, match="no parse rule|not in grammar"):
                parse(m)
    assert n >= 20


def test_ticker_grammar_et_date_and_team_split():
    assert parse_ticker("KXNFLGAME-26NOV02CHISEA-SEA")[:2] == ("KXNFLGAME", "2026-11-02")
    _, _, away, home, suffix = parse_ticker("KXNFLSPREAD-26OCT26DALPHI-PHI8")
    assert (away.code, home.code, suffix) == ("DAL", "PHI", "PHI8")
    assert tuple(t.code for t in split_team_pair("LACLAR")) == ("LAC", "LAR")
    assert tuple(t.code for t in split_team_pair("INDJAC")) == ("IND", "JAX")
    assert tuple(t.code for t in split_team_pair("NECHI")) == ("NE", "CHI")
    assert tuple(t.code for t in split_team_pair("KCSEA")) == ("KC", "SEA")
    for bad in ("DALDAL", "DALXYZ", "PHI"):
        with pytest.raises(KalshiParseError):
            split_team_pair(bad)
    with pytest.raises(KalshiParseError):
        parse_ticker("KXNFLSPREAD-26OCX26DALPHI-PHI8")


def test_spread_is_one_sided_per_team():
    phi8 = parse(BY_TICKER["KXNFLSPREAD-26OCT26DALPHI-PHI8"])
    assert (phi8.team, phi8.line, phi8.side, phi8.period) == ("PHI", -7.5, "PHI", "FG")
    assert canonical_key(phi8) == "nfl|2026-10-26|DAL@PHI|FG|spread|PHI|-7.5|PHI"
    assert complement(phi8).side == "DAL"  # NO = Cowboys +7.5
    dal8 = parse(BY_TICKER["KXNFLSPREAD-26OCT26DALPHI-DAL8"])
    assert dal8.team == "DAL" and canonical_key(dal8) != canonical_key(complement(phi8))


def test_totals_team_totals_and_yards():
    t = parse(BY_TICKER["KXNFLTOTAL-26OCT26DALPHI-63"])
    assert (t.family, t.line, t.side, t.team) == ("total", 62.5, "over", None) and complement(t).side == "under"
    tt = parse(BY_TICKER["KXNFLTEAMTOTAL-26OCT26DALPHI-PHI43"])
    assert (tt.family, tt.team, tt.line) == ("team_total", "PHI", 42.5)
    y = parse(BY_TICKER["KXNFLTEAMYDS-26OCT22NECHI-NE350"])
    assert (y.family, y.team, y.line, y.side) == ("team_off_yards", "NE", 349.5, "over")
    h = parse(BY_TICKER["KXNFL1HTEAMTOTAL-26OCT26DALPHI-DAL13"])
    assert (h.period, h.team, h.line, h.includes_ot) == ("1H", "DAL", 12.5, False)


def test_winners_and_three_way_periods():
    g = parse(BY_TICKER["KXNFLGAME-26NOV02CHISEA-SEA"])
    assert (g.family, g.side, g.rules["tie"], g.game_date) == ("moneyline", "SEA", "50-50", "2026-11-02")
    assert complement(g).side == "CHI"
    tie = parse(BY_TICKER["KXNFL1H-26OCT25GBDET-TIE"])
    assert (tie.family, tie.period, tie.side, tie.rules["tie"]) == ("moneyline", "1H", "tie", "tie_strike")
    gb = parse(BY_TICKER["KXNFL1H-26OCT25GBDET-GB"])
    assert gb.side == "GB" and complement(gb).side == "not_GB"
    q4 = parse(BY_TICKER["KXNFL4Q-26OCT25DENARI-DEN"])
    assert q4.period == "4Q" and q4.includes_ot is False


def test_ot_rules_by_period():
    assert parse(BY_TICKER["KXNFL2HSPREAD-26OCT25GBDET-GB8"]).includes_ot is True
    assert parse(BY_TICKER["KXNFL1HSPREAD-26OCT25GBDET-GB8"]).includes_ot is False
    assert parse(BY_TICKER["KXNFL4QSPREAD-26OCT25DENARI-DEN8"]).includes_ot is False
    assert parse(BY_TICKER["KXNFLSPREAD-26OCT26DALPHI-PHI8"]).includes_ot is None
    assert parse(BY_TICKER["KXNFLSFTY-26OCT22NECHI-Y"]).includes_ot is True
    assert parse(BY_TICKER["KXNFL4QBTTS-26OCT22NECHI-Y"]).includes_ot is False
    tie = parse(BY_TICKER["KXNFLTIE-26OCT25GBDET-Y"])
    assert (tie.family, tie.bucket, tie.side, tie.rules["cancel"]) == ("exact_margin", "tie", "yes", "fair_price_if_not_started_48h")
    assert parse(BY_TICKER["KXNFLSPREAD-26OCT26DALPHI-PHI8"]).rules["cancel"] == "unstated"


def test_cross_checks_raise():
    m = dict(BY_TICKER["KXNFLSPREAD-26OCT26DALPHI-PHI8"])
    with pytest.raises(KalshiParseError):
        parse({**m, "floor_strike": 8.5})                        # suffix != ceil(line)
    with pytest.raises(KalshiParseError):
        parse({**m, "strike_type": "less"})
    with pytest.raises(KalshiParseError):
        parse({**m, "title": "Dallas wins by over 7.5 points?"})  # title team != suffix team
    with pytest.raises(KalshiParseError):
        parse({**m, "rules_primary": m["rules_primary"].replace("Oct 26, 2026", "Oct 27, 2026")})
    with pytest.raises(KalshiParseError):
        parse({**m, "rules_primary": m["rules_primary"].replace("Dallas vs Philadelphia", "Philadelphia vs Dallas")})
    with pytest.raises(KalshiParseError):
        parse({**m, "ticker": "KXNFLSPREAD-26OCT26DALPHI-NYG8"})
    h = dict(BY_TICKER["KXNFL1HSPREAD-26OCT25GBDET-GB8"])
    with pytest.raises(KalshiParseError):
        parse({**h, "rules_primary": h["rules_primary"].replace("1st half", "2nd half")})
    b = dict(BY_TICKER["KXNFL1QBTTS-26OCT22NECHI-Y"])
    with pytest.raises(KalshiParseError):
        parse({**b, "ticker": "KXNFL1QBTTS-26OCT22NECHI-N"})
