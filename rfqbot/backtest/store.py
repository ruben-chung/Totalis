"""data/backtest.db: runs, decisions (with fill and settlement columns), fills, the ES1 path. The only file the backtest writes."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ..quoting.decisions import COLS as DECISION_COLS

DEFAULT_DB = Path("./data/backtest.db")

# decision columns stored as written by the live schema (so edge/risk readers work on a run unchanged), plus:
EXTRA_COLS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT NOT NULL"), ("sport", "TEXT"), ("games_json", "TEXT"), ("pair_class", "TEXT"),
    ("printed", "INTEGER"), ("fill_win", "INTEGER"), ("fill_strict", "INTEGER"), ("fill_frac", "REAL"),
    ("fill_size_e6", "INTEGER"), ("print_price", "REAL"), ("print_size_e6", "INTEGER"), ("executed_at_ms", "INTEGER"),
    ("delta", "REAL"),
)
SCHEMA_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    group_id     TEXT,                  -- runs of one compare share a group (one pass over the window)
    config_json  TEXT NOT NULL,         -- BacktestConfig
    quote_json   TEXT NOT NULL,         -- the QuoteConfig actually used
    window_json  TEXT NOT NULL,
    table_json   TEXT,                  -- the margin table snapshot used (reproducibility)
    started_ms   INTEGER NOT NULL,
    finished_ms  INTEGER,
    summary_json TEXT                   -- counts, skip reasons, latency, ES1 max, replay seconds
);
CREATE TABLE IF NOT EXISTS es1_path (
    run_id TEXT NOT NULL, at_ms INTEGER NOT NULL, es1 REAL, es1_frac REAL, positions INTEGER, pending INTEGER, notional REAL
);
CREATE INDEX IF NOT EXISTS ix_es1_run ON es1_path(run_id, at_ms);
CREATE TABLE IF NOT EXISTS settlements (
    run_id TEXT NOT NULL, rfq_id TEXT NOT NULL, status TEXT NOT NULL,   -- won | lost | unresolved | void
    payoff REAL, pnl REAL, mark REAL, unrealized REAL, legs_resolved INTEGER, legs_total INTEGER,
    source TEXT, settled_ms INTEGER, PRIMARY KEY (run_id, rfq_id)
) WITHOUT ROWID;
"""


def decisions_schema() -> str:
    cols = ["id INTEGER PRIMARY KEY"] + [f"{c} TEXT" if c in ("rfq_id", "direction", "side", "requested_unit", "legs_json", "clusters_json",
                                                              "maker_action", "decision", "reason", "config_json", "requester",
                                                              "condition_id", "binding_cap", "headroom_json", "book_json",
                                                              "edge_segment", "edge_source") else
                                        f"{c} INTEGER" if c in ("at_ms", "requested_e6", "n_legs", "certified", "price_e6", "size_e6",
                                                                "deadline_ms", "expires_ms", "pending_same_combo", "pending_requesters",
                                                                "edge_level", "edge_n") else f"{c} REAL" for c in DECISION_COLS]
    cols += [f"{c} {t}" for c, t in EXTRA_COLS]
    return (f"CREATE TABLE IF NOT EXISTS decisions ({', '.join(cols)});"
            "CREATE INDEX IF NOT EXISTS ix_bt_dec_run ON decisions(run_id, at_ms);"
            "CREATE INDEX IF NOT EXISTS ix_bt_dec_rfq ON decisions(run_id, rfq_id);")


ALL_DECISION_COLS = tuple(DECISION_COLS) + tuple(c for c, _ in EXTRA_COLS)


class BacktestStore:
    def __init__(self, path: str | Path | None = DEFAULT_DB):
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA_RUNS + decisions_schema())
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(decisions)")}
        for c in ALL_DECISION_COLS:                       # columns the quoting schema gained since this db was created
            if c not in have:
                self.conn.execute(f"ALTER TABLE decisions ADD COLUMN {c} TEXT")
        self._ins = (f"INSERT INTO decisions({', '.join(ALL_DECISION_COLS)}) VALUES ({', '.join(':' + c for c in ALL_DECISION_COLS)})")

    def close(self) -> None:
        self.conn.close()

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # -- writes ------------------------------------------------------------------------
    def start_run(self, run_id: str, name: str, config_json: str, quote_json: str, window_json: str, *, group_id: str | None = None,
                  table_json: str | None = None) -> None:
        self.conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        for t in ("decisions", "es1_path", "settlements"):
            self.conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
        self.conn.execute("INSERT INTO runs(run_id, name, group_id, config_json, quote_json, window_json, table_json, started_ms)"
                          " VALUES (?,?,?,?,?,?,?,?)", (run_id, name, group_id, config_json, quote_json, window_json, table_json,
                                                        int(time.time() * 1000)))

    def finish_run(self, run_id: str, summary: dict[str, Any]) -> None:
        self.conn.execute("UPDATE runs SET finished_ms=?, summary_json=? WHERE run_id=?",
                          (int(time.time() * 1000), json.dumps(summary, sort_keys=True, default=str), run_id))

    def insert_decisions(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        batch = []
        for r in rows:
            batch.append({c: r.get(c) for c in ALL_DECISION_COLS})
            n += 1
        if batch:
            self.conn.executemany(self._ins, batch)
        return n

    def insert_es1(self, run_id: str, rows: Iterable[tuple]) -> None:
        self.conn.executemany("INSERT INTO es1_path(run_id, at_ms, es1, es1_frac, positions, pending, notional) VALUES (?,?,?,?,?,?,?)",
                              [(run_id, *r) for r in rows])

    # -- reads -------------------------------------------------------------------------
    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def runs(self, group_id: str | None = None) -> list[sqlite3.Row]:
        if group_id:
            return self.conn.execute("SELECT * FROM runs WHERE group_id=? ORDER BY started_ms, run_id", (group_id,)).fetchall()
        return self.conn.execute("SELECT * FROM runs ORDER BY started_ms DESC").fetchall()

    def decisions(self, run_id: str, where: str = "", *args) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM decisions WHERE run_id=? {where} ORDER BY id", (run_id, *args)).fetchall()
