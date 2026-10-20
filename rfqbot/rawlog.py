"""Append-only gzipped raw frame log, one file per UTC hour.

Every frame (in and out) is written verbatim as one JSON line:
    {"t": received_at_ms, "d": "in"|"out", "c": conn_id, "s": seq, "p": "<frame text>"}
Files: <dir>/YYYY-MM-DD/HH.jsonl.gz. gzip is sync-flushed every `flush_s`
seconds so a crash loses at most that much. A truncated trailing file is
readable up to the last flushed block (see iter_rawlog).
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterator

log = logging.getLogger(__name__)


class RawLog:
    def __init__(self, directory: str | Path, *, flush_s: float = 2.0, compresslevel: int = 6):
        self.dir = Path(directory)
        self.flush_s = flush_s
        self.compresslevel = compresslevel
        self._fh: IO[bytes] | None = None
        self._hour_key: str | None = None
        self._last_flush = time.monotonic()
        self.bytes_written = 0
        self.lines_written = 0

    @staticmethod
    def hour_key(at_ms: int) -> str:
        return datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d/%H")

    def path_for(self, at_ms: int) -> Path:
        return self.dir / f"{self.hour_key(at_ms)}.jsonl.gz"

    def write(self, *, at_ms: int, direction: str, conn_id: str | None, seq: int | None, text: str) -> None:
        key = self.hour_key(at_ms)
        if key != self._hour_key:
            self._rotate(at_ms, key)
        line = json.dumps({"t": at_ms, "d": direction, "c": conn_id, "s": seq, "p": text},
                          separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        assert self._fh is not None
        self._fh.write(data)
        self.bytes_written += len(data)
        self.lines_written += 1
        if time.monotonic() - self._last_flush >= self.flush_s:
            self.flush()

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()  # GzipFile.flush -> Z_SYNC_FLUSH + file flush
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._hour_key = None

    def _rotate(self, at_ms: int, key: str) -> None:
        self.close()
        path = self.path_for(at_ms)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append mode: a restart within the same hour adds a new gzip member to the file.
        self._fh = gzip.open(path, "ab", compresslevel=self.compresslevel)
        self._hour_key = key
        log.info("raw log -> %s", path)


def iter_rawlog_file(path: Path) -> Iterator[dict]:
    """Yield frames from one .jsonl(.gz) file; tolerate a truncated final block."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping corrupt line in %s", path)
    except EOFError:
        log.warning("%s ends in a truncated gzip block; read what was flushed", path)


def iter_rawlog_dir(directory: Path) -> Iterator[dict]:
    files = sorted(p for p in directory.rglob("*.jsonl*") if p.is_file())
    for p in files:
        yield from iter_rawlog_file(p)
