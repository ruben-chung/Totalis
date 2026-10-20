"""Kalshi settled props + candlesticks, Polymarket lines from a tiny history.db, game keys."""
import sqlite3

import pytest

from rfqbot.props.kalshi_lines import KalshiPublic, market_to_line, pre_tip_price, pull_kalshi_lines
from rfqbot.props.poly_lines import game_key_from_prefix, prop_from_slug, pull_poly_lines
from rfqbot.props.store import PropsStore

MARKET = {"ticker": "KXNBAPTS-26OCT20BOSDET-BOSPGEORGE13-20", "title": "Paul George: 20+ points", "floor_strike": 19.5,
          "strike_type": "greater", "yes_sub_title": "Paul George: 20+", "result": "yes", "expiration_value": "31",
          "rules_primary": "If Paul George records 20+ points in the Boston vs Detroit Pro Basketball game originally scheduled for Oct 20, 2026, then the market resolves to Yes.",
          "expected_expiration_time": "2026-10-21T01:00:00Z", "open_time": "2026-10-16T21:36:19Z", "close_time": "2026-10-20T20:49:40Z"}


def candle(end, bid=None, ask=None, close=None, prev=None):
    c = {"end_period_ts": end, "price": {}, "yes_bid": {}, "yes_ask": {}}
    if bid is not None:
        c["yes_bid"]["close_dollars"] = f"{bid:.4f}"
    if ask is not None:
        c["yes_ask"]["close_dollars"] = f"{ask:.4f}"
    if close is not None:
        c["price"]["close_dollars"] = f"{close:.4f}"
    if prev is not None:
        c["price"]["previous_dollars"] = f"{prev:.4f}"
    return c


def test_market_to_line_and_side_mapping():
    row = market_to_line(MARKET)
    assert row["player_key"] == "p.george" and row["team"] == "BOS" and row["game_key"] == "nba|2026-10-20|BOS@DET"
    assert row["stat"] == "pts" and row["line"] == 19.5 and row["result"] == "yes" and row["actual"] == 31.0
    assert row["player_name"] == "Paul George" and row["ticker"] == MARKET["ticker"] and row["p_over"] is None
    void = market_to_line({**MARKET, "result": "scalar", "expiration_value": ""})
    assert void["result"] == "void" and void["actual"] is None
    assert market_to_line({**MARKET, "ticker": "KXNBAGAME-26OCT20BOSDET-BOS", "title": "Boston wins"}) is None   # not a prop
    reb = market_to_line({**MARKET, "ticker": "KXNBAREB-26OCT20BOSDET-DETCCUNNINGHAM2-8", "title": "Cade Cunningham: 8+ rebounds",
                          "floor_strike": 7.5, "yes_sub_title": "Cade Cunningham: 8+",
                          "rules_primary": MARKET["rules_primary"].replace("Paul George records 20+ points", "Cade Cunningham records 8+ rebounds")})
    assert reb["stat"] == "reb" and reb["team"] == "DET" and reb["player_key"] == "c.cunningham"


def test_pre_tip_price_rules():
    cs = [candle(100, 0.40, 0.44, 0.42), candle(160, 0.45, 0.47, 0.46), candle(220, 0.0, 1.0, prev=0.50), candle(280, 0.60, 0.62)]
    assert pre_tip_price(cs, 200) == (pytest.approx(0.46), 160)  # last candle at or before tip, mid of the closes
    assert pre_tip_price(cs, 160) == (pytest.approx(0.46), 160)  # inclusive
    assert pre_tip_price(cs, 250) == (0.50, 220)                 # empty book -> last trade
    assert pre_tip_price(cs, 99) == (None, None)                 # nothing before tip
    assert pre_tip_price([candle(50, 0.0, 1.0)], 60) == (None, 50)
    assert pre_tip_price([candle(50, 0.30, None)], 60) == (0.30, 50)


class FakeKalshi(KalshiPublic):
    def __init__(self, markets, candles, raw_dir):
        super().__init__(raw_dir=raw_dir, min_gap_s=0)
        self._markets, self._candles, self.calls = markets, candles, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/markets":
            return {"markets": [m for m in self._markets if m["ticker"].startswith(params["series_ticker"])], "cursor": None}
        ticker = path.split("/markets/")[1].split("/")[0]
        return {"candlesticks": self._candles.get(ticker, [])}


