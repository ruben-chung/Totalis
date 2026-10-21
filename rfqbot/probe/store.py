"""data/probe.db: one row per probe (sent, dry-run or failed) plus budget lookups."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    id               INTEGER PRIMARY KEY,
    created_ms       INTEGER NOT NULL,
    mode             TEXT NOT NULL,          -- dry | live | fake
    trigger_rfq_id   TEXT NOT NULL,
    trigger_kind     TEXT NOT NULL,          -- print | fill
    trigger_condition TEXT,
    trigger_direction TEXT,
    trigger_price    REAL,
    trigger_size_e6  INTEGER,
    trigger_at_ms    INTEGER,
    trigger_requester TEXT,
    decision_id      INTEGER,
    our_price        REAL,
    our_p_fair       REAL,
    our_margin       REAL,
    our_sigma        REAL,
    segment          TEXT,
    horizon_sec      INTEGER NOT NULL,
    direction        TEXT NOT NULL,          -- probe direction
    legs_json        TEXT NOT NULL,          -- combo legs (undisguised)
    extra_leg_json   TEXT,
    p_L              REAL,
    p_L_spread       REAL,
    body_json        TEXT NOT NULL,
    response_json    TEXT,
    status           TEXT NOT NULL,
    probe_rfq_id     TEXT,
    requestor_public_id TEXT,
    blended_price    REAL,
    backed_out_price REAL,
    backed_out_err   REAL,
    leg_fair_send    REAL,                   -- model combo fair (undisguised legs) at send time
    leg_fair_poly    REAL,
    leg_fair_kalshi  REAL,
    latency_ms       REAL,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS ix_probes_created ON probes(created_ms);
CREATE INDEX IF NOT EXISTS ix_probes_trigger ON probes(trigger_rfq_id, horizon_sec, direction);
CREATE INDEX IF NOT EXISTS ix_probes_condition ON probes(trigger_condition, created_ms);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
COLS = ("created_ms", "mode", "trigger_rfq_id", "trigger_kind", "trigger_condition", "trigger_direction", "trigger_price",
        "trigger_size_e6", "trigger_at_ms", "trigger_requester", "decision_id", "our_price", "our_p_fair", "our_margin", "our_sigma",
        "segment", "horizon_sec", "direction", "legs_json", "extra_leg_json", "p_L", "p_L_spread", "body_json", "response_json",
        "status", "probe_rfq_id", "requestor_public_id", "blended_price", "backed_out_price", "backed_out_err", "leg_fair_send",
        "leg_fair_poly", "leg_fair_kalshi", "latency_ms", "error")


def now_ms() -> int:
    return int(time.time() * 1000)


class ProbeStore:
    def __init__(self, path: str | Path | None):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def put(self, row: dict[str, Any]) -> int:
        cur = self.conn.execute(f"INSERT INTO probes({', '.join(COLS)}) VALUES ({', '.join(':' + c for c in COLS)})",
                                {c: row.get(c) for c in COLS})
        return int(cur.lastrowid)

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    # -- budget ---------------------------------------------------------------------------
    def count_since(self, since_ms: int, *, modes: Iterable[str] = ("live", "fake", "dry")) -> int:
        m = list(modes)
        return int(self.conn.execute(f"SELECT COUNT(*) FROM probes WHERE created_ms >= ? AND mode IN ({','.join('?' * len(m))})",
                                     (since_ms, *m)).fetchone()[0])

    def combo_count_since(self, condition_id: str, since_ms: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM probes WHERE trigger_condition = ? AND created_ms >= ?",
                                     (condition_id, since_ms)).fetchone()[0])

    def last_combo_ms(self, condition_id: str) -> int | None:
        r = self.conn.execute("SELECT MAX(created_ms) FROM probes WHERE trigger_condition = ?", (condition_id,)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def done(self, trigger_rfq_id: str, horizon_sec: int, direction: str) -> bool:
        return self.conn.execute("SELECT 1 FROM probes WHERE trigger_rfq_id = ? AND horizon_sec = ? AND direction = ? LIMIT 1",
                                 (trigger_rfq_id, horizon_sec, direction)).fetchone() is not None

    def segment_counts(self, since_ms: int | None = None) -> dict[str, int]:
        where = "WHERE created_ms >= ?" if since_ms is not None else ""
        args = (since_ms,) if since_ms is not None else ()
        return {r[0] or "": int(r[1]) for r in self.conn.execute(f"SELECT segment, COUNT(*) FROM probes {where} GROUP BY 1", args)}

    def rows(self, since_ms: int | None = None, *, status: str | None = None) -> list[sqlite3.Row]:
        where, args = [], []
        if since_ms is not None:
            where.append("created_ms >= ?"); args.append(since_ms)
        if status is not None:
            where.append("status = ?"); args.append(status)
        sql = "SELECT * FROM probes" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ms, id"
        return self.conn.execute(sql, args).fetchall()

    def for_trigger(self, trigger_rfq_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM probes WHERE trigger_rfq_id = ? ORDER BY horizon_sec, id", (trigger_rfq_id,)).fetchall()
