"""Book recorder against a mock CLOB market-channel server."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from rfqbot.books import Book, BookConn, BookRecorder, BookStore
from rfqbot.rawlog import RawLog, iter_rawlog_dir


def _book(asset, bids, asks, ts="1700000000000"):
    return {"event_type": "book", "asset_id": asset, "market": "0xm", "timestamp": ts, "hash": "h",
            "bids": [{"price": p, "size": s} for p, s in bids], "asks": [{"price": p, "size": s} for p, s in asks]}


class MockClob:
    def __init__(self):
        self.subscribes = []
        self.ops = []
        self.pings = 0

    async def handler(self, ws):
        sub = json.loads(await ws.recv())
        self.subscribes.append(sub)
        # initial dump: one snapshot per asset, as a list
        await ws.send(json.dumps([_book(a, [("0.40", "100"), ("0.39", "50")], [("0.42", "80"), ("0.50", "500")])
                                  for a in sub["assets_ids"]]))
        # a delta deep in the book (top unchanged) and one at the top
        await ws.send(json.dumps({"event_type": "price_change", "market": "0xm", "timestamp": "1700000001000",
                                  "price_changes": [{"asset_id": "A", "price": "0.30", "size": "10", "side": "BUY"}]}))
        await ws.send(json.dumps({"event_type": "price_change", "market": "0xm", "timestamp": "1700000002000",
                                  "price_changes": [{"asset_id": "A", "price": "0.42", "size": "0", "side": "SELL",
                                                     "best_bid": "0.40", "best_ask": "0.50"}]}))
        await ws.send(json.dumps({"event_type": "last_trade_price", "asset_id": "B", "market": "0xm", "price": "0.41",
                                  "size": "7", "side": "BUY", "timestamp": "1700000003000", "fee_rate_bps": "0"}))
        async for msg in ws:
            if msg == "PING":
                self.pings += 1
                await ws.send("PONG")
            else:
                op = json.loads(msg)
                self.ops.append(op)
                if op.get("operation") == "subscribe":
                    await ws.send(json.dumps(_book(op["assets_ids"][0], [("0.10", "1")], [("0.90", "1")])))


@pytest.fixture
async def clob():
    srv = MockClob()
    async with serve(srv.handler, "127.0.0.1", 0) as server:
        srv.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/market"
        yield srv


def test_book_summary_and_depth():
    b = Book()
    b.snapshot([{"price": "0.40", "size": "100"}, {"price": "0.36", "size": "50"}, {"price": "0.30", "size": "999"}],
               [{"price": "0.42", "size": "80"}])
    assert b.summary() == (4000, 4200, 100.0, 80.0, 150.0, 80.0)   # depth within 0.05 excludes the 0.30 level
    b.delta("0.42", "0", "SELL")
    assert b.summary()[1] is None and b.summary()[3] is None


async def test_recorder_end_to_end(clob, tmp_path):
    universe = {"A", "B"}
    store = BookStore(None)
    rawlog = RawLog(tmp_path / "books", flush_s=0)
    rec = BookRecorder(store, lambda: set(universe), url=clob.url, rawlog=rawlog, refresh_sec=0.1,
                       ping_interval_s=0.05, commit_interval_s=0, min_interval_ms=0, raw_deltas=True)

    async def script():
        while store.scalar("SELECT COUNT(*) FROM leg_trades") == 0:
            await asyncio.sleep(0.02)
        universe.add("C")                       # grows -> dynamic subscribe on the live connection
        while not any(o.get("operation") == "subscribe" for o in clob.ops):
            await asyncio.sleep(0.02)
        universe.discard("B")                   # shrinks -> unsubscribe
        while not any(o.get("operation") == "unsubscribe" for o in clob.ops):
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        rec.stop()

    await asyncio.wait_for(asyncio.gather(rec.run(), script()), 5)

    assert clob.subscribes[0] == {"assets_ids": ["A", "B"], "type": "market"}
    assert clob.pings >= 1 and rec.counts.get("pong", 0) >= 1
    # A: snapshot row, deep delta writes nothing, top delta writes a row with the next ask level
    rows = [tuple(r) for r in store.conn.execute(
        "SELECT source, best_bid, best_ask, bid_size, ask_size, bid_depth, ask_depth FROM book_top WHERE asset_id='A' ORDER BY at_ms")]
    assert rows == [("book", 4000, 4200, 100.0, 80.0, 150.0, 80.0), ("delta", 4000, 5000, 100.0, 500.0, 150.0, 500.0)]
    assert store.scalar("SELECT COUNT(*) FROM book_top WHERE asset_id='B'") == 1
    assert store.scalar("SELECT price FROM leg_trades WHERE asset_id='B'") == 4100
    assert store.scalar("SELECT COUNT(*) FROM book_top WHERE asset_id='C'") == 1
    assert [o["operation"] for o in clob.ops] == ["subscribe", "unsubscribe"]
    # subscriptions: one row per asset on the single socket, all tagged with its conn_id; B closed by the
    # unsubscribe (before the socket closed), A and C closed by the disconnect at shutdown
    conn_id = store.scalar("SELECT conn_id FROM book_connections")
    assert conn_id and store.scalar("SELECT COUNT(*) FROM book_connections") == 1
    subs = {r[0]: (r[1], r[2], r[3]) for r in store.conn.execute(
        "SELECT asset_id, position_id, unsubscribed_ms, conn_id FROM subscriptions")}
    assert set(subs) == {"A", "B", "C"} and all(cid == conn_id for _, _, cid in subs.values())
    assert subs["C"][0] == "C" and all(un is not None for un, _, _ in subs.values())
    disconnected = store.scalar("SELECT disconnected_at_ms FROM book_connections")
    assert subs["B"][1] < disconnected and subs["A"][1] == subs["C"][1] == disconnected
    # socket liveness persisted for the marginal layer
    assert store.scalar("SELECT last_frame_ms FROM book_connections") == rec.last_frame_ms(conn_id) > 0
    # raw log has every non-PONG frame verbatim
    logged = list(iter_rawlog_dir(tmp_path / "books"))
    assert len(logged) >= 5 and all(l["p"] != "PONG" for l in logged)


async def test_subscription_rows_follow_their_connection():
    """Rows open under the socket's conn_id on connect, close on disconnect, reopen under the new id on reconnect;
    an asset added to a live socket gets a row at once, one added to a socket that is still connecting does not."""
    store = BookStore(None)
    rec = BookRecorder(store, lambda: set(), commit_interval_s=0, min_interval_ms=0)
    store.begin()
    c = BookConn(rec, 0)
    c.wanted = {"A", "B"}; rec.position_of.update({"A": "pA", "B": "pB"}); rec.owner.update({"A": c, "B": c}); rec.conns.append(c)
    c.conn_id = "b0-first"
    rec.on_connected(c, 2)
    rows = lambda: {tuple(r) for r in store.conn.execute("SELECT asset_id, position_id, conn_id, unsubscribed_ms FROM subscriptions")}
    assert rows() == {("A", "pA", "b0-first", None), ("B", "pB", "b0-first", None)}
    rec.on_frame(c, "PONG")
    assert rec.last_frame_ms("b0-first") == c.last_frame_ms > 0 and rec.last_frame_ms() == c.last_frame_ms
    rec._persist_liveness()
    assert store.last_frame_ms("b0-first") == c.last_frame_ms
    rec.on_disconnected(c, "closed 1006")
    assert all(un is not None for *_, un in rows()) and store.scalar("SELECT close_reason FROM book_connections") == "closed 1006"
    c.conn_id = "b0-second"; c.last_frame_ms = None
    rec.on_connected(c, 2)
    assert {r for r in rows() if r[3] is None} == {("A", "pA", "b0-second", None), ("B", "pB", "b0-second", None)}
    assert store.last_frame_ms("b0-second") is None and rec.last_frame_ms("b0-second") is None
    # universe refresh while the socket is not open: no row yet (on_connected will write it)
    c._ws = None
    await _refresh_with(rec, {"pA": "A", "pB": "B", "pX": "X"})
    assert not any(a == "X" for a, *_ in rows())
    c._ws = object()      # pretend the socket is open: the next addition is recorded under the live conn_id
    await _refresh_with(rec, {"pA": "A", "pB": "B", "pX": "X", "pY": "Y"})
    assert ("Y", "pY", "b0-second", None) in rows()
    for t in rec._tasks:
        t.cancel()


async def _refresh_with(rec, tokens: dict[str, str]):
    """Run one universe refresh against fixed position_id -> token_id pairs."""
    async def mapper(pos):
        return {p: tokens[p] for p in pos if p in tokens}
    rec.universe, rec.mapper = (lambda: set(tokens)), mapper
    await rec._refresh_universe()


async def test_shutdown_closes_universe_and_mapper():
    closed = []

    class Universe:
        def __call__(self): return set()
        def close(self): closed.append("universe")

    class Mapper:
        async def __call__(self, pos): return {}
        def close(self): closed.append("mapper")

    store = BookStore(None)
    rec = BookRecorder(store, Universe(), mapper=Mapper(), refresh_sec=0.05)
    rec.stop()                       # stop() before run(): no event yet, run() must still exit cleanly
    await asyncio.wait_for(rec.run(), 2)
    assert closed == ["universe", "mapper"]


def test_store_migrates_pre_conn_id_schema(tmp_path):
    """books.db files written before 2026-10-21 lack subscriptions.conn_id / book_connections.last_frame_ms."""
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("CREATE TABLE subscriptions (asset_id TEXT NOT NULL, position_id TEXT, subscribed_ms INTEGER NOT NULL, unsubscribed_ms INTEGER);"
                    "CREATE TABLE book_connections (conn_id TEXT PRIMARY KEY, connected_at_ms INTEGER NOT NULL, n_assets INTEGER,"
                    " disconnected_at_ms INTEGER, close_reason TEXT); INSERT INTO subscriptions VALUES ('a', 'p', 1, NULL);")
    c.commit(); c.close()
    store = BookStore(p)
    assert "conn_id" in {r[1] for r in store.conn.execute("PRAGMA table_info(subscriptions)")}
    store.subscribed({"b": "q"}, 2, conn_id="c1"); store.conn_opened("c1", 2, 1); store.set_last_frame_ms({"c1": 5})
    assert store.last_frame_ms("c1") == 5 and store.unsubscribed_conn("c1", 9) == 1
    assert [tuple(r) for r in store.conn.execute("SELECT * FROM subscriptions ORDER BY asset_id")] == [("a", "p", 1, None, None), ("b", "q", 2, 9, "c1")]
    store.close()
    BookStore(p).close()      # idempotent on the migrated file


def test_universe_sources_close_their_connections(tmp_path):
    import sqlite3
    from rfqbot.books import GameUniverse, RfqLegUniverse, TokenMap
    from rfqbot.store import Store
    Store(tmp_path / "rfq.db").close()
    store = BookStore(None)
    legs = RfqLegUniverse(tmp_path / "rfq.db", window_ms=1000)
    gu = GameUniverse(store, tmp_path / "rfq.db", legs)
    tm = TokenMap(store, tmp_path / "rfq.db")
    assert legs() == set()
    gu.close(); tm.close()
    for conn in (legs.conn, gu.cat, tm.cat):   # GameUniverse.close() also closes the legs it wraps
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


async def test_delta_without_snapshot_is_ignored():
    store = BookStore(None)
    rec = BookRecorder(store, lambda: set(), commit_interval_s=0, min_interval_ms=0)
    store.begin()
    rec._apply({"event_type": "price_change", "market": "m", "timestamp": "1",
                "price_changes": [{"asset_id": "X", "price": "0.5", "size": "1", "side": "BUY"}]}, 1)
    assert store.scalar("SELECT COUNT(*) FROM book_top") == 0 and rec.counts["delta_without_snapshot"] == 1


async def test_token_map_resolves_via_catalog_and_gamma(tmp_path, monkeypatch):
    import json as _json
    import httpx
    from rfqbot.books import TokenMap
    from rfqbot.store import Store
    rfq = Store(tmp_path / "rfq.db")
    rfq.upsert_markets([
        {"position_id": "pos_yes", "market_id": "1", "condition_id": "0xC1", "slug": "s1", "title": "t", "outcome": "Yes",
         "outcome_index": 0, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1},
        {"position_id": "pos_no", "market_id": "1", "condition_id": "0xC1", "slug": "s1", "title": "t", "outcome": "No",
         "outcome_index": 1, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1},
    ])
    rfq.close()
    calls = []

    async def fake_get(self, url, params=None):
        calls.append(params)
        body = [{"conditionId": "0xc1", "clobTokenIds": _json.dumps(["tok_yes", "tok_no"]), "outcomes": _json.dumps(["Yes", "No"]),
                 "gameStartTime": "2026-10-21T18:00:00Z", "endDate": "2026-10-22", "events": [{"id": 42}]}]
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    st = BookStore(None)
    tm = TokenMap(st, tmp_path / "rfq.db")
    out = await tm({"pos_yes", "pos_no", "pos_unknown"})
    assert out == {"pos_yes": "tok_yes", "pos_no": "tok_no"} and tm.unresolved == 1
    assert len(calls) == 1 and ("condition_ids", "0xC1") in calls[0]
    assert st.scalar("SELECT event_id FROM leg_tokens WHERE position_id='pos_yes'") == "42"
    assert st.scalar("SELECT token_id FROM leg_tokens WHERE position_id='pos_unknown'") is None
    # second call is served from cache: no HTTP
    assert await tm({"pos_yes"}) == {"pos_yes": "tok_yes"} and len(calls) == 1


async def test_top_rows_are_coalesced_per_interval():
    store = BookStore(None)
    rec = BookRecorder(store, lambda: set(), commit_interval_s=0, min_interval_ms=1000)
    store.begin()
    rec._apply(_book("A", [("0.40", "10")], [("0.42", "10")], ts="1000"), 1000)
    rec._apply({"event_type": "price_change", "market": "m", "timestamp": "1100",
                "price_changes": [{"asset_id": "A", "price": "0.41", "size": "5", "side": "BUY"}]}, 1100)
    rec._apply({"event_type": "price_change", "market": "m", "timestamp": "1200",
                "price_changes": [{"asset_id": "A", "price": "0.39", "size": "7", "side": "BUY"}]}, 1200)   # deep: ignored
    rec._apply({"event_type": "price_change", "market": "m", "timestamp": "1300",
                "price_changes": [{"asset_id": "A", "price": "0.415", "size": "3", "side": "BUY"}]}, 1300)
    assert store.scalar("SELECT COUNT(*) FROM book_top") == 1          # only the snapshot so far
    rec._flush_dirty(1900)
    assert store.scalar("SELECT COUNT(*) FROM book_top") == 1          # interval not elapsed
    rec._flush_dirty(2000)
    rows = [tuple(r) for r in store.conn.execute("SELECT at_ms, best_bid, bid_size FROM book_top ORDER BY at_ms")]
    assert rows == [(1000, 4000, 10.0), (2000, 4150, 3.0)]            # one deferred row with the latest state
    rec._flush_dirty(5000)
    assert store.scalar("SELECT COUNT(*) FROM book_top") == 2


# ----------------------------------------------------------------- game universe
def _cat_market(pid, slug, idx=0, cond=None):
    return {"position_id": pid, "market_id": "1", "condition_id": cond or f"0x{slug}", "slug": slug, "title": slug,
            "outcome": "Yes" if idx == 0 else "No", "outcome_index": idx, "outcome_price": 0.5, "volume": 0,
            "tags": "[]", "synced_at_ms": 1}


def _game_catalog(tmp_path, n_lines=3):
    """Two NFL games (A with 3 lines, B with 3 lines), one NBA game, one soccer market."""
    from rfqbot.store import Store
    rfq = Store(tmp_path / "rfq.db")
    rows = []
    for g in ("nfl-pit-no-2026-10-25", "nfl-mia-nyj-2026-10-25"):
        rows.append(_cat_market(f"{g}#ml0", g, 0)); rows.append(_cat_market(f"{g}#ml1", g, 1))
        for k in range(n_lines):
            s = f"{g}-spread-home-{k}pt5"
            rows.append(_cat_market(f"{s}#0", s, 0)); rows.append(_cat_market(f"{s}#1", s, 1))
    rows.append(_cat_market("nba-dal-hou-2026-10-21-total-230pt5#0", "nba-dal-hou-2026-10-21-total-230pt5", 0))
    rows.append(_cat_market("nba-dal-hou-2026-10-21-spread-home-2pt5#0", "nba-dal-hou-2026-10-21-spread-home-2pt5", 0))
    rows.append(_cat_market("uel-oly-spp-2026-10-22-exact-score-5-3#0", "uel-oly-spp-2026-10-22-exact-score-5-3", 0))
    rfq.upsert_markets(rows)
    rfq.close()
    return tmp_path / "rfq.db"


class _Legs:
    """Stand-in for RfqLegUniverse: fixed positions with RFQ counts."""
    def __init__(self, counts): self.rfq_counts = dict(counts)
    def __call__(self): return set(self.rfq_counts)


def _fake_gamma(starts: dict, calls: list):
    import httpx

    async def fake_get(self, url, params=None, **kw):
        calls.append((url, params))
        if url.endswith("/events"):
            g = params["slug"]
            if g not in starts:
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))
            mk = []
            for s in ({g} | {f"{g}-spread-home-{k}pt5" for k in range(3)} | {f"{g}-total-230pt5"}):
                mk.append({"slug": s, "conditionId": f"0x{s}", "clobTokenIds": json.dumps([f"tok:{s}#0", f"tok:{s}#1"]),
                           "positionIds": [f"{s}#ml0" if s == g else f"{s}#0", f"{s}#ml1" if s == g else f"{s}#1"],
                           "outcomes": json.dumps(["Yes", "No"]), "gameStartTime": starts[g], "endDate": "2026-10-26",
                           "sportsMarketType": "spreads", "line": 1.5})
            return httpx.Response(200, json=[{"id": 777, "slug": g, "markets": mk}], request=httpx.Request("GET", url))
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))
    return fake_get


async def test_game_universe_expands_legs_to_whole_games(tmp_path, monkeypatch):
    import httpx
    from rfqbot.books import GameUniverse
    rfq_db = _game_catalog(tmp_path)
    calls = []
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_gamma({
        "nfl-pit-no-2026-10-25": "2026-10-25 17:00:00+00", "nfl-mia-nyj-2026-10-25": "2026-10-25 17:00:00+00",
        "nba-dal-hou-2026-10-21": "2026-10-21T23:00:00Z"}, calls))
    store = BookStore(None)
    legs = _Legs({"nfl-pit-no-2026-10-25-spread-home-1pt5#0": 5, "nba-dal-hou-2026-10-21-total-230pt5#0": 2,
                  "uel-oly-spp-2026-10-22-exact-score-5-3#0": 9})
    monkeypatch.setattr("rfqbot.books_universe.now_ms", lambda: 1_776_000_000_000)   # 2026-04-12: games are within 72h? no -> use big lookahead
    gu = GameUniverse(store, rfq_db, legs, sports=("nfl", "nba"), max_tokens=20_000, lookahead_hours=24 * 365)
    out = await gu()
    # whole PIT-NO game (2 ml + 3 lines x 2) and whole DAL-HOU game (2 positions), soccer stays leg-level, MIA-NYJ untouched
    assert {p for p in out if p.startswith("nfl-pit-no")} == {f"nfl-pit-no-2026-10-25#ml{i}" for i in range(2)} | \
        {f"nfl-pit-no-2026-10-25-spread-home-{k}pt5#{i}" for k in range(3) for i in range(2)}
    assert not any(p.startswith("nfl-mia-nyj") for p in out)
    assert {p for p in out if p.startswith("nba-")} == {"nba-dal-hou-2026-10-21-total-230pt5#0", "nba-dal-hou-2026-10-21-spread-home-2pt5#0"}
    assert "uel-oly-spp-2026-10-22-exact-score-5-3#0" in out
    assert gu.report["nfl"]["games"] == 1 and gu.report["nfl"]["tokens"] == 8 and gu.report["nba"]["tokens"] == 2
    # Gamma event fill: one /events call per game, tokens for every market in the game, start time recorded
    assert sorted(p["slug"] for u, p in calls if u.endswith("/events")) == ["nba-dal-hou-2026-10-21", "nfl-pit-no-2026-10-25"]
    assert store.scalar("SELECT token_id FROM leg_tokens WHERE position_id='nfl-pit-no-2026-10-25-spread-home-2pt5#1'") == \
        "tok:nfl-pit-no-2026-10-25-spread-home-2pt5#1"
    assert store.scalar("SELECT game_start_time FROM leg_tokens WHERE position_id='nfl-pit-no-2026-10-25#ml0'") == "2026-10-25 17:00:00+00"
    assert store.scalar("SELECT event_id FROM games WHERE game_prefix='nfl-pit-no-2026-10-25'") == "777"
    # second refresh: cached, no new Gamma calls
    n = len(calls); await gu(); assert len(calls) == n


async def test_game_universe_lookahead_and_cap(tmp_path, monkeypatch):
    import httpx
    from rfqbot.books import GameUniverse
    rfq_db = _game_catalog(tmp_path)
    now = 1_776_000_000_000
    monkeypatch.setattr("rfqbot.books_universe.now_ms", lambda: now)
    far = "2026-10-25 17:00:00+00"                 # > 72 h after `now` (2026-04-12)
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_gamma({
        "nfl-pit-no-2026-10-25": far, "nfl-mia-nyj-2026-10-25": "2026-04-13T00:00:00Z"}, []))
    store = BookStore(None)
    legs = _Legs({"nfl-pit-no-2026-10-25-spread-home-1pt5#0": 50, "nfl-mia-nyj-2026-10-25-spread-home-0pt5#1": 1})
    gu = GameUniverse(store, rfq_db, legs, sports=("nfl",), max_tokens=20_000, lookahead_hours=72)
    out = await gu()
    assert not any(p.startswith("nfl-pit-no") and "#ml" in p for p in out)       # far game skipped (its leg stays)
    assert "nfl-mia-nyj-2026-10-25#ml0" in out
    assert gu.report["nfl"]["skipped_lookahead"] == 1 and gu.report["nfl"]["games"] == 1
    assert store.scalar("SELECT skip_reason FROM games WHERE game_prefix='nfl-pit-no-2026-10-25'") == "lookahead"
    # cap: both games near, budget fits only the higher-RFQ game
    gu2 = GameUniverse(store, rfq_db, _Legs({"nfl-pit-no-2026-10-25-spread-home-1pt5#0": 50,
                                             "nfl-mia-nyj-2026-10-25-spread-home-0pt5#1": 1}),
                       sports=("nfl",), max_tokens=8, lookahead_hours=24 * 365)
    out2 = await gu2()
    assert "nfl-pit-no-2026-10-25#ml0" in out2 and "nfl-mia-nyj-2026-10-25#ml0" not in out2
    assert gu2.report["nfl"]["skipped_cap"] == 1 and store.scalar("SELECT included FROM games WHERE game_prefix='nfl-mia-nyj-2026-10-25'") == 0


async def test_recorder_accepts_async_universe_and_unions(tmp_path):
    """The recorder awaits an async universe; GameUniverse's output is legs | game positions."""
    store = BookStore(None)
    seen = []

    async def universe():
        return {"A", "B"}

    async def mapper(pos):
        seen.append(set(pos)); return {p: f"tok_{p}" for p in pos}

    rec = BookRecorder(store, universe, mapper=mapper, refresh_sec=0.05, min_interval_ms=0)
    store.begin()
    await rec._refresh_universe()
    assert seen == [{"A", "B"}] and set(rec.owner) == {"tok_A", "tok_B"} and rec.position_of["tok_A"] == "A"
    for c in rec.conns:
        c.wanted.clear()
    for t in rec._tasks:
        t.cancel()


