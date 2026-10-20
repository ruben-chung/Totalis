"""Kalshi price feed + leg matches. Fixtures are real public responses (no credentials anywhere)."""
import base64
import json
from pathlib import Path

import httpx
import pytest

from rfqbot import kalshi_books as kb
from rfqbot.kalshi import market_row
from rfqbot.kalshi_books import (KalshiAuth, KalshiBookStore, KalshiFeed, KalshiFeedClient, KalshiQuote, MatchRow,
                                 kalshi_for_legs, kalshi_quotes, parse_batch_market, parse_orderbook, parse_ws_ticker,
                                 refresh_leg_matches, stats_lines)
from rfqbot.store import Store

FIX = Path(__file__).parent / "fixtures"
BATCH = json.loads((FIX / "kalshi_books_batch.json").read_text())
BOOKS = json.loads((FIX / "kalshi_books_orderbooks.json").read_text())
WS = json.loads((FIX / "kalshi_books_ws_frames.json").read_text())
NFL_KALSHI = json.loads((FIX / "kalshi_nfl_markets.json").read_text())
NFL_POLY = json.loads((FIX / "poly_nfl_rows.json").read_text())


# ------------------------------------------------------------------ parsing
def test_orderbook_levels_are_bids_and_ask_is_one_minus_no_bid():
    ob = BOOKS["KXNBAGAME-26OCT21GSWLAL-GSW"]
    yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth = parse_orderbook(ob)
    fp = ob["orderbook_fp"]
    assert yes_bid == float(fp["yes_dollars"][-1][0]) == 0.35          # best yes bid is the LAST (ascending) level
    assert yes_ask == round(1 - float(fp["no_dollars"][-1][0]), 4) == 0.36   # best no bid 0.64 -> yes ask 0.36
    assert bid_size == float(fp["yes_dollars"][-1][1]) and ask_size == float(fp["no_dollars"][-1][1])
    assert bid_depth == pytest.approx(sum(float(s) for p, s in fp["yes_dollars"] if float(p) >= 0.30))
    assert ask_depth == pytest.approx(sum(float(s) for p, s in fp["no_dollars"] if float(p) >= 0.59))
    assert yes_bid < yes_ask
    # matches the market record captured at the same time (yes_bid_dollars 0.35 / yes_ask_dollars 0.36)
    assert parse_orderbook(BOOKS["KXNFLGAME-26OCT26DALPHI-PHI"])[:2] == (0.34, 0.35)


def test_orderbook_empty_sides():
    assert parse_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}) == (None,) * 6
    assert parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.10", "5"]]}}) == (0.10, None, 5.0, None, 5.0, None)
    assert parse_orderbook({"orderbook_fp": {"no_dollars": [["0.90", "7"], ["0.91", "0"]]}}) == (None, 0.10, None, 7.0, None, 7.0)
    assert parse_orderbook({}) == (None,) * 6