def test_pull_kalshi_lines_uses_game_tip_and_is_incremental(tmp_path):
    store = PropsStore(tmp_path / "props.db")
    store.upsert("games", [{"game_id": "g1", "game_key": "nba|2026-10-20|BOS@DET", "season": 2027, "season_type": 2, "date_et": "2026-10-20",
                            "tip_utc": "2026-10-20T23:00:00Z", "away": "BOS", "home": "DET", "away_pts": None, "home_pts": None,
                            "ot_periods": None, "status": "scheduled", "fetched_ms": 1}])
    from datetime import datetime, timezone
    tip = int(datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc).timestamp())
    other = {**MARKET, "ticker": "KXNBAREB-26OCT20BOSDET-BOSPGEORGE13-8", "title": "Paul George: 8+ rebounds", "floor_strike": 7.5,
             "yes_sub_title": "Paul George: 8+", "result": "no", "expiration_value": "5",
             "rules_primary": MARKET["rules_primary"].replace("20+ points", "8+ rebounds")}
    client = FakeKalshi([MARKET, other], {MARKET["ticker"]: [candle(tip - 600, 0.50, 0.54), candle(tip + 600, 0.9, 0.92)]}, tmp_path / "raw")
    n = pull_kalshi_lines(store, client, series=("KXNBAPTS", "KXNBAREB"), out=lambda *a: None)
    assert n == {"markets": 2, "lines": 2, "unparsed": 0, "priced": 1, "no_candles": 1, "tip_from_close": 0}
    rows = {r["ticker"]: r for r in store.rows("SELECT * FROM prop_lines")}
    assert rows[MARKET["ticker"]]["p_over"] == pytest.approx(0.52) and rows[MARKET["ticker"]]["t"] == tip - 600
    assert rows[other["ticker"]]["p_over"] is None and rows[other["ticker"]]["result"] == "no" and rows[other["ticker"]]["actual"] == 5.0
    cpath = [c for c in client.calls if "candlesticks" in c[0]]
    assert len(cpath) == 2 and cpath[0][1]["end_ts"] == tip and cpath[0][1]["start_ts"] >= tip - 5000 * 60
    # second run: candles already fetched (fetch_log + raw cache) -> no candlestick requests, prices kept
    client.calls.clear()
    n = pull_kalshi_lines(store, client, series=("KXNBAPTS", "KXNBAREB"), out=lambda *a: None)
    assert [c for c in client.calls if "candlesticks" in c[0]] == [] and n["priced"] == 1
    assert store.scalar("SELECT p_over FROM prop_lines WHERE ticker=?", MARKET["ticker"]) == pytest.approx(0.52)
    store.close()


def test_game_key_and_slug_fallback():
    assert game_key_from_prefix("nba-okc-ind-2025-10-23") == "nba|2025-10-23|OKC@IND"
    assert game_key_from_prefix("nba-gsw-lal-2026-10-21") == "nba|2026-10-21|GSW@LAL"
    assert game_key_from_prefix("nfl-ne-chi-2026-10-23") is None and game_key_from_prefix("nba-xxx-lal-2026-10-21") is None
    assert prop_from_slug("nba-mia-bkn-2025-12-18-points-nicolas-claxton-13pt5", "Nicolas Claxton: Points Over 13.5", 13.5) == (
        "pts", "n.claxton", "Nicolas Claxton", 13.5)
    assert prop_from_slug("nba-mia-bkn-2025-12-18-assists-michael-porter-jr-4pt5", None, None) == ("ast", "m.porter", "michael porter jr", 4.5)
    assert prop_from_slug("nba-mia-bkn-2025-12-18-spread-home-3pt5", "Spread", 3.5) is None


