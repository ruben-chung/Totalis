"""Re-normalize recorded raw frames into a fresh store.

Sources:
  * a raw-log directory (data/raw) or a single .jsonl / .jsonl.gz file written by RawLog
    (lines: {"t","d","c","s","p"}), or the simpler fixture form {"received_at_ms","direction","payload"};
  * an existing rfq.db (reads its raw_messages table — complete only if it was recorded with RAW_DB_TYPES=all).

The replay clock is the recorded receive time; the expiry sweeper runs against
that clock (once per replay-second) before each message, so no decision sees
the future. This is also the base of the backtest stage.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from . import messages as m
from .config import ACCEPTANCE_WINDOW_MS, LAST_LOOK_WINDOW_MS, SUBMISSION_WINDOW_MS
from .normalizer import Normalizer, PaperQuote
from .rawlog import iter_rawlog_dir, iter_rawlog_file
from .store import RawRow, Store

log = logging.getLogger(__name__)


def _row_from_obj(i: int, obj: dict) -> RawRow:
    if "p" in obj:  # RawLog format
        text = obj["p"]
        ts, direction, conn_id, seq = int(obj.get("t", 0)), obj.get("d", "in"), obj.get("c"), obj.get("s")
    else:           # fixture format
        payload = obj.get("payload", obj)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        ts, direction, conn_id, seq = int(obj.get("received_at_ms", 0)), obj.get("direction", "in"), obj.get("conn_id"), obj.get("seq")
    try:
        ptype, rfq_id, quote_id = m.extract_ids(json.loads(text))
    except json.JSONDecodeError:
        ptype, rfq_id, quote_id = "UNPARSEABLE", "", ""
    return RawRow(id=i, received_at_ms=ts, direction=direction, conn_id=conn_id, seq=seq,
                  msg_type=ptype or "UNKNOWN", rfq_id=rfq_id, quote_id=quote_id, payload=text)


def iter_source(source: Path) -> Iterator[RawRow]:
    if source.is_dir():
        for i, obj in enumerate(iter_rawlog_dir(source), 1):
            yield _row_from_obj(i, obj)
    elif source.name.endswith((".jsonl", ".jsonl.gz", ".ndjson", ".json")):
        for i, obj in enumerate(iter_rawlog_file(source), 1):
            yield _row_from_obj(i, obj)
    else:
        src = Store(source, wal=False)
        try:
            yield from list(src.iter_raw())
        finally:
            src.close()


def replay(source: Path, target: Store, *, expiry_grace_ms: int = 5_000, raw_db_types: str = "non_request",
           final_sweep: bool = True, progress_every: int = 100_000) -> dict[str, int]:
    norm = Normalizer(target, expiry_grace_ms=expiry_grace_ms)
    counts: dict[str, int] = {}
    last_ms = 0
    last_sweep_ms = 0
    n = 0
    tx = target.tx(); tx.__enter__()
    try:
        for row in iter_source(source):
            n += 1
            ts = row.received_at_ms or last_ms
            if ts > last_ms:
                last_ms = ts
            if ts - last_sweep_ms >= 1000:
                norm.expire_stale(ts)
                last_sweep_ms = ts
            counts[row.msg_type] = counts.get(row.msg_type, 0) + 1
            raw_id = None
            if raw_db_types == "all" or (raw_db_types == "non_request" and row.msg_type != m.RFQ_REQUEST):
                raw_id = target.insert_raw(received_at_ms=ts, direction=row.direction, conn_id=row.conn_id, seq=row.seq,
                                           msg_type=row.msg_type, rfq_id=row.rfq_id, quote_id=row.quote_id,
                                           payload=row.payload)
            try:
                obj = json.loads(row.payload)
            except json.JSONDecodeError:
                continue
            if row.direction == "out":
                if obj.get("type") == m.RFQ_QUOTE and obj.get("rfq_id"):
                    q = PaperQuote(price_e6=int(obj.get("price_e6", 0)), size_e6=int(obj.get("size_e6", 0)),
                                   source=obj.get("source", "collateral"), reason=obj.get("reason", ""))
                    try:
                        norm.record_paper_quote(obj["rfq_id"], q, at_ms=ts, is_paper=bool(obj.get("paper", True)))
                    except KeyError:
                        log.warning("paper quote for unknown rfq %s skipped", obj["rfq_id"])
                continue
            try:
                msg = m.parse_inbound(obj)
            except m.ParseError as e:
                log.warning("skip raw #%s: %s", row.id, e)
                continue
            norm.apply(msg, at_ms=ts, raw_id=raw_id)
            if n % 10_000 == 0:
                tx.__exit__(None, None, None); tx = target.tx(); tx.__enter__()
            if progress_every and n % progress_every == 0:
                log.info("replayed %d frames", n)
        if final_sweep and last_ms:
            # Close out anything still open at the end of the recording using the last
            # observed timestamp plus the full expiry horizon (no real clock involved).
            norm.expire_stale(last_ms + SUBMISSION_WINDOW_MS + ACCEPTANCE_WINDOW_MS + LAST_LOOK_WINDOW_MS
                              + expiry_grace_ms + 1)
    finally:
        tx.__exit__(None, None, None)
    return counts