def test_game_prefix_and_time_parsing():
    from rfqbot.books import _parse_gamma_time, game_prefix_of   # re-exports used by rfqbot.history
    assert game_prefix_of("nfl-pit-no-2026-10-25-spread-home-3pt5") == "nfl-pit-no-2026-10-25"
    assert game_prefix_of("nba-dal-hou-2026-10-21-rebounds-cooper-flagg-6pt5") == "nba-dal-hou-2026-10-21"
    assert game_prefix_of("will-the-us-invade-iran-before-2027") is None
    assert _parse_gamma_time("2026-10-25 13:30:00+00") == 1_792_935_000_000
    assert _parse_gamma_time("2026-10-25T13:30:00Z") == 1_792_935_000_000
    assert _parse_gamma_time(None) is None and _parse_gamma_time("garbage") is None


def test_parse_iso_ms_spellings(monkeypatch):
    from datetime import datetime, timezone
    from rfqbot.timeutil import now_ms, parse_iso_ms
    t = 1_792_935_000_000                                             # 2026-10-25 13:30:00 UTC
    assert parse_iso_ms("2026-10-25T13:30:00Z") == t
    assert parse_iso_ms("2026-10-25T13:30:00+00:00") == t
    assert parse_iso_ms("2026-10-25 13:30:00+00") == t                 # Gamma's short offset
    assert parse_iso_ms("2026-10-25T09:30:00-04:00") == t              # ET offset converts, does not shift
    assert parse_iso_ms("2026-10-25T13:30:00.250Z") == t + 250
    assert parse_iso_ms("2026-10-25T13:30:00") == t                    # naive -> UTC regardless of the host zone
    assert parse_iso_ms("2026-10-25") == t - 13 * 3_600_000 - 30 * 60_000
    assert parse_iso_ms("  2026-10-25T13:30:00Z ") == t
    local = datetime(2026, 10, 25, 13, 30).astimezone()                # host zone, whatever it is
    assert parse_iso_ms("2026-10-25T13:30:00", assume_utc=False) == int(local.timestamp() * 1000)
    assert parse_iso_ms(None) is None and parse_iso_ms("") is None and parse_iso_ms("garbage") is None
    assert abs(now_ms() - int(datetime.now(timezone.utc).timestamp() * 1000)) < 1_000


