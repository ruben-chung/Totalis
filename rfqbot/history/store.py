"""SQLite schema and helpers for data/history.db."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- one row per game prefix ({league}-{away}-{home}-{date}); main and side events roll up here
CREATE TABLE IF NOT EXISTS hist_games (
    game_prefix     TEXT PRIMARY KEY,
    sport           TEXT NOT NULL,
    event_ids       TEXT,                 -- json list of Gamma event ids (main + side events)
    event_slugs     TEXT,                 -- json list
    game_start_time TEXT,                 -- Gamma gameStartTime (UTC)
    end_date        TEXT,
    n_markets       INTEGER NOT NULL DEFAULT 0,
    n_resolved      INTEGER NOT NULL DEFAULT 0,
    pulled_ms       INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hist_markets (
    condition_id       TEXT PRIMARY KEY,
    game_prefix        TEXT NOT NULL,
    sport              TEXT NOT NULL,
    event_id           TEXT,
    event_slug         TEXT,
    slug               TEXT,
    title              TEXT,
    outcomes           TEXT,              -- json list
    token_ids          TEXT,              -- json list (clobTokenIds)
    position_ids       TEXT,              -- json list (Gamma positionIds; catalog ids when present)
    game_start_time    TEXT,
    end_date           TEXT,
    sports_market_type TEXT,
    line               REAL,
    outcome_prices     TEXT,              -- json list, final
    resolution_status  TEXT,              -- umaResolutionStatus
    closed             INTEGER,
    resolved           INTEGER NOT NULL DEFAULT 0,   -- 1 when outcome_prices are exactly 0/1 and status resolved
    pulled_ms          INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hist_markets_game ON hist_markets(game_prefix);

-- one row per outcome; LegSpec fields from the sport parser (NULL + parse_error when it did not parse)
CREATE TABLE IF NOT EXISTS hist_legs (
    condition_id  TEXT NOT NULL,
    outcome_index INTEGER NOT NULL,
    game_prefix   TEXT NOT NULL,
    token_id      TEXT,
    position_id   TEXT,
    outcome       TEXT,
    won           INTEGER,                -- 1 / 0; NULL when void (0.5/0.5) or unresolved
    family        TEXT,
    period        TEXT,
    side          TEXT,
    team          TEXT,
    line          REAL,
    bucket        TEXT,
    includes_ot   INTEGER,
    game_key      TEXT,
    canonical_key TEXT,
    contract_key  TEXT,
    parse_error   TEXT,
    PRIMARY KEY (condition_id, outcome_index)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hist_legs_game ON hist_legs(game_prefix);
CREATE INDEX IF NOT EXISTS ix_hist_legs_token ON hist_legs(token_id);

-- last CLOB price at or before kickoff - offset_min
CREATE TABLE IF NOT EXISTS hist_prices (
    token_id   TEXT NOT NULL,
    offset_min INTEGER NOT NULL,
    t          INTEGER NOT NULL,          -- unix seconds of the price point used
    p          REAL NOT NULL,
    PRIMARY KEY (token_id, offset_min)
) WITHOUT ROWID;

-- tokens checked for prices (hit or miss) so reruns are incremental; a miss for a past kickoff is permanent
CREATE TABLE IF NOT EXISTS hist_price_checks (
    token_id    TEXT PRIMARY KEY,
    kickoff_s   INTEGER,
    n_points    INTEGER NOT NULL,
    offsets     TEXT NOT NULL,            -- json list of offsets requested
    checked_ms  INTEGER NOT NULL
) WITHOUT ROWID;

-- resolution tracker bookkeeping (what was written to rfq.db market_resolutions, and when unresolved ones were last checked)
CREATE TABLE IF NOT EXISTS hist_resolve_checks (
    condition_id     TEXT PRIMARY KEY,
    end_date         TEXT,
    resolved_outcome TEXT,
    last_checked_ms  INTEGER NOT NULL
) WITHOUT ROWID;

-- every pair of parsed legs within a resolved game (FG/1H/2H families first, capped per game)
CREATE TABLE IF NOT EXISTS hist_pairs (
    game_prefix TEXT NOT NULL,
    sport       TEXT NOT NULL,
    cond_a      TEXT NOT NULL, idx_a INTEGER NOT NULL,
    cond_b      TEXT NOT NULL, idx_b INTEGER NOT NULL,
    family_a    TEXT, period_a TEXT, side_a TEXT, team_a TEXT, line_a REAL,
    family_b    TEXT, period_b TEXT, side_b TEXT, team_b TEXT, line_b REAL,
    won_a       INTEGER NOT NULL,
    won_b       INTEGER NOT NULL,
    PRIMARY KEY (cond_a, idx_a, cond_b, idx_b)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hist_pairs_game ON hist_pairs(game_prefix);
CREATE INDEX IF NOT EXISTS ix_hist_pairs_fam ON hist_pairs(sport, family_a, family_b);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class HistoryStore:
    def __init__(self, path: str | Path | None = "./data/history.db"):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._depth = 0

    # -- tx (nestable)
    def __enter__(self):
        if self._depth == 0:
            # IMMEDIATE: take the write lock up front so the busy timeout applies (a deferred tx that upgrades to a
            # write after another process committed gets SQLITE_BUSY immediately, which killed concurrent jobs).
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self

    def __exit__(self, et, ev, tb):
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT" if et is None else "ROLLBACK")
        return False

    def close(self) -> None:
        self.conn.close()

    def scalar(self, sql: str, *args) -> Any:
        r = self.conn.execute(sql, args).fetchone()
        return None if r is None else r[0]

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    # -- writers
    def upsert_game(self, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO hist_games(game_prefix, sport, event_ids, event_slugs, game_start_time, end_date, n_markets,"
            " n_resolved, pulled_ms) VALUES (:game_prefix, :sport, :event_ids, :event_slugs, :game_start_time, :end_date,"
            " :n_markets, :n_resolved, :pulled_ms) ON CONFLICT(game_prefix) DO UPDATE SET event_ids=excluded.event_ids,"
            " event_slugs=excluded.event_slugs, game_start_time=COALESCE(excluded.game_start_time, game_start_time),"
            " end_date=COALESCE(excluded.end_date, end_date), n_markets=excluded.n_markets, n_resolved=excluded.n_resolved,"
            " pulled_ms=excluded.pulled_ms", row)

    def upsert_market(self, row: dict) -> None:
        cols = ("condition_id", "game_prefix", "sport", "event_id", "event_slug", "slug", "title", "outcomes", "token_ids",
                "position_ids", "game_start_time", "end_date", "sports_market_type", "line", "outcome_prices",
                "resolution_status", "closed", "resolved", "pulled_ms")
        self.conn.execute(
            f"INSERT OR REPLACE INTO hist_markets({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})",
            {c: row.get(c) for c in cols})

    def upsert_leg(self, row: dict) -> None:
        cols = ("condition_id", "outcome_index", "game_prefix", "token_id", "position_id", "outcome", "won", "family",
                "period", "side", "team", "line", "bucket", "includes_ot", "game_key", "canonical_key", "contract_key",
                "parse_error")
        self.conn.execute(
            f"INSERT OR REPLACE INTO hist_legs({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})",
            {c: row.get(c) for c in cols})

    def insert_prices(self, token_id: str, points: dict[int, tuple[int, float]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO hist_prices(token_id, offset_min, t, p) VALUES (?,?,?,?)",
                              [(token_id, off, t, p) for off, (t, p) in points.items()])

    def record_price_check(self, token_id: str, kickoff_s: int | None, n_points: int, offsets: Iterable[int], at_ms: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO hist_price_checks VALUES (?,?,?,?,?)",
                          (token_id, kickoff_s, n_points, json.dumps(sorted(offsets)), at_ms))

    def record_resolve_check(self, condition_id: str, end_date: str | None, resolved_outcome: str | None, at_ms: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO hist_resolve_checks VALUES (?,?,?,?)",
                          (condition_id, end_date, resolved_outcome, at_ms))

    def insert_pairs(self, rows: Iterable[tuple]) -> int:
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO hist_pairs(game_prefix, sport, cond_a, idx_a, cond_b, idx_b, family_a, period_a, side_a,"
            " team_a, line_a, family_b, period_b, side_b, team_b, line_b, won_a, won_b)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(rows))
        return cur.rowcount
