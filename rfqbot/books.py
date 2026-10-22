"""Leg order-book recorder: CLOB market channel for every leg position seen in recent RFQ flow.

Why: the combo-markets catalog price is a placeholder, so pricing and the backtest need
point-in-time leg books. Every frame goes verbatim to a gz raw log (BOOK_LOG_DIR), and a
top-of-book row goes to books.db whenever the top or near-top depth of an asset changes.

Wire format (docs.polymarket.com/api-reference/wss/market):
    url        wss://ws-subscriptions-clob.polymarket.com/ws/market
    subscribe  {"assets_ids": [...], "type": "market"}
    later      {"operation": "subscribe" | "unsubscribe", "assets_ids": [...]}
    keepalive  client sends text "PING" every 10 s, server answers "PONG"
    events     book (full snapshot), price_change (level deltas), last_trade_price, tick_size_change
All prices / sizes / timestamps on the wire are strings.

Universe: leg position_ids of RFQs seen in the last BOOK_UNIVERSE_HOURS, read incrementally
from the listener's rfq.db (WAL reader, separate process), widened to whole games for the
configured sports; the universe and token-map classes live in rfqbot.books_universe and are
re-exported here. Tokens are spread over connections of at most BOOK_ASSETS_PER_CONN each and
added / removed dynamically.

Liveness: every `subscriptions` row carries the `conn_id` of the socket that holds the asset, and
is closed (`unsubscribed_ms`) when that socket disconnects, so an asset is live at time t iff it
has an open row whose connection was open at t. `book_connections.last_frame_ms` is the last
receive time per socket (persisted every flusher tick), which readers use to tell "connection
quiet" from "book unchanged".
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import websockets
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .books_universe import DEFAULT_GAMMA_URL, GameUniverse, RfqLegUniverse, TokenMap, _sport_of  # noqa: F401  (re-exports)
from .rawlog import RawLog
from .slugs import game_prefix_of  # noqa: F401  (re-export: rfqbot.history.pull imports it from here)
from .timeutil import now_ms, parse_iso_ms

log = logging.getLogger(__name__)

DEFAULT_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PRICE_SCALE = 10_000          # prices stored as integers in 1e-4 (finest CLOB tick)
DEPTH_WINDOW = 500            # near-top depth = size within 0.05 of the best price
WS_MAX_FRAME_BYTES = 16 * 1024 * 1024   # a full `book` snapshot list for 500 assets can exceed websockets' 1 MiB default

__all__ = ["Book", "BookStore", "BookConn", "BookRecorder", "RfqLegUniverse", "GameUniverse", "TokenMap",
           "identity_map", "coverage_report", "build_recorder", "game_prefix_of", "DEFAULT_GAMMA_URL",
           "DEFAULT_CLOB_WS_URL", "WS_MAX_FRAME_BYTES"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- one row per change of the top-of-book summary (not per wire event)
CREATE TABLE IF NOT EXISTS book_top (
    asset_id     TEXT NOT NULL,
    at_ms        INTEGER NOT NULL,     -- local receive time
    server_ts_ms INTEGER,              -- gateway timestamp from the event
    best_bid     INTEGER,              -- price * 10000, NULL if side empty
    best_ask     INTEGER,
    bid_size     REAL,                 -- shares at best
    ask_size     REAL,
    bid_depth    REAL,                 -- shares within 0.05 of best
    ask_depth    REAL,
    source       TEXT NOT NULL         -- 'book' snapshot | 'delta' price_change
);
CREATE INDEX IF NOT EXISTS ix_book_top_asset_time ON book_top(asset_id, at_ms);
CREATE INDEX IF NOT EXISTS ix_book_top_time ON book_top(at_ms);   -- coverage: rows in the last N minutes

-- whole-game ladder universe (GameUniverse): one row per game prefix seen via RFQ legs
CREATE TABLE IF NOT EXISTS games (
    game_prefix     TEXT PRIMARY KEY,   -- e.g. nfl-pit-no-2026-10-25
    sport           TEXT NOT NULL,
    event_id        TEXT,
    game_start_time TEXT,
    n_positions     INTEGER,            -- catalog positions under the prefix
    rfq_count       INTEGER,            -- RFQs touching the game in the leg window (ranking key)
    included        INTEGER NOT NULL DEFAULT 0,
    skip_reason     TEXT,               -- lookahead | cap | no_event
    fetched_ms      INTEGER,            -- last Gamma event fetch
    updated_ms      INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS leg_trades (
    asset_id     TEXT NOT NULL,
    at_ms        INTEGER NOT NULL,
    server_ts_ms INTEGER,
    price        INTEGER,
    size         REAL,
    side         TEXT,                 -- taker side
    fee_rate_bps TEXT,
    tx_hash      TEXT
);
CREATE INDEX IF NOT EXISTS ix_leg_trades_asset_time ON leg_trades(asset_id, at_ms);

CREATE TABLE IF NOT EXISTS tick_sizes (
    asset_id TEXT NOT NULL, at_ms INTEGER NOT NULL, old_tick TEXT, new_tick TEXT
);

-- RFQ leg position_id -> CLOB token (asset_id); game_start / event from Gamma for later stages
CREATE TABLE IF NOT EXISTS leg_tokens (
    position_id     TEXT PRIMARY KEY,
    condition_id    TEXT,
    outcome_index   INTEGER,
    outcome         TEXT,
    token_id        TEXT,                -- NULL = unresolved (not in catalog / Gamma yet)
    slug            TEXT,
    event_id        TEXT,
    game_start_time TEXT,
    end_date        TEXT,
    fetched_ms      INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_leg_tokens_token ON leg_tokens(token_id);

-- when each token entered / left the subscription set on one socket (provenance for the backtest and
-- liveness for the marginal layer): a row is opened when the socket connects with the asset or the
-- asset is added to a live socket, and closed when the asset is removed or the socket disconnects.
CREATE TABLE IF NOT EXISTS subscriptions (
    asset_id        TEXT NOT NULL,
    position_id     TEXT,
    subscribed_ms   INTEGER NOT NULL,
    unsubscribed_ms INTEGER,
    conn_id         TEXT                 -- book_connections.conn_id holding the asset (NULL before 2026-10-21)
);
CREATE INDEX IF NOT EXISTS ix_subs_asset ON subscriptions(asset_id);

CREATE TABLE IF NOT EXISTS book_connections (
    conn_id TEXT PRIMARY KEY, connected_at_ms INTEGER NOT NULL, n_assets INTEGER,
    disconnected_at_ms INTEGER, close_reason TEXT,
    last_frame_ms INTEGER                -- last frame received on this socket (any event or PONG)
);
"""

