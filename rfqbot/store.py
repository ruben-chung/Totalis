"""SQLite persistence for normalized records plus low-volume raw frames.

Volume notes (measured 2026-10-20): the quoter feed carries ~190 RFQ_REQUEST/s.
Layout is sized for that:
  * combos / requesters are interned; an RFQ row holds integer references.
  * legs live on the combo, not the RFQ (the combo's condition_id is derived
    from its legs, so this is lossless).
  * `rfq_legs` is a view joining combo legs with the market catalog.
  * raw_messages holds every frame EXCEPT RFQ_REQUEST (those only go to the
    gzipped raw log, see rawlog.py). RAW_DB_TYPES=all overrides this.
  * events are recorded only for the quote/confirmation/execution path;
    RECEIVED / EXPIRED are implied by the rfqs row.

Single-writer: one Store per process, used from the asyncio loop. WAL mode keeps
readers (stats, backtest) from blocking the listener.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS connections (
    conn_id            TEXT PRIMARY KEY,
    attempt            INTEGER NOT NULL,
    connected_at_ms    INTEGER NOT NULL,
    auth_sent          INTEGER NOT NULL DEFAULT 0,
    auth_ok            INTEGER,
    auth_role          TEXT,
    auth_address       TEXT,
    auth_error         TEXT,
    disconnected_at_ms INTEGER,
    close_code         INTEGER,
    close_reason       TEXT
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id             INTEGER PRIMARY KEY,
    received_at_ms INTEGER NOT NULL,
    direction      TEXT NOT NULL CHECK (direction IN ('in','out')),
    conn_id        TEXT,
    seq            INTEGER,
    msg_type       TEXT NOT NULL,
    rfq_id         TEXT,
    quote_id       TEXT,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_rfq  ON raw_messages(rfq_id);
CREATE INDEX IF NOT EXISTS ix_raw_type ON raw_messages(msg_type);

CREATE TABLE IF NOT EXISTS requesters (
    id        INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS combos (
    id              INTEGER PRIMARY KEY,
    condition_id    TEXT NOT NULL UNIQUE,
    yes_position_id TEXT,
    no_position_id  TEXT,
    n_legs          INTEGER NOT NULL,
    first_seen_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS combo_legs (
    combo_id    INTEGER NOT NULL,
    leg_index   INTEGER NOT NULL,
    position_id TEXT NOT NULL,
    PRIMARY KEY (combo_id, leg_index)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_combo_legs_position ON combo_legs(position_id);

CREATE TABLE IF NOT EXISTS rfqs (
    rfq_id                 TEXT PRIMARY KEY,
    requester_id           INTEGER,
    combo_id               INTEGER,
    direction              TEXT,
    side                   TEXT,
    size_unit              TEXT,
    size_value_e6          INTEGER,
    submission_deadline_ms INTEGER,
    first_seen_ms          INTEGER NOT NULL,
    status                 TEXT NOT NULL,
    status_at_ms           INTEGER NOT NULL,
    stub                   INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_rfqs_open ON rfqs(submission_deadline_ms)
    WHERE status IN ('RECEIVED','QUOTED');
CREATE INDEX IF NOT EXISTS ix_rfqs_seen ON rfqs(first_seen_ms);

CREATE TABLE IF NOT EXISTS rfq_events (
    id             INTEGER PRIMARY KEY,
    rfq_id         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    at_ms          INTEGER NOT NULL,
    raw_message_id INTEGER,
    details        TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_rfq ON rfq_events(rfq_id, at_ms);

CREATE TABLE IF NOT EXISTS quotes (
    local_id        INTEGER PRIMARY KEY,
    rfq_id          TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    server_quote_id TEXT,
    price_e6        INTEGER NOT NULL,
    size_e6         INTEGER NOT NULL,
    source          TEXT NOT NULL DEFAULT 'collateral',
    is_paper        INTEGER NOT NULL DEFAULT 1,
    state           TEXT NOT NULL,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL,
    reason          TEXT,
    UNIQUE (rfq_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_quotes_server ON quotes(server_quote_id);

CREATE TABLE IF NOT EXISTS executions (
    id       INTEGER PRIMARY KEY,
    rfq_id   TEXT NOT NULL,
    quote_id TEXT,
    status   TEXT NOT NULL,
    tx_hash  TEXT,
    at_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exec_rfq ON executions(rfq_id);

CREATE TABLE IF NOT EXISTS trades (
    rfq_id         TEXT PRIMARY KEY,
    requester_id   INTEGER,
    combo_id       INTEGER,
    direction      TEXT,
    side           TEXT,
    price_e6       INTEGER,
    size_e6        INTEGER,
    executed_at_ms INTEGER,
    received_at_ms INTEGER,
    is_ours        INTEGER NOT NULL DEFAULT 0,
    raw_message_id INTEGER
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_trades_time ON trades(executed_at_ms);

CREATE TABLE IF NOT EXISTS settlements (
    rfq_id          TEXT PRIMARY KEY,
    tx_hash         TEXT,
    confirmed_at_ms INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS market_resolutions (
    condition_id     TEXT PRIMARY KEY,
    resolved_outcome TEXT,
    resolved_at_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS unmet_demand (
    id             INTEGER PRIMARY KEY,
    at_ms          INTEGER NOT NULL,
    window_ms      INTEGER,
    total_unfilled INTEGER,
    raw_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS unmet_demand_items (
    summary_id   INTEGER NOT NULL,
    combo_id     INTEGER,
    reason       TEXT,
    direction    TEXT,
    side         TEXT,
    bucket_unit  TEXT,
    min_value_e6 INTEGER,
    max_value_e6 INTEGER,
    count        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_unmet_items ON unmet_demand_items(summary_id);

CREATE TABLE IF NOT EXISTS markets (
    position_id   TEXT PRIMARY KEY,
    market_id     TEXT,
    condition_id  TEXT,
    slug          TEXT,
    title         TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    outcome_price REAL,
    volume        REAL,
    tags          TEXT,
    synced_at_ms  INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_markets_condition ON markets(condition_id);

CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker                   TEXT PRIMARY KEY,
    event_ticker             TEXT,
    series_ticker            TEXT NOT NULL,
    sport                    TEXT NOT NULL,
    title                    TEXT,
    yes_sub_title            TEXT,
    floor_strike             REAL,
    cap_strike               REAL,
    strike_type              TEXT,
    rules_primary            TEXT,
    rules_secondary          TEXT,
    expected_expiration_time TEXT,
    close_time               TEXT,
    status                   TEXT,
    synced_at_ms             INTEGER NOT NULL,
    parse_ok                 INTEGER NOT NULL,
    parse_error              TEXT,
    canonical_key            TEXT,
    game_key                 TEXT,
    away                     TEXT,
    home                     TEXT,
    game_date                TEXT,
    family                   TEXT,
    period                   TEXT,
    team                     TEXT,
    line                     REAL,
    side                     TEXT,
    includes_ot              INTEGER,
    rules_json               TEXT
);
CREATE INDEX IF NOT EXISTS ix_kalshi_series ON kalshi_markets(series_ticker);
CREATE INDEX IF NOT EXISTS ix_kalshi_game ON kalshi_markets(game_key);

CREATE VIEW IF NOT EXISTS rfq_legs AS
    SELECT r.rfq_id, l.leg_index, l.position_id,
           m.condition_id AS market_condition_id, m.slug AS market_slug, m.title AS market_title,
           m.outcome, m.outcome_price, m.tags
    FROM rfqs r
    JOIN combo_legs l ON l.combo_id = r.combo_id
    LEFT JOIN markets m ON m.position_id = l.position_id;

CREATE VIEW IF NOT EXISTS rfqs_v AS
    SELECT r.*, q.public_id AS requestor_public_id, c.condition_id, c.yes_position_id, c.no_position_id, c.n_legs
    FROM rfqs r
    LEFT JOIN requesters q ON q.id = r.requester_id
    LEFT JOIN combos c ON c.id = r.combo_id;
"""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RawRow:
    id: int
    received_at_ms: int
    direction: str
    conn_id: str | None
    seq: int | None
    msg_type: str
    rfq_id: str | None
    quote_id: str | None
    payload: str