def _history(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE hist_markets (condition_id TEXT PRIMARY KEY, game_prefix TEXT, sport TEXT, slug TEXT, title TEXT, line REAL,
                                   end_date TEXT, game_start_time TEXT, sports_market_type TEXT);
        CREATE TABLE hist_legs (condition_id TEXT, outcome_index INTEGER, game_prefix TEXT, token_id TEXT, outcome TEXT, won INTEGER,
                                family TEXT, period TEXT, side TEXT, team TEXT, line REAL, bucket TEXT, parse_error TEXT);
        CREATE TABLE hist_prices (token_id TEXT, offset_min INTEGER, t INTEGER, p REAL);
    """)
    g = "nba-phx-por-2026-02-03"
    conn.executemany("INSERT INTO hist_markets VALUES (?,?,?,?,?,?,?,?,?)", [
        ("c1", g, "nba", g + "-points-jerami-grant-16pt5", "Jerami Grant: Points O/U 16.5", 16.5, "2026-02-04", "2026-02-04 03:00:00+00", "points"),
        ("c2", g, "nba", g + "-assists-devin-booker-6pt5", "Devin Booker: Assists Over 6.5", 6.5, "2026-02-04", "2026-02-04 03:00:00+00", "assists"),
        ("c3", g, "nba", g + "-spread-away-5pt5", "Spread: Suns (-5.5)", None, "2026-02-04", None, "spread"),
        ("c4", g, "nba", g + "-spread-away-3pt5", "Spread: Suns (-3.5)", None, "2026-02-04", None, "spread"),
        ("c5", g, "nba", g + "-total-219pt5", "Total O/U 219.5", None, "2026-02-04", None, "total"),
        ("c6", g, "nba", g + "-total-223pt5", "Total O/U 223.5", None, "2026-02-04", None, "total"),
        ("c7", g, "nba", g + "-1h-total-110pt5", "1H Total", None, "2026-02-04", None, "total"),
    ])
    conn.executemany("INSERT INTO hist_legs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("c1", 0, g, "t1", "Yes", 1, "player_points", "FG", "over", None, 16.5, "j.grant", None),
        ("c1", 1, g, "t1b", "No", 0, "player_points", "FG", "under", None, 16.5, "j.grant", None),
        ("c2", 0, g, "t2", "Yes", 0, None, None, None, None, None, None, "PolyParseError: title does not match player grammar"),
        ("c2", 1, g, "t2b", "No", 1, None, None, None, None, None, None, "PolyParseError: title does not match player grammar"),
        ("c3", 0, g, "t3", "Suns", 1, "spread", "FG", "PHX", "PHX", -5.5, None, None),
        ("c3", 1, g, "t3b", "Blazers", 0, "spread", "FG", "POR", "PHX", -5.5, None, None),
        ("c4", 0, g, "t4", "Suns", 1, "spread", "FG", "PHX", "PHX", -3.5, None, None),
        ("c5", 0, g, "t5", "Over", 1, "total", "FG", "over", None, 219.5, None, None),
        ("c6", 0, g, "t6", "Over", 0, "total", "FG", "over", None, 223.5, None, None),
        ("c7", 0, g, "t7", "Over", 0, "total", "1H", "over", None, 110.5, None, None),
    ])
    conn.executemany("INSERT INTO hist_prices VALUES (?,?,?,?)", [("t1", 15, 1770170000, 0.47), ("t1", 60, 1770160000, 0.50), ("t2", 15, 1770170000, 0.61)])
    conn.commit(); conn.close()


def test_pull_poly_lines_and_game_lines(tmp_path):
    _history(tmp_path / "history.db")
    store = PropsStore(tmp_path / "props.db")
    n = pull_poly_lines(store, history_db=tmp_path / "history.db", offset_min=15, out=lambda *a: None)
    assert n == {"legs": 4, "lines": 2, "parsed": 1, "slug_fallback": 1, "skipped": 2, "no_game_key": 0, "priced": 2, "game_lines": 2}
    rows = {r["ticker"]: r for r in store.rows("SELECT * FROM prop_lines")}
    a = rows["c1:0"]
    assert (a["player_key"], a["stat"], a["line"], a["p_over"], a["t"], a["result"], a["game_key"], a["team"]) == (
        "j.grant", "pts", 16.5, 0.47, 1770170000, "yes", "nba|2026-02-03|PHX@POR", None)
    b = rows["c2:0"]
    assert (b["player_key"], b["player_name"], b["stat"], b["line"], b["p_over"], b["result"]) == ("d.booker", "Devin Booker", "ast", 6.5, 0.61, "no")
    gl = {r["team"]: r for r in store.rows("SELECT * FROM game_lines")}
    assert gl["PHX"]["total_line"] == 221.5 and gl["PHX"]["spread_line"] == -4.5 and gl["PHX"]["team_pts_line"] == 113.0
    assert gl["POR"]["spread_line"] == 4.5 and gl["POR"]["team_pts_line"] == 108.5 and gl["POR"]["n_total"] == 2   # 1H total ignored
    assert pull_poly_lines(store, history_db=tmp_path / "none.db", out=lambda *a: None)["lines"] == 0
    store.close()