def test_coverage_report_lines():
    from rfqbot.books import coverage_report
    store = BookStore(None)
    store.upsert_tokens([("p1", "0x1", 0, "Yes", "t1", "nfl-pit-no-2026-10-25-spread-home-1pt5", "1", "2026-10-25 13:30:00+00", None, 1),
                         ("p2", "0x2", 0, "Yes", "t2", "nfl-pit-no-2026-10-25-total-40pt5", "1", "2026-10-25 13:30:00+00", None, 1)])
    store.subscribed({"t1": "p1", "t2": "p2"}, 1000)
    store.begin(); store.insert_top("t1", 5000, None, (4000, 4200, 1.0, 1.0, 1.0, 1.0), "book"); store.commit()
    store.upsert_game({"game_prefix": "nfl-pit-no-2026-10-25", "sport": "nfl", "event_id": "1", "game_start_time": "x",
                       "n_positions": 2, "rfq_count": 10, "included": 1, "skip_reason": None, "fetched_ms": 1, "updated_ms": 1})
    lines = coverage_report(store, recent_ms=600_000, now=6000)
    assert any("nfl" in l and "games    1" in l and "tokens      2" in l and "50.0%" in l for l in lines), lines
    assert any("nfl-pit-no-2026-10-25" in l and "rfqs     10" in l for l in lines), lines
