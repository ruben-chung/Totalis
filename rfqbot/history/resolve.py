"""resolve: resolved outcomes for every market in rfq.db rfq_legs and hist_markets -> rfq.db market_resolutions.

Writes rows only (condition_id, resolved_outcome, resolved_at_ms); the table's schema belongs to the pricing
session. resolved_outcome is the winning outcome's name ("Steelers", "Over", "Yes"); "VOID" when Gamma
settled 0.5/0.5. Timer-safe: a market is re-checked only when it is still unresolved, its endDate has
passed, and the last check is older than `recheck_ms`.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..books import _parse_gamma_time
from .gamma import Http
from .pull import _jl, resolution
from .store import HistoryStore, now_ms

log = logging.getLogger(__name__)


def candidate_markets(store: HistoryStore, rfq_db: str | Path, *, now: int | None = None,
                      recheck_ms: int = 3_600_000) -> tuple[list[str], dict[str, int]]:
    """condition_ids worth asking Gamma about, and why the rest were skipped."""
    now = now or now_ms()
    rfq = sqlite3.connect(f"file:{Path(rfq_db)}?mode=ro", uri=True)
    rfq.execute("PRAGMA busy_timeout=30000")
    try:
        legs = {r[0].lower() for r in rfq.execute(
            "SELECT DISTINCT market_condition_id FROM rfq_legs WHERE market_condition_id IS NOT NULL")}
        done = {r[0].lower() for r in rfq.execute("SELECT condition_id FROM market_resolutions WHERE resolved_outcome IS NOT NULL")}
    finally:
        rfq.close()
    hist = {r[0].lower() for r in store.rows("SELECT condition_id FROM hist_markets")}
    local = {r[0].lower() for r in store.rows("SELECT condition_id FROM hist_markets WHERE resolved = 1")}
    checks = {r["condition_id"]: r for r in store.rows("SELECT * FROM hist_resolve_checks")}
    todo, skipped = [], {"already_resolved": 0, "end_date_not_passed": 0, "checked_recently": 0, "local": 0}
    for cid in sorted(legs | hist):
        if cid in done:
            skipped["already_resolved"] += 1
            continue
        if cid in local:
            skipped["local"] += 1      # the pull already holds the final prices: written without a Gamma call
            continue
        chk = checks.get(cid)
        if chk is not None:
            end_ms = _parse_gamma_time(chk["end_date"])
            if end_ms and end_ms > now:
                skipped["end_date_not_passed"] += 1
                continue
            if now - chk["last_checked_ms"] < recheck_ms:
                skipped["checked_recently"] += 1
                continue
        todo.append(cid)
    return todo, skipped


def outcome_name(mk: dict) -> str | None:
    """Winning outcome name for a resolved market, 'VOID' for 0.5/0.5, None when unresolved."""
    resolved, won = resolution(mk)
    outcomes = [str(o) for o in _jl(mk.get("outcomes"))]
    if resolved:
        for i, w in enumerate(won):
            if w == 1:
                return outcomes[i] if i < len(outcomes) else str(i)
        return None
    prices = [str(p).strip() for p in _jl(mk.get("outcomePrices"))]
    if str(mk.get("umaResolutionStatus") or "") == "resolved" and prices and all(p in ("0.5",) for p in prices):
        return "VOID"
    return None


def write_resolutions(rfq_db: str | Path, rows: list[tuple[str, str, int]]) -> int:
    if not rows:
        return 0
    c = sqlite3.connect(str(rfq_db), timeout=30)
    try:
        c.executemany("INSERT OR REPLACE INTO market_resolutions(condition_id, resolved_outcome, resolved_at_ms) VALUES (?,?,?)", rows)
        c.commit()
    finally:
        c.close()
    return len(rows)


def local_resolutions(store: HistoryStore, rfq_db: str | Path, now: int) -> list[tuple[str, str, int]]:
    """Rows for hist_markets already resolved by the pull that rfq.db does not have yet."""
    rfq = sqlite3.connect(f"file:{Path(rfq_db)}?mode=ro", uri=True)
    try:
        done = {r[0].lower() for r in rfq.execute("SELECT condition_id FROM market_resolutions WHERE resolved_outcome IS NOT NULL")}
    finally:
        rfq.close()
    rows = []
    for cid, outcomes, prices in store.rows("SELECT condition_id, outcomes, outcome_prices FROM hist_markets WHERE resolved = 1"):
        if cid.lower() in done:
            continue
        name = outcome_name({"umaResolutionStatus": "resolved", "outcomePrices": prices, "outcomes": outcomes})
        if name:
            rows.append((cid.lower(), name, now))
    return rows


async def resolve(store: HistoryStore, rfq_db: str | Path, *, http: Http | None = None, limit: int | None = None,
                  recheck_ms: int = 3_600_000) -> dict[str, int]:
    now = now_ms()
    local_rows = local_resolutions(store, rfq_db, now)
    written_local = write_resolutions(rfq_db, local_rows)
    with store:
        for cid, name, _ in local_rows:
            store.record_resolve_check(cid, None, name, now)
    todo, skipped = candidate_markets(store, rfq_db, now=now, recheck_ms=recheck_ms)
    if limit:
        todo = todo[:limit]
    own = http is None
    http = http or Http()
    try:
        # Gamma hides closed markets unless closed=true: look resolved ones up first, then the rest as open markets
        markets = await http.markets_by_condition_ids(todo, closed=True)
        rest = [c for c in todo if c not in markets]
        if rest:
            markets.update(await http.markets_by_condition_ids(rest))
    finally:
        if own:
            await http.aclose()
    rows, unresolved, missing = [], 0, 0
    with store:
        for cid in todo:
            mk = markets.get(cid)
            if mk is None:
                missing += 1
                store.record_resolve_check(cid, None, None, now)
                continue
            name = outcome_name(mk)
            store.record_resolve_check(cid, mk.get("endDate"), name, now)
            if name is None:
                unresolved += 1
            else:
                rows.append((cid, name, now))
            # keep hist_markets in sync when the market is one of ours
            if store.scalar("SELECT 1 FROM hist_markets WHERE condition_id=?", cid):
                res, _ = resolution(mk)
                store.conn.execute("UPDATE hist_markets SET outcome_prices=?, resolution_status=?, closed=?, resolved=? WHERE condition_id=?",
                                   (str(mk.get("outcomePrices")), mk.get("umaResolutionStatus"), int(bool(mk.get("closed"))), res, cid))
    written = write_resolutions(rfq_db, rows)
    counts = {"candidates": len(todo), "written": written + written_local, "written_local": written_local,
              "unresolved": unresolved, "not_in_gamma": missing, **skipped}
    log.info("resolve: %s", counts)
    return counts
