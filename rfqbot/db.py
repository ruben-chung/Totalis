"""Shared SQLite helpers.

Every reader in the project opens the recorders' databases read-only while the recorders keep
writing to them. This is the one place that connection is configured, so busy timeouts, the row
factory and thread affinity do not drift between modules.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_S = 30.0
# Chunk size for `WHERE x IN (...)` parameter lists; SQLite's default variable limit is 999 on
# older builds, so stay well under it.
SQLITE_IN_CHUNK = 500


def ro_connect(path: str | Path, *, timeout: float = DEFAULT_BUSY_TIMEOUT_S,
               rows: bool = True, shared: bool = True) -> sqlite3.Connection:
    """Open ``path`` read-only (``mode=ro`` URI, so a missing file raises instead of being created).

    ``rows`` installs ``sqlite3.Row``; ``shared`` allows the connection to be used from another
    thread (the recorders' readers hand connections to worker threads; the connection itself is
    never written through).
    """
    p = Path(path)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout, check_same_thread=not shared)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    if rows:
        conn.row_factory = sqlite3.Row
    return conn


def chunks(items: list, n: int = SQLITE_IN_CHUNK) -> list[list]:
    """Split ``items`` into lists of at most ``n`` for parameterised IN queries."""
    return [items[i:i + n] for i in range(0, len(items), n)]
