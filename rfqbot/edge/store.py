"""data/edge.db: counterfactual fills, fill curves, curse estimates, markouts, wallet scores."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    decision_id     INTEGER PRIMARY KEY,
    rfq_id          TEXT NOT NULL,
    at_ms           INTEGER NOT NULL,
    direction       TEXT,
    sport           TEXT,
    n_legs          INTEGER,
    same_game       INTEGER,
    p_fair          REAL,
    sigma           REAL,
    margin          REAL,
    price           REAL,
    size_e6         INTEGER,
    requester       TEXT,
    round_trip      INTEGER,
    printed         INTEGER,
    print_price     REAL,
    print_size_e6   INTEGER,
    executed_at_ms  INTEGER,
    delta           REAL,
    win_actual      INTEGER,
    fill_frac       REAL,
    undercut        REAL,
    segment         TEXT
);
CREATE INDEX IF NOT EXISTS ix_fills_rfq ON fills(rfq_id);
CREATE INDEX IF NOT EXISTS ix_fills_seg ON fills(segment);
CREATE TABLE IF NOT EXISTS markouts (
    rfq_id          TEXT NOT NULL,
    ref             TEXT NOT NULL,
    horizon_min     INTEGER NOT NULL,
    t0_ms           INTEGER NOT NULL,
    fair0           REAL,
    fair_h          REAL,
    markout         REAL,
    direction       TEXT,
    sport           TEXT,
    n_legs          INTEGER,
    same_game       INTEGER,
    sigma           REAL,
    segment         TEXT,
    requester       TEXT,
    print_price     REAL,
    computed_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (rfq_id, ref, horizon_min)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_markouts_t0 ON markouts(t0_ms);
CREATE TABLE IF NOT EXISTS curves (
    node            TEXT PRIMARY KEY,
    level           INTEGER,
    n               INTEGER,
    n_printed       INTEGER,
    fitted_at_ms    INTEGER NOT NULL,
    json            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cells (
    cell            TEXT PRIMARY KEY,
    node            TEXT,
    level           INTEGER,
    n               INTEGER,
    fitted_at_ms    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS curse (
    node            TEXT NOT NULL,
    horizon_min     INTEGER NOT NULL,
    ref             TEXT NOT NULL,
    fitted_at_ms    INTEGER NOT NULL,
    json            TEXT NOT NULL,
    PRIMARY KEY (node, horizon_min, ref)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS wallets (
    public_id       TEXT PRIMARY KEY,
    n               INTEGER,
    shrunk          REAL,
    upper           REAL,
    blocked         INTEGER,
    blocked_until_ms INTEGER,
    reason          TEXT,
    premium         REAL,
    updated_ms      INTEGER NOT NULL,
    json            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class EdgeStore:
    def __init__(self, path: str | Path | None):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- meta -------------------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    # -- fills --------------------------------------------------------------------------
    def put_fills(self, rows: Iterable[dict[str, Any]]) -> int:
        cols = ("decision_id", "rfq_id", "at_ms", "direction", "sport", "n_legs", "same_game", "p_fair", "sigma", "margin",
                "price", "size_e6", "requester", "round_trip", "printed", "print_price", "print_size_e6", "executed_at_ms",
                "delta", "win_actual", "fill_frac", "undercut", "segment")
        sql = f"INSERT OR REPLACE INTO fills({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        n = 0
        with self.conn:
            for r in rows:
                self.conn.execute(sql, tuple(r.get(c) for c in cols))
                n += 1
        return n

    # -- markouts -----------------------------------------------------------------------
    def put_markouts(self, rows: Iterable[dict[str, Any]]) -> int:
        cols = ("rfq_id", "ref", "horizon_min", "t0_ms", "fair0", "fair_h", "markout", "direction", "sport", "n_legs",
                "same_game", "sigma", "segment", "requester", "print_price", "computed_at_ms")
        sql = f"INSERT OR REPLACE INTO markouts({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        n = 0
        with self.conn:
            for r in rows:
                self.conn.execute(sql, tuple(r.get(c) for c in cols))
                n += 1
        return n

    def markout_done(self) -> dict[str, set[tuple[str, int]]]:
        """rfq_id -> {(ref, horizon)} already stored. A missing fair is point-in-time (the books never
        backfill), so a stored row is final; only horizons that had not matured are absent."""
        out: dict[str, set[tuple[str, int]]] = {}
        for r in self.conn.execute("SELECT rfq_id, ref, horizon_min FROM markouts"):
            out.setdefault(r[0], set()).add((r[1], int(r[2])))
        return out

    def markout_rows(self, since_ms: int | None = None) -> list[sqlite3.Row]:
        if since_ms is None:
            return self.conn.execute("SELECT * FROM markouts ORDER BY t0_ms").fetchall()
        return self.conn.execute("SELECT * FROM markouts WHERE t0_ms >= ? ORDER BY t0_ms", (since_ms,)).fetchall()

    # -- curves / cells / curse ---------------------------------------------------------
    def put_curves(self, curves: Iterable[Any], cells: dict[str, tuple[str, int]], cell_n: dict[str, int], at_ms: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM curves")
            self.conn.execute("DELETE FROM cells")
            for c in curves:
                self.conn.execute("INSERT INTO curves(node, level, n, n_printed, fitted_at_ms, json) VALUES (?,?,?,?,?,?)",
                                  (c.key, c.segment.level, c.n, c.n_printed, at_ms, json.dumps(c.as_dict())))
            for cell, (node, level) in cells.items():
                self.conn.execute("INSERT INTO cells(cell, node, level, n, fitted_at_ms) VALUES (?,?,?,?,?)",
                                  (cell, node, level, cell_n.get(cell, 0), at_ms))

    def put_curse(self, estimates: Iterable[Any], at_ms: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM curse")
            for e in estimates:
                self.conn.execute("INSERT OR REPLACE INTO curse(node, horizon_min, ref, fitted_at_ms, json) VALUES (?,?,?,?,?)",
                                  (e.segment, e.horizon_min, e.ref, at_ms, json.dumps(e.as_dict())))

    # -- wallets ------------------------------------------------------------------------
    def put_wallets(self, scores: Iterable[Any], at_ms: int) -> None:
        with self.conn:
            for s in scores:
                self.conn.execute("INSERT OR REPLACE INTO wallets(public_id, n, shrunk, upper, blocked, blocked_until_ms, reason,"
                                  " premium, updated_ms, json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (s.public_id, s.n, s.shrunk, s.upper, int(s.blocked), s.blocked_until_ms, s.reason, s.premium,
                                   at_ms, json.dumps(s.as_dict())))

    def wallet_scores(self) -> dict[str, Any]:
        from .wallets import WalletScore
        out: dict[str, WalletScore] = {}
        for r in self.conn.execute("SELECT json FROM wallets"):
            d = json.loads(r[0])
            out[d["public_id"]] = WalletScore(**d)
        return out
