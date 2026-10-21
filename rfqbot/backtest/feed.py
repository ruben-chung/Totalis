"""The recorded RFQ stream: ``RFQ_REQUEST`` frames from the raw log in time order, each once.

The raw log is one JSON object per line ``{"t": ms, "d": "in", "c": conn, "s": seq, "p": "<ws frame>"}``
in hourly files ``data/raw/YYYY-MM-DD/HH.jsonl.gz`` (UTC). Files wholly outside ``[start_ms, end_ms]``
are not opened. The live listener calls its handler once per new ``rfq_id``; so does this iterator.

``classify`` is the cheap sport pre-filter: 95% of RFQs have a leg outside the quoted sports and are skipped
by the live parser at no cost, so the replay counts them by reason instead of pricing them.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .. import messages as m
from .asof import ro

log = logging.getLogger(__name__)
_FILE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[/\\](\d{2})\.jsonl")


def file_window_ms(path: Path) -> tuple[int, int] | None:
    """[start, end) of the UTC hour a raw-log file covers, from its path; None when the name does not say."""
    mt = _FILE_RE.search(str(path))
    if mt is None:
        return None
    t = datetime.fromisoformat(mt.group(1)).replace(tzinfo=timezone.utc, hour=int(mt.group(2)))
    s = int(t.timestamp() * 1000)
    return s, s + 3_600_000


def raw_files(source: Path, start_ms: int, end_ms: int) -> list[Path]:
    if source.is_file():
        return [source]
    out = []
    for p in sorted(x for x in source.rglob("*.jsonl*") if x.is_file()):
        w = file_window_ms(p)
        if w is not None and (w[1] <= start_ms or w[0] > end_ms):
            continue
        out.append(p)
    return out


def iter_requests(source: Path, start_ms: int, end_ms: int, *, counts: dict[str, int] | None = None) -> Iterator[tuple[int, m.RfqRequest]]:
    """(received_ms, RfqRequest) for every first-seen RFQ_REQUEST with ``start_ms <= t <= end_ms``, non-decreasing in t."""
    seen: set[str] = set()
    last_t = 0
    c = counts if counts is not None else {}
    for path in raw_files(Path(source), start_ms, end_ms):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    if '\\"RFQ_REQUEST\\"' not in line and '"RFQ_REQUEST"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = int(obj.get("t") or obj.get("received_at_ms") or 0)
                        if obj.get("d", obj.get("direction", "in")) != "in":
                            continue
                        if t < start_ms:
                            continue
                        if t > end_ms:
                            return
                        text = obj["p"] if "p" in obj else obj.get("payload")
                        payload = json.loads(text) if isinstance(text, str) else text
                        if payload.get("type") != m.RFQ_REQUEST:
                            continue
                        msg = m.parse_inbound(payload)
                    except (json.JSONDecodeError, KeyError, m.ParseError, ValueError, TypeError):
                        c["unparseable"] = c.get("unparseable", 0) + 1
                        continue
                    if not isinstance(msg, m.RfqRequest) or msg.rfq_id in seen:
                        if isinstance(msg, m.RfqRequest):
                            c["duplicate"] = c.get("duplicate", 0) + 1
                        continue
                    seen.add(msg.rfq_id)
                    last_t = max(last_t, t)
                    c["frames"] = c.get("frames", 0) + 1
                    yield last_t, msg
        except EOFError:
            log.warning("%s ends in a truncated gzip block; read what was flushed", path)


class SportIndex:
    """position_id -> sport prefix ("nfl", "nba", ...) from the catalog, for the pre-filter. Loaded lazily per sport."""

    def __init__(self, rfq_db: str | Path, quoted: Iterable[str] = ("nfl", "nba")):
        self.quoted = tuple(quoted)
        conn = ro(rfq_db)
        try:
            self.quoted_pids: dict[str, str] = {}
            for sp in self.quoted:
                for r in conn.execute("SELECT position_id FROM markets WHERE slug LIKE ?", (f"{sp}-%",)):
                    self.quoted_pids[r[0]] = sp
            self.known: set[str] = set()
            for r in conn.execute("SELECT position_id FROM markets"):
                self.known.add(r[0])
        finally:
            conn.close()

    def classify(self, leg_position_ids: Iterable[str]) -> str:
        """quoted | other_sport | unknown_market (live reason order: an unknown leg is reported before a foreign sport)."""
        foreign = False
        for pid in leg_position_ids:
            if pid in self.quoted_pids:
                continue
            if pid not in self.known:
                return "unknown_market"
            foreign = True
        return "other_sport" if foreign else "quoted"
