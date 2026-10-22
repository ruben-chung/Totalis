"""pull: closed Gamma events of a sport's series -> hist_games / hist_markets / hist_legs."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from ..books import _parse_gamma_time, game_prefix_of
from .gamma import SERIES, Http, is_side_event
from .store import HistoryStore, now_ms

log = logging.getLogger(__name__)


def _jl(v: Any) -> list:
    """Gamma gives some lists as JSON strings (clobTokenIds, outcomes, outcomePrices) and some as lists."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


def resolution(mk: dict) -> tuple[int, list[int | None]]:
    """(resolved, won per outcome): resolved when status 'resolved' and every price is exactly 0 or 1."""
    prices = _jl(mk.get("outcomePrices"))
    status = str(mk.get("umaResolutionStatus") or "")
    won: list[int | None] = []
    for p in prices:
        s = str(p).strip()
        won.append(1 if s in ("1", "1.0") else 0 if s in ("0", "0.0") else None)
    resolved = bool(prices) and status == "resolved" and all(w is not None for w in won)
    return int(resolved), (won if resolved else [None] * len(prices))


def _kickoff(mk: dict) -> datetime | None:
    ms = _parse_gamma_time(mk.get("gameStartTime"))
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def parse_leg(sport: str, slug: str, title: str, outcome: str, idx: int, *, position_id: str | None,
              kickoff_utc: datetime | None) -> tuple[dict, str | None]:
    """LegSpec fields for one outcome via the registered sport parser, or ({}, error)."""
    try:
        from ..sports import registry
        from .. import sports  # noqa: F401
        from ..sports.legspec import canonical_key, contract_key
        spec = registry.get(sport).parse_poly(slug, title, outcome, idx, position_id=position_id, kickoff_utc=kickoff_utc)
    except Exception as e:  # PolyParseError / LegSpecError / unregistered sport: keep the row, mark the error
        return {}, f"{type(e).__name__}: {e}"[:300]
    return {
        "family": spec.family, "period": spec.period, "side": spec.side, "team": spec.team, "line": spec.line,
        "bucket": spec.bucket, "includes_ot": None if spec.includes_ot is None else int(spec.includes_ot),
        "game_key": spec.game_key, "canonical_key": canonical_key(spec), "contract_key": contract_key(spec),
    }, None


def ingest_event(store: HistoryStore, sport: str, ev: dict, *, at_ms: int | None = None) -> tuple[str | None, int, int]:
    """Store one Gamma event (main or side). Returns (game_prefix, n_markets, n_resolved)."""
    at_ms = at_ms or now_ms()
    slug = str(ev.get("slug") or "")
    prefix = game_prefix_of(slug)
    if not prefix:
        return None, 0, 0
    n, n_res = 0, 0
    with store:
        for mk in ev.get("markets") or []:
            cid = str(mk.get("conditionId") or "").lower()
            if not cid:
                continue
            resolved, won = resolution(mk)
            outcomes, tokens, positions = _jl(mk.get("outcomes")), _jl(mk.get("clobTokenIds")), _jl(mk.get("positionIds"))
            store.upsert_market({
                "condition_id": cid, "game_prefix": prefix, "sport": sport, "event_id": str(ev.get("id") or "") or None,
                "event_slug": slug, "slug": mk.get("slug"), "title": mk.get("question"), "outcomes": json.dumps(outcomes),
                "token_ids": json.dumps([str(t) for t in tokens]), "position_ids": json.dumps([str(p) for p in positions]),
                "game_start_time": mk.get("gameStartTime"), "end_date": mk.get("endDate"),
                "sports_market_type": mk.get("sportsMarketType"), "line": mk.get("line"),
                "outcome_prices": json.dumps([str(p) for p in _jl(mk.get("outcomePrices"))]),
                "resolution_status": mk.get("umaResolutionStatus"), "closed": int(bool(mk.get("closed"))),
                "resolved": resolved, "pulled_ms": at_ms,
            })
            kick = _kickoff(mk)
            for i, outcome in enumerate(outcomes):
                fields, err = parse_leg(sport, mk.get("slug") or "", mk.get("question") or "", str(outcome), i,
                                        position_id=str(positions[i]) if i < len(positions) else None, kickoff_utc=kick)
                store.upsert_leg({
                    "condition_id": cid, "outcome_index": i, "game_prefix": prefix,
                    "token_id": str(tokens[i]) if i < len(tokens) else None,
                    "position_id": str(positions[i]) if i < len(positions) else None,
                    "outcome": str(outcome), "won": won[i] if i < len(won) else None, "parse_error": err, **fields,
                })
            n += 1
            n_res += resolved
        _refresh_game(store, prefix, sport, at_ms)
    return prefix, n, n_res


