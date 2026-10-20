"""prices: pre-kickoff CLOB price snapshots -> hist_prices.

One prices-history request per token covering [kickoff - max_offset - lookback, kickoff] at 1-minute
fidelity; for each offset the last point at or before kickoff - offset is kept. A token with no
points is recorded in hist_price_checks (a miss for a past kickoff is permanent, so it is not retried).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

from ..books import _parse_gamma_time
from .gamma import Http
from .store import HistoryStore, now_ms

log = logging.getLogger(__name__)

DEFAULT_OFFSETS = (60, 15, 5)
LOOKBACK_S = 6 * 3600          # how far before the earliest offset we look for a last price


def pick_points(history: Iterable[dict], kickoff_s: int, offsets: Iterable[int]) -> dict[int, tuple[int, float]]:
    """For each offset, the last (t, p) with t <= kickoff - offset*60. Pure; used by tests."""
    pts = sorted((int(h["t"]), float(h["p"])) for h in history if "t" in h and "p" in h)
    out: dict[int, tuple[int, float]] = {}
    for off in offsets:
        cutoff = kickoff_s - int(off) * 60
        best = None
        for t, p in pts:
            if t <= cutoff:
                best = (t, p)
            else:
                break
        if best is not None:
            out[int(off)] = best
    return out


def tokens_to_check(store: HistoryStore, *, sport: str | None = None, offsets: Iterable[int] = DEFAULT_OFFSETS,
                    include_props: bool = False, only_resolved: bool = True, now_s: int | None = None) -> list[tuple[str, int]]:
    """(token_id, kickoff_s) for legs whose kickoff has passed and that were not checked for these offsets yet."""
    now_s = now_s or now_ms() // 1000
    want = json.dumps(sorted(int(o) for o in offsets))
    sql = ("SELECT l.token_id, m.game_start_time FROM hist_legs l JOIN hist_markets m USING(condition_id)"
           " LEFT JOIN hist_price_checks c ON c.token_id = l.token_id AND c.offsets = ?"
           " WHERE l.token_id IS NOT NULL AND c.token_id IS NULL")
    args: list = [want]
    if sport:
        sql += " AND m.sport = ?"; args.append(sport)
    if only_resolved:
        sql += " AND m.resolved = 1"
    if not include_props:
        sql += " AND l.parse_error IS NULL"       # player props and other unparsed markets are off by default
    out = []
    for token, start in store.rows(sql, *args):
        ms = _parse_gamma_time(start)
        if ms and ms // 1000 <= now_s:
            out.append((token, ms // 1000))
    return out


async def snapshot_prices(store: HistoryStore, *, sport: str | None = None, offsets: Iterable[int] = DEFAULT_OFFSETS,
                          http: Http | None = None, include_props: bool = False, limit: int | None = None,
                          fidelity_min: int = 1, progress_every: int = 500) -> dict[str, int]:
    offsets = tuple(int(o) for o in offsets)
    todo = tokens_to_check(store, sport=sport, offsets=offsets, include_props=include_props)
    if limit:
        todo = todo[:limit]
    own = http is None
    http = http or Http()
    counts = {"tokens": len(todo), "with_history": 0, "no_history": 0, "errors": 0}
    for off in offsets:
        counts[f"offset_{off}"] = 0
    span = max(offsets) * 60 + LOOKBACK_S
    done = 0

    async def one(token: str, kick: int) -> None:
        nonlocal done
        try:
            hist = await http.price_history(token, kick - span, kick, fidelity_min)
        except Exception as e:
            counts["errors"] += 1
            log.warning("prices-history %s failed: %s", token[:12], e)
            return
        pts = pick_points(hist, kick, offsets)
        with store:
            if pts:
                store.insert_prices(token, pts)
            store.record_price_check(token, kick, len(hist), offsets, now_ms())
        counts["with_history" if hist else "no_history"] += 1
        for off in pts:
            counts[f"offset_{off}"] += 1
        done += 1
        if progress_every and done % progress_every == 0:
            log.info("prices: %d/%d tokens (%d with history)", done, len(todo), counts["with_history"])

    try:
        # bounded concurrency comes from Http.sem; gather in chunks so memory stays flat
        for i in range(0, len(todo), 200):
            await asyncio.gather(*(one(t, k) for t, k in todo[i:i + 200]))
    finally:
        if own:
            await http.aclose()
    log.info("prices: %s", counts)
    return counts


def coverage_by_offset(store: HistoryStore, *, sport: str | None = None) -> list[tuple[int, int, int]]:
    """(offset_min, tokens with a snapshot, tokens checked) over parsed legs of resolved games."""
    sql = ("SELECT l.token_id FROM hist_legs l JOIN hist_markets m USING(condition_id)"
           " WHERE l.token_id IS NOT NULL AND m.resolved = 1 AND l.parse_error IS NULL")
    args: list = []
    if sport:
        sql += " AND m.sport = ?"; args.append(sport)
    tokens = {r[0] for r in store.rows(sql, *args)}
    checked = {r[0] for r in store.rows("SELECT token_id FROM hist_price_checks")} & tokens
    out = []
    for off, in store.rows("SELECT DISTINCT offset_min FROM hist_prices ORDER BY 1"):
        have = {r[0] for r in store.rows("SELECT token_id FROM hist_prices WHERE offset_min=?", off)} & tokens
        out.append((int(off), len(have), len(checked)))
    return out