# columns added after the first books.db files were written; ALTERed in at startup
_MIGRATIONS = (("subscriptions", "conn_id", "TEXT"), ("book_connections", "last_frame_ms", "INTEGER"))
_POST_MIGRATION_SQL = "CREATE INDEX IF NOT EXISTS ix_subs_conn ON subscriptions(conn_id)"


def _px(p) -> int:
    return int(round(float(p) * PRICE_SCALE))


# ---------------------------------------------------------------------- book
class Book:
    """In-memory aggregated book for one asset: price(1e-4 int) -> size."""
    __slots__ = ("bids", "asks")

    def __init__(self):
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}

    def snapshot(self, bids: Iterable[dict], asks: Iterable[dict]) -> None:
        self.bids = {_px(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {_px(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}

    def delta(self, price, size, side: str) -> None:
        levels = self.bids if side == "BUY" else self.asks
        p, s = _px(price), float(size)
        if s <= 0:
            levels.pop(p, None)
        else:
            levels[p] = s

    def summary(self) -> tuple:
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        bs = self.bids[bb] if bb is not None else None
        as_ = self.asks[ba] if ba is not None else None
        bd = sum(s for p, s in self.bids.items() if p >= bb - DEPTH_WINDOW) if bb is not None else None
        ad = sum(s for p, s in self.asks.items() if p <= ba + DEPTH_WINDOW) if ba is not None else None
        return (bb, ba, bs, as_, bd, ad)


# --------------------------------------------------------------------- store
class BookStore:
    def __init__(self, path: str | Path | None):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for table, col, typ in _MIGRATIONS:
            if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.execute(_POST_MIGRATION_SQL)
        self._in_tx = False

    def begin(self) -> None:
        if not self._in_tx:
            self.conn.execute("BEGIN"); self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self.conn.execute("COMMIT"); self._in_tx = False

    def close(self) -> None:
        self.commit(); self.conn.close()

    def insert_top(self, asset_id: str, at_ms: int, server_ts: int | None, summ: tuple, source: str) -> None:
        self.conn.execute("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (asset_id, at_ms, server_ts, *summ, source))

    def insert_trade(self, asset_id: str, at_ms: int, ev: dict) -> None:
        self.conn.execute("INSERT INTO leg_trades VALUES (?,?,?,?,?,?,?,?)",
                          (asset_id, at_ms, _ts(ev), _px(ev.get("price", 0)), float(ev.get("size") or 0),
                           ev.get("side"), ev.get("fee_rate_bps"), ev.get("transaction_hash")))

    def insert_tick(self, asset_id: str, at_ms: int, ev: dict) -> None:
        self.conn.execute("INSERT INTO tick_sizes VALUES (?,?,?,?)",
                          (asset_id, at_ms, ev.get("old_tick_size"), ev.get("new_tick_size")))

    def subscribed(self, assets: dict[str, str], at_ms: int, conn_id: str | None = None) -> None:
        self.conn.executemany("INSERT INTO subscriptions(asset_id, position_id, subscribed_ms, conn_id) VALUES (?,?,?,?)",
                              [(a, p, at_ms, conn_id) for a, p in assets.items()])

    def tokens_cached(self, position_ids: Iterable[str]) -> dict[str, tuple[str | None, int]]:
        out = {}
        for pid in position_ids:
            r = self.conn.execute("SELECT token_id, fetched_ms FROM leg_tokens WHERE position_id=?", (pid,)).fetchone()
            if r is not None:
                out[pid] = (r[0], r[1])
        return out

    def upsert_game(self, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO games(game_prefix, sport, event_id, game_start_time, n_positions, rfq_count, included,"
            " skip_reason, fetched_ms, updated_ms) VALUES (:game_prefix, :sport, :event_id, :game_start_time,"
            " :n_positions, :rfq_count, :included, :skip_reason, :fetched_ms, :updated_ms)"
            " ON CONFLICT(game_prefix) DO UPDATE SET sport=excluded.sport,"
            " event_id=COALESCE(excluded.event_id, event_id), game_start_time=COALESCE(excluded.game_start_time, game_start_time),"
            " n_positions=excluded.n_positions, rfq_count=excluded.rfq_count, included=excluded.included,"
            " skip_reason=excluded.skip_reason, fetched_ms=COALESCE(excluded.fetched_ms, fetched_ms), updated_ms=excluded.updated_ms",
            row)

    def game(self, game_prefix: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM games WHERE game_prefix=?", (game_prefix,)).fetchone()

    def upsert_tokens(self, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO leg_tokens(position_id, condition_id, outcome_index, outcome, token_id, slug,"
            " event_id, game_start_time, end_date, fetched_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", list(rows))

    def close_stale_subscriptions(self, at_ms: int) -> int:
        """A killed recorder leaves rows open; close them at startup so coverage counts only this run."""
        return self.conn.execute("UPDATE subscriptions SET unsubscribed_ms=? WHERE unsubscribed_ms IS NULL", (at_ms,)).rowcount

    def unsubscribed(self, assets: Iterable[str], at_ms: int) -> None:
        self.conn.executemany("UPDATE subscriptions SET unsubscribed_ms=? WHERE asset_id=? AND unsubscribed_ms IS NULL",
                              [(at_ms, a) for a in assets])

    def conn_opened(self, conn_id: str, at_ms: int, n: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO book_connections(conn_id, connected_at_ms, n_assets) VALUES (?,?,?)",
                          (conn_id, at_ms, n))

    def unsubscribed_conn(self, conn_id: str, at_ms: int) -> int:
        """Close every open row held by one socket (it disconnected; the reconnect opens fresh rows)."""
        return self.conn.execute("UPDATE subscriptions SET unsubscribed_ms=? WHERE conn_id=? AND unsubscribed_ms IS NULL",
                                 (at_ms, conn_id)).rowcount

    def conn_closed(self, conn_id: str, at_ms: int, reason: str, last_frame_ms: int | None = None) -> None:
        self.conn.execute("UPDATE book_connections SET disconnected_at_ms=?, close_reason=?,"
                          " last_frame_ms=COALESCE(?, last_frame_ms) WHERE conn_id=?", (at_ms, reason, last_frame_ms, conn_id))

    def set_last_frame_ms(self, updates: dict[str, int]) -> None:
        self.conn.executemany("UPDATE book_connections SET last_frame_ms=? WHERE conn_id=?",
                              [(ms, cid) for cid, ms in updates.items()])

    def last_frame_ms(self, conn_id: str) -> int | None:
        """Persisted last receive time of one socket (what a reader of books.db sees)."""
        return self.scalar("SELECT last_frame_ms FROM book_connections WHERE conn_id=?", conn_id)

    def scalar(self, sql: str, *args):
        r = self.conn.execute(sql, args).fetchone()
        return None if r is None else r[0]


def _ts(ev: dict) -> int | None:
    t = ev.get("timestamp")
    try:
        return int(t) if t not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ universe
# RfqLegUniverse, GameUniverse and TokenMap live in rfqbot.books_universe and are re-exported above.
# rfqbot.history.* still imports the Gamma time parser under its old name; it is rfqbot.timeutil.parse_iso_ms.
_parse_gamma_time = parse_iso_ms


async def identity_map(position_ids: set[str]) -> dict[str, str]:
    return {p: p for p in position_ids}


# ---------------------------------------------------------------- connection
class BookConn:
    """One market-channel socket owning a set of assets; reconnects with backoff."""

    def __init__(self, rec: "BookRecorder", index: int):
        self.rec = rec
        self.index = index
        self.wanted: set[str] = set()
        self._ws: ClientConnection | None = None
        self._pending_add: set[str] = set()
        self._pending_remove: set[str] = set()
        self.conn_id = ""
        self.seq = 0
        self.stopped = False
        self.last_frame_ms: int | None = None   # receive time of the last frame on the current socket

    def __len__(self) -> int:
        return len(self.wanted)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def add(self, assets: set[str]) -> None:
        self.wanted |= assets
        self._pending_add |= assets
        self._pending_remove -= assets

    def remove(self, assets: set[str]) -> None:
        self.wanted -= assets
        self._pending_remove |= assets
        self._pending_add -= assets

    async def _flush_pending(self) -> None:
        ws = self._ws
        if ws is None:
            return
        if self._pending_add:
            await ws.send(json.dumps({"operation": "subscribe", "assets_ids": sorted(self._pending_add)}))
            self._pending_add.clear()
        if self._pending_remove:
            await ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": sorted(self._pending_remove)}))
            self._pending_remove.clear()

    async def run(self) -> None:
        backoff_n = 0
        while not self.rec.stopping:
            if not self.wanted:
                await asyncio.sleep(1)
                continue
            self.conn_id = f"b{self.index}-{now_ms():x}-{uuid.uuid4().hex[:6]}"
            self.seq = 0
            self.last_frame_ms = None
            got_frames = False
            reason = "closed"
            try:
                async with connect(self.rec.url, ping_interval=None, close_timeout=5, max_size=WS_MAX_FRAME_BYTES) as ws:
                    self._ws = ws
                    self._pending_add.clear(); self._pending_remove.clear()
                    self.rec.on_connected(self, len(self.wanted))
                    sub = {"assets_ids": sorted(self.wanted), "type": "market"}
                    if self.rec.custom_features:
                        sub["custom_feature_enabled"] = True
                    await ws.send(json.dumps(sub))
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        while not self.rec.stopping:
                            try:
                                text = await asyncio.wait_for(ws.recv(), timeout=self.rec.idle_timeout_s)
                            except asyncio.TimeoutError:
                                reason = f"idle {self.rec.idle_timeout_s}s"
                                break
                            if isinstance(text, bytes):
                                text = text.decode("utf-8", "replace")
                            got_frames = True
                            backoff_n = 0
                            self.seq += 1
                            self.rec.on_frame(self, text)
                            await self._flush_pending()
                    finally:
                        pinger.cancel()
            except ConnectionClosed as e:
                rc = e.rcvd or e.sent
                reason = f"closed {rc.code if rc else '?'} {rc.reason if rc else ''}".strip()
            except (OSError, websockets.InvalidURI, websockets.InvalidHandshake, asyncio.TimeoutError) as e:
                reason = f"{type(e).__name__}: {e}"
            except Exception as e:  # keep the recorder alive on anything unexpected
                log.exception("book conn %s unexpected error", self.conn_id)
                reason = f"{type(e).__name__}: {e}"
            finally:
                self._ws = None
                self.rec.on_disconnected(self, reason)
            if self.rec.stopping:
                break
            if not got_frames:
                backoff_n = min(backoff_n + 1, 8)
            delay = max(0.5, random.uniform(0, min(60.0, 2 ** backoff_n)))
            log.warning("book conn %d: %s; reconnecting in %.1fs", self.index, reason, delay)
            await asyncio.sleep(delay)

    async def _pinger(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self.rec.ping_interval_s)
            await ws.send("PING")
            await self._flush_pending()  # also delivers universe changes while the feed is quiet

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


# ----------------------------------------------------------------- recorder
class BookRecorder:
    def __init__(
        self,
        store: BookStore,
        universe: Callable[[], set[str]],
        *,
        mapper: Callable[[set[str]], Awaitable[dict[str, str]]] = identity_map,
        url: str = DEFAULT_CLOB_WS_URL,
        rawlog: RawLog | None = None,
        assets_per_conn: int = 500,
        refresh_sec: float = 60.0,
        ping_interval_s: float = 10.0,
        idle_timeout_s: float = 60.0,
        commit_interval_s: float = 0.5,
        custom_features: bool = False,
        min_interval_ms: int = 1_000,
        change_key: str = "top",
        raw_deltas: bool = False,
    ):
        # Measured 2026-10-20: ~2.5k price_change frames/s over ~16k tokens; without coalescing that is ~10 GB/day of
        # rows and ~30 GB/day of gz raw log, so: one row per asset per min_interval_ms, only when the top changes.
        self.min_interval_ms = min_interval_ms
        self.change_key = change_key            # "top": best px/size at best  |  "depth": also near-top depth
        self.raw_deltas = raw_deltas            # False: price_change frames are not raw-logged (book_top is the record)
        self._last_write_ms: dict[str, int] = {}
        self._dirty: dict[str, tuple[int | None, str]] = {}
        self.store = store
        self.universe = universe
        self.mapper = mapper
        self.url = url
        self.rawlog = rawlog
        self.assets_per_conn = assets_per_conn
        self.refresh_sec = refresh_sec
        self.ping_interval_s = ping_interval_s
        self.idle_timeout_s = idle_timeout_s
        self.commit_interval_s = commit_interval_s
        self.custom_features = custom_features
        self.conns: list[BookConn] = []
        self.owner: dict[str, BookConn] = {}       # token -> connection
        self.position_of: dict[str, str] = {}      # token -> RFQ position_id
        self.have_snapshot: set[str] = set()
        self.books: dict[str, Book] = {}
        self.last_summary: dict[str, tuple] = {}
        self.counts: dict[str, int] = {}
        self.stopping = False
        self._stop_ev: asyncio.Event | None = None   # created in run(); stop() before run() just sets `stopping`
        self._tasks: list[asyncio.Task] = []
        self._last_commit = time.monotonic()
        self._persisted_frame_ms: dict[str, int] = {}   # conn_id -> last_frame_ms written to book_connections

    # -- lifecycle ----------------------------------------------------------
    async def run(self) -> None:
        self._stop_ev = asyncio.Event()
        self.store.begin()
        self.store.close_stale_subscriptions(now_ms())
        self._tasks.append(asyncio.create_task(self._flusher(), name="book-flusher"))
        try:
            while not self.stopping:
                await self._refresh_universe()
                self.store.commit(); self.store.begin()
                try:
                    await asyncio.wait_for(self._stop_ev.wait(), timeout=self.refresh_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stopping = True
            for c in self.conns:
                await c.close()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._persist_liveness()
            self.store.commit()
            if self.rawlog is not None:
                self.rawlog.close()
            self._close_sources()

    def _close_sources(self) -> None:
        """Release the universe's and mapper's rfq.db connections (RfqLegUniverse / GameUniverse / TokenMap)."""
        for src in (self.universe, self.mapper):
            close = getattr(src, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:  # shutdown must finish; a closed-twice connection is not worth a traceback
                    log.warning("closing %s failed: %s", type(src).__name__, e)

    async def _flusher(self) -> None:
        """Write deferred (coalesced) top changes once their asset's interval has elapsed; persist socket liveness."""
        while True:
            await asyncio.sleep(min(0.25, self.min_interval_ms / 1000 or 0.25))
            self._flush_dirty(now_ms())
            self._persist_liveness()

    def _persist_liveness(self) -> None:
        """Write each socket's last_frame_ms to book_connections when it moved (not per frame: ~2.5k frames/s)."""
        updates = {c.conn_id: c.last_frame_ms for c in self.conns
                   if c.conn_id and c.last_frame_ms is not None and self._persisted_frame_ms.get(c.conn_id) != c.last_frame_ms}
        if updates:
            self.store.set_last_frame_ms(updates)
            self._persisted_frame_ms.update(updates)

    def last_frame_ms(self, conn_id: str | None = None) -> int | None:
        """In-memory last receive time: of one socket by conn_id, or the newest over all sockets."""
        if conn_id is not None:
            return next((c.last_frame_ms for c in self.conns if c.conn_id == conn_id), None)
        seen = [c.last_frame_ms for c in self.conns if c.last_frame_ms is not None]
        return max(seen) if seen else None

    def _flush_dirty(self, now: int) -> None:
        for a in [a for a, _ in self._dirty.items() if now - self._last_write_ms.get(a, 0) >= self.min_interval_ms]:
            ts, source = self._dirty.pop(a)
            b = self.books.get(a)
            if b is not None:
                self.store.insert_top(a, now, ts, b.summary(), source)
                self._last_write_ms[a] = now
                self._bump("top_rows")

    def stop(self) -> None:
        self.stopping = True
        if self._stop_ev is not None:
            self._stop_ev.set()

    # -- universe -> connections -------------------------------------------
    async def _refresh_universe(self) -> None:
        try:
            positions = self.universe()
            if inspect.isawaitable(positions):
                positions = await positions
            positions = set(positions)
            tokens = await self.mapper(positions)          # position_id -> token_id (unresolved ones dropped)
        except Exception as e:  # rfq.db may be mid-write or missing; keep the current set
            log.warning("universe refresh failed: %s", e)
            return
        desired = {t: p for p, t in tokens.items()}
        current = set(self.owner)
        added, removed = set(desired) - current, current - set(desired)
        now = now_ms()
        by_conn: dict[BookConn, set[str]] = {}
        for a in removed:
            by_conn.setdefault(self.owner.pop(a), set()).add(a)
            self.position_of.pop(a, None)
            self.books.pop(a, None); self.last_summary.pop(a, None); self.have_snapshot.discard(a)
        for c, assets in by_conn.items():
            c.remove(assets)
        if removed:
            self.store.unsubscribed(removed, now)
        live_added: dict[BookConn, dict[str, str]] = {}
        for a in sorted(added):
            c = self._conn_with_room()
            c.add({a}); self.owner[a] = c; self.position_of[a] = desired[a]
            if c.connected:   # a socket not yet open records its whole set in on_connected
                live_added.setdefault(c, {})[a] = desired[a]
        for c, assets in live_added.items():
            self.store.subscribed(assets, now, conn_id=c.conn_id)
        if added or removed:
            log.info("universe: %d positions -> %d tokens (+%d -%d) over %d connections", len(positions), len(self.owner),
                     len(added), len(removed), sum(1 for c in self.conns if len(c)))
        rep = getattr(self.universe, "report", None)
        if rep:
            log.info("game ladders: %s", "; ".join(f"{sp}: {r['games']} games {r['tokens']} tokens"
                                                   + (f" (skipped {r['skipped_lookahead']} lookahead, {r['skipped_cap']} cap)"
                                                      if r['skipped_lookahead'] or r['skipped_cap'] else "")
                                                   for sp, r in sorted(rep.items())))

    def _conn_with_room(self) -> BookConn:
        for c in self.conns:
            if len(c) < self.assets_per_conn:
                return c
        c = BookConn(self, len(self.conns))
        self.conns.append(c)
        self._tasks.append(asyncio.create_task(c.run(), name=f"book-conn-{c.index}"))
        return c

    # -- connection callbacks ----------------------------------------------
    def on_connected(self, c: BookConn, n: int) -> None:
        now = now_ms()
        self.store.conn_opened(c.conn_id, now, n)
        # every asset on this socket gets a fresh row under its conn_id (the previous socket's rows were closed)
        self.store.subscribed({a: self.position_of.get(a, "") for a in c.wanted}, now, conn_id=c.conn_id)
        log.info("book conn %d connected %s with %d assets", c.index, c.conn_id, n)

    def on_disconnected(self, c: BookConn, reason: str) -> None:
        now = now_ms()
        self.store.unsubscribed_conn(c.conn_id, now)
        self.store.conn_closed(c.conn_id, now, reason, c.last_frame_ms)
        if c.last_frame_ms is not None:
            self._persisted_frame_ms[c.conn_id] = c.last_frame_ms

    def on_frame(self, c: BookConn, text: str) -> None:
        at = now_ms()
        c.last_frame_ms = at
        if text == "PONG":
            self._bump("pong")
            return
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        events = obj if isinstance(obj, list) else [obj]
        is_delta = isinstance(obj, dict) and obj.get("event_type") == "price_change"
        if self.rawlog is not None and (self.raw_deltas or not is_delta):
            self.rawlog.write(at_ms=at, direction="in", conn_id=c.conn_id, seq=c.seq, text=text)
        if obj is None:
            self._bump("unparseable"); log.warning("unparseable book frame: %.200s", text)
            return
        for ev in events:
            try:
                self._apply(ev, at)
            except Exception as e:  # one bad event must not stop recording; the raw frame is on disk
                self._bump("apply_errors"); log.warning("book apply error: %s: %.300s", e, json.dumps(ev))
        if time.monotonic() - self._last_commit >= self.commit_interval_s:
            self.store.commit(); self.store.begin(); self._last_commit = time.monotonic()

    def _apply(self, ev: dict, at: int) -> None:
        if not isinstance(ev, dict):
            self._bump("non_object"); return
        et = ev.get("event_type") or "unknown"
        self._bump(et)
        if et == "book":
            a = str(ev.get("asset_id", ""))
            b = self.books.setdefault(a, Book())
            b.snapshot(ev.get("bids") or [], ev.get("asks") or [])
            self.have_snapshot.add(a)
            self._maybe_write_top(a, at, _ts(ev), "book")
        elif et == "price_change":
            ts = _ts(ev)
            touched: set[str] = set()
            for ch in ev.get("price_changes") or []:
                a = str(ch.get("asset_id", ""))
                if a not in self.have_snapshot:  # deltas also arrive for the complement token; no snapshot -> skip
                    self._bump("delta_without_snapshot"); continue
                self.books[a].delta(ch.get("price", 0), ch.get("size", 0), str(ch.get("side", "")).upper())
                touched.add(a)
            for a in touched:
                self._maybe_write_top(a, at, ts, "delta")
        elif et == "last_trade_price":
            self.store.insert_trade(str(ev.get("asset_id", "")), at, ev)
        elif et == "tick_size_change":
            self.store.insert_tick(str(ev.get("asset_id", "")), at, ev)

    def _maybe_write_top(self, asset_id: str, at: int, server_ts: int | None, source: str) -> None:
        summ = self.books[asset_id].summary()
        key = summ if self.change_key == "depth" else summ[:4]
        if self.last_summary.get(asset_id) == key:
            return
        self.last_summary[asset_id] = key
        if source == "book" or at - self._last_write_ms.get(asset_id, 0) >= self.min_interval_ms:
            self.store.insert_top(asset_id, at, server_ts, summ, source)
            self._last_write_ms[asset_id] = at
            self._dirty.pop(asset_id, None)
            self._bump("top_rows")
        else:  # too soon after the last row: remember, the flusher writes the then-current state
            self._dirty[asset_id] = (server_ts, source)

    def _bump(self, k: str, n: int = 1) -> None:
        self.counts[k] = self.counts.get(k, 0) + n


# ------------------------------------------------------------------ coverage
def coverage_report(store: BookStore, *, recent_ms: int = 600_000, now: int | None = None) -> list[str]:
    """Lines for `bookstats`: per sport games covered / tokens subscribed / share with a recent book_top row,
    and the ten highest-RFQ games with the lowest coverage."""
    now = now or now_ms()
    c = store.conn
    recent = {r[0] for r in c.execute("SELECT DISTINCT asset_id FROM book_top WHERE at_ms >= ?", (now - recent_ms,))}
    subs = c.execute("SELECT DISTINCT s.asset_id, t.slug FROM subscriptions s LEFT JOIN leg_tokens t ON t.token_id = s.asset_id"
                     " WHERE s.unsubscribed_ms IS NULL").fetchall()
    games = {r["game_prefix"]: r for r in c.execute("SELECT * FROM games")}
    game_sports = {r["sport"] for r in games.values()}
    per_sport: dict[str, list[int]] = {}
    per_game: dict[str, list[int]] = {}
    for asset, slug in subs:
        sport = _sport_of(slug or "") or "?"
        if sport not in game_sports:
            sport = "other (leg-level)"
        a = per_sport.setdefault(sport, [0, 0]); a[0] += 1; a[1] += asset in recent
        g = game_prefix_of(slug or "")
        if g:
            b = per_game.setdefault(g, [0, 0]); b[0] += 1; b[1] += asset in recent
    covered: dict[str, int] = {}
    for g, r in games.items():
        if r["included"]:
            covered[r["sport"]] = covered.get(r["sport"], 0) + 1
    lines = [f"coverage (book_top row in last {recent_ms // 60000} min):"]
    for sport, (n, hit) in sorted(per_sport.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  {sport:<18} games {covered.get(sport, 0):>4}  tokens {n:>6}  with recent book {hit / n if n else 0:6.1%}")
    ranked = sorted((r for r in games.values() if r["included"] and r["rfq_count"]), key=lambda r: -r["rfq_count"])[:30]
    worst = sorted(ranked, key=lambda r: (per_game.get(r["game_prefix"], [0, 0])[1] / max(1, per_game.get(r["game_prefix"], [1, 0])[0])))[:10]
    if worst:
        lines.append("  highest-RFQ games with the lowest coverage:")
        for r in worst:
            n, hit = per_game.get(r["game_prefix"], [0, 0])
            lines.append(f"    {r['game_prefix']:<34} rfqs {r['rfq_count']:>6}  tokens {n:>4}  recent {hit / n if n else 0:6.1%}"
                         f"  start {r['game_start_time'] or '?'}")
    return lines


# ----------------------------------------------------------------- entrypoint
def build_recorder(settings, *, db_path: str | Path | None = None, rfq_db: str | Path | None = None,
                   assets: Iterable[str] | None = None, assets_per_conn: int | None = None,
                   game_sports: Iterable[str] | None = None) -> tuple[BookStore, "BookRecorder"]:
    """Wire store + universe (legs, widened to whole games for `game_sports`) + token map + recorder from Settings."""
    store = BookStore(db_path or settings.book_db_path)
    rfq_db = rfq_db or settings.db_path
    if assets:  # explicit CLOB token ids, no mapping
        fixed = set(assets)
        universe, mapper = (lambda: fixed), identity_map
    else:
        legs = RfqLegUniverse(rfq_db, window_ms=int(settings.book_universe_hours * 3_600_000))
        sports = settings.book_game_sports if game_sports is None else tuple(game_sports)
        universe = GameUniverse(store, rfq_db, legs, sports=sports, max_tokens=settings.book_game_max_tokens,
                                lookahead_hours=settings.book_game_lookahead_hours, gamma_url=settings.gamma_url) if sports else legs
        mapper = TokenMap(store, rfq_db, gamma_url=settings.gamma_url)
    rawlog = RawLog(settings.book_log_dir) if settings.book_log_dir else None
    rec = BookRecorder(store, universe, mapper=mapper, url=settings.clob_ws_url, rawlog=rawlog,
                       assets_per_conn=assets_per_conn or settings.book_assets_per_conn, refresh_sec=settings.book_refresh_sec,
                       min_interval_ms=settings.book_min_interval_ms, change_key=settings.book_change_key,
                       raw_deltas=settings.book_raw_deltas)
    return store, rec


def main(argv: list[str] | None = None) -> int:
    """python -m rfqbot.books [--duration N] [--db PATH] [--rfq-db PATH] [--assets a,b] [--no-games]"""
    import argparse
    import signal
    from .config import load_settings

    ap = argparse.ArgumentParser(prog="rfqbot.books", description="record CLOB order books for RFQ legs and whole-game ladders")
    ap.add_argument("--env", default=".env"); ap.add_argument("--db", default=None); ap.add_argument("--rfq-db", default=None)
    ap.add_argument("--assets", default=None, help="comma-separated CLOB token ids instead of the RFQ-derived universe")
    ap.add_argument("--assets-per-conn", type=int, default=None)
    ap.add_argument("--no-games", action="store_true", help="leg-level only (ignore BOOK_GAME_SPORTS)")
    ap.add_argument("--duration", type=float, default=None); ap.add_argument("--log-level", default=None)
    args = ap.parse_args(argv)
    settings = load_settings(args.env)
    logging.basicConfig(level=getattr(logging, (args.log_level or settings.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("websockets").setLevel(logging.WARNING)
    store, rec = build_recorder(settings, db_path=args.db, rfq_db=args.rfq_db,
                                assets=args.assets.split(",") if args.assets else None,
                                assets_per_conn=args.assets_per_conn, game_sports=() if args.no_games else None)
    log.info("books db=%s raw_log=%s universe=%s game_sports=%s", store.path, settings.book_log_dir or "off",
             "fixed" if args.assets else f"rfq legs, last {settings.book_universe_hours}h",
             "off" if args.no_games or args.assets else ",".join(settings.book_game_sports))

    async def run() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, rec.stop)
        if args.duration:
            loop.call_later(args.duration, rec.stop)

        async def summary():
            prev: dict[str, int] = {}
            while True:
                await asyncio.sleep(60)
                cur = dict(rec.counts)
                delta = {k: cur[k] - prev.get(k, 0) for k in sorted(cur) if cur[k] - prev.get(k, 0)}
                prev = cur
                log.info("last 60s: %s | tokens=%d conns=%d", delta, len(rec.owner), sum(1 for c in rec.conns if len(c)))

        t = asyncio.create_task(summary())
        try:
            await rec.run()
            return 0
        finally:
            t.cancel(); store.close()

    return asyncio.run(run())


if __name__ == "__main__":
    import sys
    sys.exit(main())
