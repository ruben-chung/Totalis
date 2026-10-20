"""Decision log: one row per RFQ seen by the quote handler (the backtest's input)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    rfq_id          TEXT NOT NULL,
    at_ms           INTEGER NOT NULL,
    direction       TEXT,
    side            TEXT,
    requested_unit  TEXT,
    requested_e6    INTEGER,
    n_legs          INTEGER NOT NULL,
    legs_json       TEXT NOT NULL,
    clusters_json   TEXT NOT NULL,
    certified       INTEGER,
    p_fair          REAL,
    u_m             REAL,
    u_d             REAL,
    e_b             REAL,
    e_i             REAL,
    margin          REAL,
    maker_action    TEXT,
    price_e6        INTEGER,
    size_e6         INTEGER,
    decision        TEXT NOT NULL,
    reason          TEXT,
    latency_ms      REAL,
    model_built_at  REAL,
    config_json     TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_rfq ON decisions(rfq_id);
CREATE INDEX IF NOT EXISTS ix_decisions_at ON decisions(at_ms);
"""
COLS = ("rfq_id", "at_ms", "direction", "side", "requested_unit", "requested_e6", "n_legs", "legs_json", "clusters_json",
        "certified", "p_fair", "u_m", "u_d", "e_b", "e_i", "margin", "maker_action", "price_e6", "size_e6", "decision",
        "reason", "latency_ms", "model_built_at", "config_json")


class DecisionLog:
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
        self._sql = f"INSERT INTO decisions({', '.join(COLS)}) VALUES ({', '.join(':' + c for c in COLS)})"
        self.written = 0

    def write(self, row: dict[str, Any]) -> None:
        self.conn.execute(self._sql, {c: row.get(c) for c in COLS})
        self.written += 1

    def read(self, rfq_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM decisions WHERE rfq_id=? ORDER BY id", (rfq_id,)).fetchall()

    def counts(self, since_ms: int | None = None) -> list[tuple[str, int]]:
        where = "WHERE at_ms >= ?" if since_ms is not None else ""
        args = (since_ms,) if since_ms is not None else ()
        return [(r[0], r[1]) for r in self.conn.execute(f"SELECT decision, COUNT(*) FROM decisions {where} GROUP BY 1 ORDER BY 2 DESC", args)]

    def close(self) -> None:
        self.conn.close()


def decision_row(d: Any, config_json: str) -> dict[str, Any]:
    """Flatten an assemble.Decision into a decisions row."""
    return {
        "rfq_id": d.rfq_id, "at_ms": d.at_ms, "direction": d.direction, "side": d.side,
        "requested_unit": d.requested_unit, "requested_e6": d.requested_e6, "n_legs": d.n_legs,
        "legs_json": json.dumps([l.as_dict() for l in d.legs]), "clusters_json": json.dumps([c.as_dict() for c in d.clusters]),
        "certified": None if d.certified is None else int(d.certified), "p_fair": d.p_fair, "u_m": d.u_m, "u_d": d.u_d,
        "e_b": d.e_b, "e_i": d.e_i, "margin": d.margin, "maker_action": d.maker_action, "price_e6": d.price_e6,
        "size_e6": d.size_e6, "decision": d.decision, "reason": d.reason, "latency_ms": d.latency_ms,
        "model_built_at": d.model_built_at, "config_json": config_json,
    }