def _refresh_game(store: HistoryStore, prefix: str, sport: str, at_ms: int) -> None:
    r = store.conn.execute(
        "SELECT COUNT(*), SUM(resolved), MIN(game_start_time), MAX(end_date) FROM hist_markets WHERE game_prefix=?",
        (prefix,)).fetchone()
    evs = store.rows("SELECT DISTINCT event_id, event_slug FROM hist_markets WHERE game_prefix=? ORDER BY event_slug", prefix)
    store.upsert_game({
        "game_prefix": prefix, "sport": sport, "event_ids": json.dumps([e[0] for e in evs]),
        "event_slugs": json.dumps([e[1] for e in evs]), "game_start_time": r[2], "end_date": r[3],
        "n_markets": r[0] or 0, "n_resolved": r[1] or 0, "pulled_ms": at_ms,
    })


async def pull(store: HistoryStore, sport: str, *, since: str | None = None, http: Http | None = None,
               series_slug: str | None = None, include_side_events: bool = True, max_events: int | None = None) -> dict[str, int]:
    """Pull every closed event of the sport's series with endDate >= since (ISO date)."""
    series = series_slug or SERIES.get(sport)
    if not series:
        raise ValueError(f"no Gamma series known for sport {sport!r}; pass series_slug")
    since_ms = _parse_gamma_time(f"{since}T00:00:00Z") if since else None
    own = http is None
    http = http or Http()
    counts = {"events": 0, "side_events": 0, "markets": 0, "resolved": 0, "games": 0, "skipped_old": 0}
    games: set[str] = set()
    try:
        async for ev in http.iter_closed_events(series):
            end_ms = _parse_gamma_time(ev.get("endDate"))
            if since_ms and end_ms and end_ms < since_ms:
                counts["skipped_old"] += 1
                break   # newest-first: everything after this is older
            side = is_side_event(str(ev.get("slug") or ""))
            if side and not include_side_events:
                continue
            prefix, n, n_res = ingest_event(store, sport, ev)
            if prefix is None:
                continue
            counts["events"] += 1
            counts["side_events"] += side
            counts["markets"] += n
            counts["resolved"] += n_res
            games.add(prefix)
            if max_events and counts["events"] >= max_events:
                break
    finally:
        if own:
            await http.aclose()
    counts["games"] = len(games)
    log.info("pull %s: %s", sport, counts)
    return counts


def reparse(store: HistoryStore, sport: str | None = None, *, only_failed: bool = False) -> dict[str, int]:
    """Re-run the sport parser over stored hist_markets rows (after a parser change), no Gamma calls.
    Rewrites hist_legs rows in place; `won` is recomputed from the stored final prices."""
    sql = "SELECT * FROM hist_markets" + (" WHERE sport = ?" if sport else "")
    counts = {"markets": 0, "legs": 0, "parsed": 0, "failed": 0, "newly_parsed": 0}
    with store:
        for mk in store.rows(sql, *([sport] if sport else [])):
            resolved, won = resolution({"umaResolutionStatus": mk["resolution_status"], "outcomePrices": mk["outcome_prices"]})
            outcomes, tokens, positions = _jl(mk["outcomes"]), _jl(mk["token_ids"]), _jl(mk["position_ids"])
            kick = _kickoff({"gameStartTime": mk["game_start_time"]})
            before = {r["outcome_index"]: r["parse_error"] for r in store.rows(
                "SELECT outcome_index, parse_error FROM hist_legs WHERE condition_id=?", mk["condition_id"])}
            if only_failed and not any(before.values()):
                continue
            counts["markets"] += 1
            for i, outcome in enumerate(outcomes):
                fields, err = parse_leg(mk["sport"], mk["slug"] or "", mk["title"] or "", str(outcome), i,
                                        position_id=str(positions[i]) if i < len(positions) else None, kickoff_utc=kick)
                store.upsert_leg({
                    "condition_id": mk["condition_id"], "outcome_index": i, "game_prefix": mk["game_prefix"],
                    "token_id": str(tokens[i]) if i < len(tokens) else None,
                    "position_id": str(positions[i]) if i < len(positions) else None,
                    "outcome": str(outcome), "won": won[i] if i < len(won) else None, "parse_error": err, **fields,
                })
                counts["legs"] += 1
                counts["parsed" if err is None else "failed"] += 1
                if err is None and before.get(i):
                    counts["newly_parsed"] += 1
    log.info("reparse %s: %s", sport or "all", counts)
    return counts