def test_batch_uses_dollar_fields_and_treats_zero_one_as_empty():
    m = next(x for x in BATCH["markets"] if x["ticker"] == "KXNBAGAME-26OCT21GSWLAL-GSW")
    top, ts = parse_batch_market(m)
    assert top == (float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"]), float(m["yes_bid_size_fp"]), float(m["yes_ask_size_fp"]), None, None)
    assert m.get("yes_bid") is None and m.get("yes_ask") is None   # legacy cent fields are null / absent: never used
    assert ts == kb._iso_ms(m["updated_time"]) and ts > 1_700_000_000_000
    empty, _ = parse_batch_market({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000", "yes_bid_size_fp": "0.00", "yes_ask_size_fp": "0.00"})
    assert empty == (None, None, None, None, None, None)
    assert parse_batch_market({})[0] == (None,) * 6


def test_ws_ticker_frames():
    ticks = [f for f in WS if f.get("type") == "ticker"]
    assert WS[0]["type"] == "subscribed" and ticks
    t = next(f for f in ticks if f["msg"]["market_ticker"].startswith(("KXNBA", "KXNFL")))
    ticker, top, ts = parse_ws_ticker(t["msg"])
    assert ticker == t["msg"]["market_ticker"] and ts == t["msg"]["ts_ms"]
    assert top[0] == float(t["msg"]["yes_bid_dollars"]) and top[1] == float(t["msg"]["yes_ask_dollars"])
    assert parse_ws_ticker({"market_ticker": "X", "ts": 1669149841})[2] == 1669149841000


def test_chunk_tickers_by_count_and_url_length():
    ts = [f"KXNBASPREAD-26OCT23HOUSAS-SAS{i}" for i in range(450)]
    chunks = KalshiFeedClient.chunk_tickers(ts)
    assert [len(c) for c in chunks] == [200, 200, 50] and sum(chunks, []) == ts
    small = KalshiFeedClient.chunk_tickers(ts, max_chars=100)
    assert all(len(",".join(c)) <= 100 for c in small) and sum(small, []) == ts


# --------------------------------------------------------------------- auth
def test_auth_signs_timestamp_method_path_without_query(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)       # throwaway test key
    pem = tmp_path / "k.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    auth = KalshiAuth("key-id-123", pem)
    h = auth.headers("get", "/trade-api/v2/markets?tickers=A,B", timestamp_ms=1700000000000)
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert h["KALSHI-ACCESS-KEY"] == "key-id-123" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1700000000000GET/trade-api/v2/markets",
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    assert "key-id-123" not in repr(auth) or repr(auth).endswith("kind=rsa)")
    assert "BEGIN" not in repr(auth)


def test_auth_from_settings_requires_both_and_existing_file(tmp_path):
    class S:
        kalshi_key_id = ""
        kalshi_private_key_path = None
    assert KalshiAuth.from_settings(S()) is None
    S.kalshi_key_id = "x"; S.kalshi_private_key_path = tmp_path / "missing.pem"
    assert KalshiAuth.from_settings(S()) is None


# ------------------------------------------------------------------- client
async def test_client_backs_off_on_429_and_counts(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert "KALSHI-ACCESS-KEY" not in request.headers         # unauthenticated client sends no auth headers
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "too many requests"})
        return httpx.Response(200, json=BATCH)

    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(kb.asyncio, "sleep", fake_sleep)
    seen = []
    cl = KalshiFeedClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0,
                          on_response=lambda kind, text, at: seen.append(kind))
    async with cl:
        rows = await cl.batch_markets([m["ticker"] for m in BATCH["markets"]])
    assert len(rows) == len(BATCH["markets"]) and calls["n"] == 2
    assert cl.counts["n429"] == 1 and cl.counts["requests"] == 2 and cl.counts["requests_batch"] == 2
    assert sleeps and sleeps[0] == 1.5 and seen == ["batch", "batch"]


async def test_client_sends_signed_headers(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    pem = tmp_path / "e.pem"
    pem.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    auth = KalshiAuth("kid", pem)
    assert auth.kind == "ed25519"
    got = {}

    def handler(request):
        got.update(request.headers)
        return httpx.Response(200, json={"orderbook_fp": {}})

    async with KalshiFeedClient("https://x.test/trade-api/v2", auth=auth, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0) as cl:
        await cl.orderbook("T-1", 5)
    assert got["kalshi-access-key"] == "kid" and got["kalshi-access-timestamp"].isdigit() and got["kalshi-access-signature"]


# -------------------------------------------------------------------- store
def test_change_only_writes_and_restart_seeding(tmp_path):
    db = tmp_path / "kb.db"
    s = KalshiBookStore(db)
    top = (0.35, 0.36, 10.0, 20.0, None, None)
    assert s.insert_top("T", 1000, None, top, "batch") is True
    assert s.insert_top("T", 2000, None, top, "batch") is False                       # unchanged -> no row
    assert s.insert_top("T", 3000, None, (0.35, 0.36, 10.0, 25.0, None, None), "batch") is True   # size change -> row
    assert s.insert_top("T", 4000, None, (0.35, 0.36, 10.0, 25.0, 99.0, 99.0), "orderbook") is False  # depth alone is not a change
    assert s.insert_top("T", 4000, None, (0.35, 0.36, 10.0, 25.0, 99.0, 99.0), "orderbook", force=True) is True
    assert s.scalar("SELECT COUNT(*) FROM kalshi_top") == 3
    s.close()
    s2 = KalshiBookStore(db)                                                           # restart: cache seeded from the db
    assert s2.insert_top("T", 5000, None, (0.35, 0.36, 10.0, 25.0, None, None), "batch") is False
    assert s2.insert_top("T", 6000, None, (0.34, 0.36, 10.0, 25.0, None, None), "batch") is True
    s2.close()


def test_point_in_time_reads_never_return_a_future_row(tmp_path):
    s = KalshiBookStore(tmp_path / "kb.db")
    s.insert_top("A", 1000, 900, (0.30, 0.32, 1, 1, None, None), "batch")
    s.insert_top("A", 2000, 1900, (0.31, 0.33, 1, 1, None, None), "batch")
    s.insert_top("A", 3000, 2900, (0.32, 0.34, 1, 1, None, None), "batch")
    s.insert_top("B", 2500, None, (0.50, 0.52, 1, 1, 5, 5), "orderbook")
    assert s.latest_quote("A").yes_bid == 0.32 and s.latest_quote("A").at_ms == 3000
    assert s.latest_quote("A", at_ms=2000).yes_bid == 0.31          # at-or-before is inclusive
    assert s.latest_quote("A", at_ms=1999).yes_bid == 0.30
    assert s.latest_quote("A", at_ms=999) is None
    q = kalshi_quotes(["A", "B", "C"], at_ms=2500, db=s)
    assert set(q) == {"A", "B"} and q["A"].yes_bid == 0.31 and q["B"].source == "orderbook" and q["B"].bid_depth == 5
    assert isinstance(q["A"], KalshiQuote) and q["A"].mid == pytest.approx(0.32)
    assert q["A"].server_ts_ms == 1900
    s.close()


def test_match_upsert_rewrites_on_change(tmp_path):
    s = KalshiBookStore(tmp_path / "kb.db")
    row = {"position_id": "p1", "sport": "nfl", "canonical_key": "k", "game_key": "g", "kalshi_ticker": "T1", "kalshi_side": "yes",
           "confidence": "exact", "notes_json": "[]", "line_candidates_json": None, "matched_at_ms": 1}
    assert s.upsert_matches([row]) == {"new": 1}
    assert s.upsert_matches([{**row, "matched_at_ms": 2}]) == {"unchanged": 1}
    assert s.upsert_matches([{**row, "confidence": "shifted_line", "kalshi_ticker": "T2", "matched_at_ms": 3,
                              "line_candidates_json": json.dumps({"below": {"ticker": "T2", "side": "yes", "line": -6.5}})}]) == {"changed": 1}
    m = s.matches(["p1", "nope"])
    assert list(m) == ["p1"] and m["p1"].kalshi_ticker == "T2" and m["p1"].confidence == "shifted_line" and m["p1"].matched_at_ms == 3
    assert m["p1"].line_candidates["below"]["line"] == -6.5
    assert s.matched_tickers() == {"T2"} and s.match_counts() == {"shifted_line": 1}
    s.close()


# --------------------------------------------------------- matches end to end
@pytest.fixture
def rfq_db(tmp_path):
    """A small rfq.db with the NFL fixtures: catalog rows + synced kalshi_markets."""
    path = tmp_path / "rfq.db"
    st = Store(path)
    st.upsert_markets([{**r, "outcome_price": None, "volume": None, "tags": '["sports", "nfl"]', "synced_at_ms": 0} for r in NFL_POLY])
    rows = [market_row(m, "nfl", 1000) for m in NFL_KALSHI]
    for r in rows:
        r["status"] = "active"
    st.upsert_kalshi_markets(rows)
    st.close()
    return path


def test_refresh_leg_matches_and_join(rfq_db, tmp_path):
    s = KalshiBookStore(tmp_path / "kb.db")
    res = refresh_leg_matches(s, rfq_db, now=5000)
    assert res["legs"] == len(NFL_POLY) and res["parse_failures"] == {} and res["kalshi_markets"] > 0
    assert set(res["confidence"]) <= set(kb.CONFIDENCES) and res["confidence"].get("exact", 0) >= 2
    assert res["changes"] == {"new": len(NFL_POLY)}
    assert s.get_meta("matches_sync_stamp") == 1000 and s.get_meta("matches_refreshed_ms") == 5000
    # Bears vs Seahawks moneyline (slug 2026-11-03 UTC) matched KXNFLGAME-26NOV02CHISEA (ET): the day shift is resolved
    bears = next(r for r in NFL_POLY if r["slug"] == "nfl-chi-sea-2026-11-03" and r["outcome"] == "Bears")
    m = s.matches([bears["position_id"]])[bears["position_id"]]
    assert m.kalshi_ticker.startswith("KXNFLGAME-26NOV02CHISEA-") and m.confidence == "exact" and m.game_key == "nfl|2026-11-03|CHI@SEA"
    # join through quotes, point in time
    s.insert_top(m.kalshi_ticker, 100, None, (0.4, 0.42, 1, 1, None, None), "batch")
    s.insert_top(m.kalshi_ticker, 300, None, (0.41, 0.43, 1, 1, None, None), "batch")
    out = kalshi_for_legs([bears["position_id"], "unknown"], at_ms=200, db=s)
    assert list(out) == [bears["position_id"]]
    mr, q = out[bears["position_id"]]
    assert isinstance(mr, MatchRow) and q.yes_bid == 0.4 and q.at_ms == 100
    assert kalshi_for_legs([bears["position_id"]], db=s)[bears["position_id"]][1].yes_bid == 0.41
    # a leg with no Kalshi counterpart is present with quote None
    none_leg = next(pid for pid, mm in s.matches([r["position_id"] for r in NFL_POLY]).items() if mm.confidence == "none")
    assert kalshi_for_legs([none_leg], db=s)[none_leg] == (s.matches([none_leg])[none_leg], None)
    # second refresh after a re-sync that drops a market: the leg is rewritten, not kept
    st = Store(rfq_db)
    st.conn.execute("DELETE FROM kalshi_markets WHERE ticker=?", (m.kalshi_ticker,))
    st.conn.execute("UPDATE kalshi_markets SET synced_at_ms=2000")
    st.close()
    res2 = refresh_leg_matches(s, rfq_db, now=6000)
    assert res2["changes"].get("changed", 0) >= 1 and s.get_meta("matches_sync_stamp") == 2000
    m2 = s.matches([bears["position_id"]])[bears["position_id"]]
    assert m2.kalshi_ticker != m.kalshi_ticker
    assert kb.kalshi_sync_stamp(rfq_db) == 2000
    s.close()


# ---------------------------------------------------------------------- feed
async def test_sweep_and_depth_write_rows_and_request_minutes(rfq_db, tmp_path):
    books_by_ticker = {t: b for t, b in BOOKS.items()}
    batch_by_ticker = {m["ticker"]: m for m in BATCH["markets"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            t = request.url.path.split("/")[-2]
            return httpx.Response(200, json=books_by_ticker.get(t, {"orderbook_fp": {}}))
        ts = request.url.params["tickers"].split(",")
        return httpx.Response(200, json={"markets": [batch_by_ticker[t] for t in ts if t in batch_by_ticker]})

    s = KalshiBookStore(tmp_path / "kb.db")
    cl = KalshiFeedClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0)
    universe = sorted(batch_by_ticker)
    feed = KalshiFeed(s, cl, rfq_db, universe=lambda: universe, depth_targets=lambda: set(BOOKS))
    async with cl:
        n1 = await feed.sweep_once(universe)
        n2 = await feed.sweep_once(universe)          # unchanged -> nothing written
        d = await feed.depth_once(sorted(BOOKS))
    assert n1 == len(universe) and n2 == 0 and d == len(BOOKS)
    assert s.scalar("SELECT COUNT(*) FROM kalshi_top WHERE source='batch'") == len(universe)
    assert s.scalar("SELECT COUNT(*) FROM kalshi_top WHERE source='orderbook' AND bid_depth IS NOT NULL") == len(BOOKS)
    rm = s.conn.execute("SELECT kind, SUM(requests), SUM(tickers) FROM request_minutes GROUP BY kind").fetchall()
    assert {r[0]: (r[1], r[2]) for r in rm} == {"batch": (2, 2 * len(universe)), "orderbook": (len(BOOKS), len(BOOKS))}
    # ws frames: only universe tickers are stored; a repeat with the same top is a no-op
    tick = next(f for f in WS if f.get("type") == "ticker")
    msg = {**tick, "msg": {**tick["msg"], "market_ticker": universe[0], "yes_bid_dollars": "0.0100", "yes_ask_dollars": "0.0200",
                          "yes_bid_size_fp": "1.00", "yes_ask_size_fp": "2.00"}}
    feed.universe = universe
    before = s.scalar("SELECT COUNT(*) FROM kalshi_top")
    later = kb.now_ms() + 1_000                           # after the sweep rows written at wall-clock time
    feed.on_ws_frame(json.dumps(msg), later)
    feed.on_ws_frame(json.dumps(msg), later + 1)
    feed.on_ws_frame(json.dumps(tick), later + 2)          # outside the universe
    assert s.scalar("SELECT COUNT(*) FROM kalshi_top") == before + 1 and feed.counts["ws_ticker_outside_universe"] == 1
    assert s.latest_quote(universe[0]).source == "ws" and s.latest_quote(universe[0]).yes_bid == 0.01
    lines = stats_lines(s, rfq_db, now=200_000)
    assert any("kalshi_top rows" in ln for ln in lines) and any("leg_matches" in ln for ln in lines)
    s.close()


async def test_feed_run_refreshes_matches_then_stops(rfq_db, tmp_path):
    def handler(request):
        return httpx.Response(200, json={"markets": []})

    s = KalshiBookStore(tmp_path / "kb.db")
    cl = KalshiFeedClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0)
    feed = KalshiFeed(s, cl, rfq_db, sweep_sec=0.05, depth_sec=0.05, universe=lambda: [], depth_targets=lambda: set())
    import asyncio
    asyncio.get_running_loop().call_later(0.6, feed.stop)
    await feed.run()
    assert feed.counts["match_refreshes"] >= 1 and s.scalar("SELECT COUNT(*) FROM leg_matches") == len(NFL_POLY)
    s.close()


def test_batch_raw_log_keeps_quote_fields_of_changed_markets_only(tmp_path):
    from rfqbot.kalshi_books import BATCH_LOG_FIELDS, trim_batch_response
    from rfqbot.rawlog import RawLog, iter_rawlog_dir

    text = json.dumps(BATCH)
    slim = json.loads(trim_batch_response(text))
    assert slim["n_markets"] == len(BATCH["markets"]) and len(slim["markets"]) == len(BATCH["markets"])
    assert len(trim_batch_response(text)) < len(text) / 4
    assert set(slim["markets"][0]) <= set(BATCH_LOG_FIELDS) and "rules_primary" not in slim["markets"][0]
    assert slim["markets"][0]["yes_bid_dollars"] == BATCH["markets"][0]["yes_bid_dollars"]
    one = json.loads(trim_batch_response(text, {BATCH["markets"][1]["ticker"]}))
    assert one["n_markets"] == len(BATCH["markets"]) and [m["ticker"] for m in one["markets"]] == [BATCH["markets"][1]["ticker"]]
    assert trim_batch_response("not json") == "not json" and trim_batch_response('{"x": 1}') == '{"x": 1}'
    # through the feed: first sweep logs every market (all new), second logs none (nothing changed); orderbook verbatim
    batch_by_ticker = {m["ticker"]: m for m in BATCH["markets"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json=BOOKS["KXNBAGAME-26OCT21GSWLAL-GSW"])
        ts = request.url.params["tickers"].split(",")
        return httpx.Response(200, json={"markets": [batch_by_ticker[t] for t in ts]})

    rl = RawLog(tmp_path / "raw")
    cl = KalshiFeedClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0)
    feed = KalshiFeed(KalshiBookStore(None), cl, tmp_path / "none.db", rawlog=rl, universe=lambda: [], depth_targets=lambda: set())
    import asyncio

    async def go():
        async with cl:
            await feed.sweep_once(sorted(batch_by_ticker))
            await feed.sweep_once(sorted(batch_by_ticker))
            await feed.depth_once(["KXNBAGAME-26OCT21GSWLAL-GSW"])
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
    rl.close()
    frames = list(iter_rawlog_dir(tmp_path / "raw"))
    assert [f["c"] for f in frames] == ["batch", "batch", "orderbook"]
    first, second = json.loads(frames[0]["p"]), json.loads(frames[1]["p"])
    assert first["n_markets"] == len(BATCH["markets"]) == len(first["markets"]) and "rules_primary" not in frames[0]["p"]
    assert second["n_markets"] == len(BATCH["markets"]) and second["markets"] == []
    assert json.loads(frames[2]["p"]) == BOOKS["KXNBAGAME-26OCT21GSWLAL-GSW"]


def test_ticker_seen_tracks_observations_not_only_changes(tmp_path):
    s = KalshiBookStore(tmp_path / "kb.db")
    top = (0.35, 0.36, 10.0, 20.0, None, None)
    s.insert_top("T", 1000, None, top, "batch"); s.mark_seen(["T", "U"], 1000, "batch")
    assert s.insert_top("T", 9000, None, top, "batch") is False       # unchanged: no row ...
    s.mark_seen(["T"], 9000, "batch")                                 # ... but still observed
    assert s.seen_since(5000) == {"T"} and s.seen_since(500) == {"T", "U"}
    assert s.scalar("SELECT source FROM ticker_seen WHERE ticker='T'") == "batch"
    s.close()