class Store:
    def __init__(self, path: str | Path | None, *, wal: bool = True, busy_timeout_sec: float = 30.0):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # the listener writes continuously; side tools (kalshi-sync, stats) wait for the lock
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=busy_timeout_sec)
        self.conn.row_factory = sqlite3.Row
        if wal and self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-65536")  # 64 MB
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._tx_depth = 0
        self._combo_cache: dict[str, int] = {}
        self._requester_cache: dict[str, int] = {}

    def close(self) -> None:
        if self._tx_depth:
            self.conn.execute("COMMIT")
            self._tx_depth = 0
        self.conn.close()

    # -- transactions (nestable: only the outermost BEGINs/COMMITs) -------
    class _Tx:
        def __init__(self, store: "Store"):
            self.store = store

        def __enter__(self):
            if self.store._tx_depth == 0:
                self.store.conn.execute("BEGIN")
            self.store._tx_depth += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            self.store._tx_depth -= 1
            if self.store._tx_depth == 0:
                self.store.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
            return False

    def tx(self) -> "Store._Tx":
        return Store._Tx(self)

    # -- connections --------------------------------------------------------
    def connection_opened(self, conn_id: str, attempt: int, at_ms: int, auth_sent: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO connections(conn_id, attempt, connected_at_ms, auth_sent) VALUES (?,?,?,?)",
            (conn_id, attempt, at_ms, 1 if auth_sent else 0),
        )

    def connection_auth(self, conn_id: str, ok: bool, role: str = "", address: str = "", error: str = "") -> None:
        self.conn.execute(
            "UPDATE connections SET auth_ok=?, auth_role=?, auth_address=?, auth_error=? WHERE conn_id=?",
            (1 if ok else 0, role or None, address or None, error or None, conn_id),
        )

    def connection_closed(self, conn_id: str, code: int | None, reason: str, at_ms: int) -> None:
        self.conn.execute(
            "UPDATE connections SET disconnected_at_ms=?, close_code=?, close_reason=? WHERE conn_id=?",
            (at_ms, code, reason, conn_id),
        )

    # -- raw messages -------------------------------------------------------
    def insert_raw(self, *, received_at_ms: int, direction: str, conn_id: str | None, seq: int | None,
                   msg_type: str, rfq_id: str | None, quote_id: str | None, payload: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO raw_messages(received_at_ms, direction, conn_id, seq, msg_type, rfq_id, quote_id, payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (received_at_ms, direction, conn_id, seq, msg_type, rfq_id or None, quote_id or None, payload),
        )
        return int(cur.lastrowid)

    def iter_raw(self, *, direction: str | None = None, since_ms: int | None = None) -> Iterator[RawRow]:
        sql = "SELECT * FROM raw_messages"
        where, args = [], []
        if direction:
            where.append("direction=?"); args.append(direction)
        if since_ms is not None:
            where.append("received_at_ms>=?"); args.append(since_ms)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY received_at_ms, id"
        for r in self.conn.execute(sql, args):
            yield RawRow(**dict(r))

    # -- interning ----------------------------------------------------------
    def requester_id(self, public_id: str) -> int | None:
        if not public_id:
            return None
        rid = self._requester_cache.get(public_id)
        if rid is None:
            self.conn.execute("INSERT OR IGNORE INTO requesters(public_id) VALUES (?)", (public_id,))
            rid = int(self.conn.execute("SELECT id FROM requesters WHERE public_id=?", (public_id,)).fetchone()[0])
            if len(self._requester_cache) > 100_000:
                self._requester_cache.clear()
            self._requester_cache[public_id] = rid
        return rid

    def combo_id(self, condition_id: str, legs: Iterable[str] = (), *, yes: str = "", no: str = "",
                 at_ms: int = 0) -> int | None:
        """Intern a combo by condition_id; records legs/yes/no the first time it is seen."""
        if not condition_id:
            return None
        cid = self._combo_cache.get(condition_id)
        if cid is not None:
            return cid
        legs = list(legs)
        row = self.conn.execute("SELECT id, n_legs FROM combos WHERE condition_id=?", (condition_id,)).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO combos(condition_id, yes_position_id, no_position_id, n_legs, first_seen_ms)"
                " VALUES (?,?,?,?,?)", (condition_id, yes or None, no or None, len(legs), at_ms),
            )
            cid = int(cur.lastrowid)
            if legs:
                self.conn.executemany("INSERT OR IGNORE INTO combo_legs(combo_id, leg_index, position_id) VALUES (?,?,?)",
                                      [(cid, i, p) for i, p in enumerate(legs)])
        else:
            cid = int(row[0])
            if row[1] == 0 and legs:  # seen first via a message without legs; fill them now
                self.conn.executemany("INSERT OR IGNORE INTO combo_legs(combo_id, leg_index, position_id) VALUES (?,?,?)",
                                      [(cid, i, p) for i, p in enumerate(legs)])
                self.conn.execute("UPDATE combos SET n_legs=?, yes_position_id=COALESCE(yes_position_id,?),"
                                  " no_position_id=COALESCE(no_position_id,?) WHERE id=?",
                                  (len(legs), yes or None, no or None, cid))
        if len(self._combo_cache) > 500_000:
            self._combo_cache.clear()
        self._combo_cache[condition_id] = cid
        return cid

    def combo_legs(self, combo_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT position_id FROM combo_legs WHERE combo_id=? ORDER BY leg_index", (combo_id,))]

    # -- rfqs ---------------------------------------------------------------
    def insert_rfq(self, row: dict[str, Any]) -> bool:
        """Insert an RFQ if new. Returns True if inserted."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO rfqs(rfq_id, requester_id, combo_id, direction, side, size_unit, size_value_e6,"
            " submission_deadline_ms, first_seen_ms, status, status_at_ms, stub)"
            " VALUES (:rfq_id, :requester_id, :combo_id, :direction, :side, :size_unit, :size_value_e6,"
            " :submission_deadline_ms, :first_seen_ms, :status, :status_at_ms, :stub)",
            row,
        )
        return cur.rowcount == 1

    def get_rfq(self, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rfqs WHERE rfq_id=?", (rfq_id,)).fetchone()

    def get_rfq_full(self, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rfqs_v WHERE rfq_id=?", (rfq_id,)).fetchone()

    def set_rfq_status(self, rfq_id: str, status: str, at_ms: int) -> None:
        self.conn.execute("UPDATE rfqs SET status=?, status_at_ms=? WHERE rfq_id=?", (status, at_ms, rfq_id))

    def expire_open_rfqs(self, *, cutoff_ms: int, horizon_ms: int, now_ms: int,
                         default_window_ms: int) -> list[str]:
        """Mark open RFQs whose deadline + horizon is before cutoff as EXPIRED (skipping any with executions).
        Uses the partial index on open RFQs, so cost is proportional to open RFQs only."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT rfq_id FROM rfqs WHERE status IN ('RECEIVED','QUOTED')"
            " AND COALESCE(submission_deadline_ms, first_seen_ms + ?) + ? < ?"
            " AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.rfq_id = rfqs.rfq_id)",
            (default_window_ms, horizon_ms, cutoff_ms),
        )]
        if ids:
            self.conn.executemany("UPDATE rfqs SET status='EXPIRED', status_at_ms=? WHERE rfq_id=?",
                                  [(now_ms, i) for i in ids])
        return ids

    def add_event(self, rfq_id: str, event_type: str, at_ms: int, raw_message_id: int | None = None,
                  details: dict[str, Any] | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO rfq_events(rfq_id, event_type, at_ms, raw_message_id, details) VALUES (?,?,?,?,?)",
            (rfq_id, event_type, at_ms, raw_message_id, json.dumps(details) if details else None),
        )
        return int(cur.lastrowid)

    def events_for(self, rfq_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM rfq_events WHERE rfq_id=? ORDER BY at_ms, id", (rfq_id,)).fetchall()

    # -- quotes -------------------------------------------------------------
    def next_quote_revision(self, rfq_id: str) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(revision),0) FROM quotes WHERE rfq_id=?", (rfq_id,)).fetchone()
        return int(r[0]) + 1

    def insert_quote(self, *, rfq_id: str, revision: int, price_e6: int, size_e6: int, source: str, is_paper: bool,
                     state: str, at_ms: int, server_quote_id: str | None = None, reason: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO quotes(rfq_id, revision, server_quote_id, price_e6, size_e6, source, is_paper, state,"
            " created_at_ms, updated_at_ms, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rfq_id, revision, server_quote_id, price_e6, size_e6, source, 1 if is_paper else 0, state,
             at_ms, at_ms, reason),
        )
        return int(cur.lastrowid)

    def latest_quote(self, rfq_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM quotes WHERE rfq_id=? ORDER BY revision DESC LIMIT 1",
                                 (rfq_id,)).fetchone()

    def update_quote_state(self, local_id: int, state: str, at_ms: int, *, server_quote_id: str | None = None,
                           reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE quotes SET state=?, updated_at_ms=?, server_quote_id=COALESCE(?, server_quote_id),"
            " reason=COALESCE(?, reason) WHERE local_id=?",
            (state, at_ms, server_quote_id, reason, local_id),
        )

    def expire_live_quotes(self, rfq_ids: list[str], at_ms: int, state: str, reason: str | None = None) -> int:
        n = 0
        for rid in rfq_ids:
            n += self.conn.execute(
                "UPDATE quotes SET state=?, updated_at_ms=?, reason=COALESCE(?, reason) WHERE rfq_id=?"
                " AND state IN ('SUBMITTED','ACKED','SELECTED','CONFIRMED')", (state, at_ms, reason, rid),
            ).rowcount
        return n

    def quote_by_server_id(self, server_quote_id: str) -> sqlite3.Row | None:
        if not server_quote_id:
            return None
        return self.conn.execute("SELECT * FROM quotes WHERE server_quote_id=?", (server_quote_id,)).fetchone()

    # -- executions / trades / settlements ----------------------------------
    def insert_execution(self, rfq_id: str, quote_id: str | None, status: str, tx_hash: str | None, at_ms: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO executions(rfq_id, quote_id, status, tx_hash, at_ms) VALUES (?,?,?,?,?)",
            (rfq_id, quote_id or None, status, tx_hash or None, at_ms),
        )
        return int(cur.lastrowid)

    def has_execution(self, rfq_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM executions WHERE rfq_id=? LIMIT 1", (rfq_id,)).fetchone() is not None

    def insert_trade(self, row: dict[str, Any]) -> bool:
        """INSERT OR IGNORE on rfq_id: dedupes replayed RFQ_TRADE broadcasts. Returns True if new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO trades(rfq_id, requester_id, combo_id, direction, side, price_e6, size_e6,"
            " executed_at_ms, received_at_ms, is_ours, raw_message_id) VALUES (:rfq_id, :requester_id, :combo_id,"
            " :direction, :side, :price_e6, :size_e6, :executed_at_ms, :received_at_ms, :is_ours, :raw_message_id)",
            row,
        )
        return cur.rowcount == 1

    def mark_trade_ours(self, rfq_id: str) -> None:
        self.conn.execute("UPDATE trades SET is_ours=1 WHERE rfq_id=?", (rfq_id,))

    def insert_settlement(self, rfq_id: str, tx_hash: str | None, at_ms: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO settlements(rfq_id, tx_hash, confirmed_at_ms) VALUES (?,?,?)",
                                (rfq_id, tx_hash or None, at_ms))
        return cur.rowcount == 1

    # -- unmet demand -------------------------------------------------------
    def insert_unmet_demand(self, *, at_ms: int, window_ms: int, total_unfilled: int, raw_message_id: int | None,
                            items: list[dict[str, Any]]) -> int:
        cur = self.conn.execute(
            "INSERT INTO unmet_demand(at_ms, window_ms, total_unfilled, raw_message_id) VALUES (?,?,?,?)",
            (at_ms, window_ms, total_unfilled, raw_message_id),
        )
        sid = int(cur.lastrowid)
        self.conn.executemany(
            "INSERT INTO unmet_demand_items(summary_id, combo_id, reason, direction, side, bucket_unit, min_value_e6,"
            " max_value_e6, count) VALUES (?,?,?,?,?,?,?,?,?)",
            [(sid, it["combo_id"], it["reason"], it["direction"], it["side"], it["bucket_unit"],
              it["min_value_e6"], it["max_value_e6"], it["count"]) for it in items],
        )
        return sid

    # -- markets ------------------------------------------------------------
    def upsert_markets(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        self.conn.executemany(
            "INSERT INTO markets(position_id, market_id, condition_id, slug, title, outcome, outcome_index,"
            " outcome_price, volume, tags, synced_at_ms)"
            " VALUES (:position_id, :market_id, :condition_id, :slug, :title, :outcome, :outcome_index,"
            " :outcome_price, :volume, :tags, :synced_at_ms)"
            " ON CONFLICT(position_id) DO UPDATE SET market_id=excluded.market_id, condition_id=excluded.condition_id,"
            " slug=excluded.slug, title=excluded.title, outcome=excluded.outcome, outcome_index=excluded.outcome_index,"
            " outcome_price=excluded.outcome_price, volume=excluded.volume, tags=excluded.tags,"
            " synced_at_ms=excluded.synced_at_ms",
            rows,
        )
        return len(rows)

    def get_market(self, position_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM markets WHERE position_id=?", (position_id,)).fetchone()

    def unknown_positions(self, position_ids: Iterable[str]) -> list[str]:
        return [p for p in position_ids if self.get_market(p) is None]

    # -- kalshi -------------------------------------------------------------
    KALSHI_COLS = ("ticker", "event_ticker", "series_ticker", "sport", "title", "yes_sub_title", "floor_strike",
                   "cap_strike", "strike_type", "rules_primary", "rules_secondary", "expected_expiration_time",
                   "close_time", "status", "synced_at_ms", "parse_ok", "parse_error", "canonical_key", "game_key",
                   "away", "home", "game_date", "family", "period", "team", "line", "side", "includes_ot", "rules_json")

    def upsert_kalshi_markets(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = [{c: r.get(c) for c in self.KALSHI_COLS} for r in rows]
        cols = ", ".join(self.KALSHI_COLS)
        vals = ", ".join(f":{c}" for c in self.KALSHI_COLS)
        upd = ", ".join(f"{c}=excluded.{c}" for c in self.KALSHI_COLS if c != "ticker")
        self.conn.executemany(f"INSERT INTO kalshi_markets({cols}) VALUES ({vals}) ON CONFLICT(ticker) DO UPDATE SET {upd}", rows)
        return len(rows)

    def iter_kalshi_markets(self, sport: str | None = None, *, parsed_only: bool = False,
                            latest_only: bool = False) -> Iterator[sqlite3.Row]:
        """latest_only restricts to the most recent sync batch per sport: the sync fetches open
        markets and upserts, so a market that closed between syncs keeps its old row otherwise."""
        where, args = [], []
        if sport:
            where.append("sport=?"); args.append(sport)
        if parsed_only:
            where.append("parse_ok=1")
        if latest_only:
            where.append("synced_at_ms = (SELECT MAX(k2.synced_at_ms) FROM kalshi_markets k2 WHERE k2.sport = kalshi_markets.sport)")
        sql = "SELECT * FROM kalshi_markets" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ticker"
        yield from self.conn.execute(sql, args)

    # -- stats --------------------------------------------------------------
    def count_by(self, table: str, column: str, limit: int = 30) -> list[tuple[Any, int]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}")]

    def scalar(self, sql: str, *args) -> Any:
        r = self.conn.execute(sql, args).fetchone()
        return None if r is None else r[0]
