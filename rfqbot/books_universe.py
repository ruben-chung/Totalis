"""Subscription universes and token resolution for the book recorder.

Three sources, each holding one read-only connection to the listener's rfq.db (closed by
``close()``, which ``BookRecorder.run`` calls on shutdown):

* ``RfqLegUniverse``: leg position_ids of RFQs seen in the last window, read incrementally.
* ``GameUniverse``: widens the legs to whole games (every catalog position under a game's slug
  prefix) for the configured sports, filling ``leg_tokens`` per game via Gamma ``/events``.
* ``TokenMap``: position_id -> CLOB token_id via the catalog (condition_id + outcome) and Gamma
  ``/markets``, cached in books.db. An RFQ position_id is NOT a CLOB token id (verified
  2026-10-20: the market channel answers [] for them).

``rfqbot.books`` re-exports these names, so ``from rfqbot.books import GameUniverse`` keeps working.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import httpx

from .db import SQLITE_IN_CHUNK, chunks, ro_connect
from .slugs import game_prefix_of
from .timeutil import now_ms, parse_iso_ms

if TYPE_CHECKING:
    from .books import BookStore

log = logging.getLogger(__name__)

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
GAMMA_USER_AGENT = "Mozilla/5.0 rfqbot/0.1"   # Gamma returns 403 to a bare urllib / httpx UA


def _sport_of(slug: str) -> str | None:
    try:
        from .sports.registry import sport_for_slug
        from . import sports  # noqa: F401  (registers built-in sports)
        spec = sport_for_slug(slug)
        if spec is not None:
            return spec.sport
    except Exception:  # registry unavailable: fall back to the league token of the slug
        pass
    return slug.split("-", 1)[0] if slug else None


# ------------------------------------------------------------------ universe
class RfqLegUniverse:
    """Leg position_ids seen in RFQs within `window_ms`, read incrementally from the listener's db."""

    def __init__(self, rfq_db: str | Path, *, window_ms: int):
        self.window_ms = window_ms
        self.conn = ro_connect(rfq_db, rows=False)
        self.last_seen: dict[str, int] = {}
        self.rfq_counts: dict[str, int] = {}   # RFQ rows per position since it entered the window (ranking only)
        self._cursor_ms = 0

    def __call__(self) -> set[str]:
        now = now_ms()
        since = max(self._cursor_ms - 2_000, now - self.window_ms)  # 2 s overlap: listener commits in batches
        rows = self.conn.execute(
            "SELECT r.first_seen_ms, l.position_id FROM rfqs r JOIN combo_legs l ON l.combo_id = r.combo_id"
            " WHERE r.first_seen_ms > ?", (since,)).fetchall()
        for ts, pid in rows:
            if ts > self.last_seen.get(pid, 0):
                self.last_seen[pid] = ts
            if ts > self._cursor_ms:
                self._cursor_ms = ts
            if ts > self._cursor_ms - 2_000:   # rows in the overlap are re-read; count only the fresh tail
                self.rfq_counts[pid] = self.rfq_counts.get(pid, 0) + 1
        cutoff = now - self.window_ms
        for pid in [p for p, t in self.last_seen.items() if t < cutoff]:
            del self.last_seen[pid]
            self.rfq_counts.pop(pid, None)
        return set(self.last_seen)

    def close(self) -> None:
        self.conn.close()


# ------------------------------------------------------------- game universe
class GameUniverse:
    """Whole-game ladders: every catalog position of a game that any recent RFQ leg belongs to.

    Legs (from RfqLegUniverse) -> game prefixes via markets.slug -> all positions under the prefix,
    for sports in `sports` only; other sports stay leg-level. Gamma /events?slug={game} fills
    leg_tokens for the whole game in one call (and gives gameStartTime for the lookahead filter).
    Games are ranked by RFQ count when `max_tokens` binds. Returns legs | game positions.
    """

    def __init__(self, store: "BookStore", rfq_db: str | Path, legs: RfqLegUniverse, *, sports: Iterable[str] = ("nfl", "nba"),
                 max_tokens: int = 20_000, lookahead_hours: float = 72.0, gamma_url: str = DEFAULT_GAMMA_URL,
                 game_refresh_ms: int = 6 * 3_600_000, concurrency: int = 4, client: httpx.AsyncClient | None = None):
        self.store = store
        self.legs = legs
        self.sports = {x.strip().lower() for x in sports if x.strip()}
        self.max_tokens = max_tokens
        self.lookahead_ms = int(lookahead_hours * 3_600_000)
        self.gamma_url = gamma_url.rstrip("/")
        self.game_refresh_ms = game_refresh_ms
        self.concurrency = concurrency
        self._client = client
        self.cat = ro_connect(rfq_db, rows=False)
        self.report: dict[str, dict[str, int]] = {}   # sport -> {games, tokens, skipped_lookahead, skipped_cap}

    async def __call__(self) -> set[str]:
        leg_pos = self.legs()
        if not self.sports:
            return leg_pos
        now = now_ms()
        # 1. leg -> slug -> game prefix (+ RFQ count per game for ranking)
        games: dict[str, str] = {}            # prefix -> sport
        rfq_count: dict[str, int] = {}
        for chunk in chunks(sorted(leg_pos), SQLITE_IN_CHUNK):
            q = f"SELECT position_id, slug FROM markets WHERE position_id IN ({','.join('?' * len(chunk))})"
            for pid, slug in self.cat.execute(q, chunk):
                sport = _sport_of(slug or "")
                prefix = game_prefix_of(slug)
                if sport in self.sports and prefix:
                    games[prefix] = sport
                    rfq_count[prefix] = rfq_count.get(prefix, 0) + self.legs.rfq_counts.get(pid, 1)
        # 2. Gamma event per game (new or stale) -> leg_tokens + games.game_start_time
        await self._fill_games(games, now)
        # 3. catalog positions per game (one LIKE scan per sport, filtered in Python)
        positions: dict[str, list[str]] = {g: [] for g in games}
        for sport in {s for s in games.values()}:
            like = f"{sport}-%"
            for pid, slug in self.cat.execute("SELECT position_id, slug FROM markets WHERE slug LIKE ?", (like,)):
                prefix = game_prefix_of(slug)
                if prefix in positions:
                    positions[prefix].append(pid)
        # 4. lookahead filter, then rank by RFQ count and apply the token cap
        out = set(leg_pos)
        budget = self.max_tokens
        report: dict[str, dict[str, int]] = {}
        for g, sport in sorted(games.items(), key=lambda kv: -rfq_count.get(kv[0], 0)):
            r = report.setdefault(sport, {"games": 0, "tokens": 0, "skipped_lookahead": 0, "skipped_cap": 0, "no_event": 0})
            row = self.store.game(g)
            start_ms = parse_iso_ms(row["game_start_time"]) if row else None
            skip = None
            if start_ms is not None and start_ms > now + self.lookahead_ms:
                skip = "lookahead"; r["skipped_lookahead"] += 1
            elif row is not None and row["event_id"] is None:
                r["no_event"] += 1          # tokens may still resolve via the catalog batch path
            n = len(positions[g])
            if skip is None and n > budget:
                skip = "cap"; r["skipped_cap"] += 1
            if skip is None:
                out.update(positions[g]); budget -= n
                r["games"] += 1; r["tokens"] += n
            self.store.upsert_game({"game_prefix": g, "sport": sport, "event_id": None, "game_start_time": None,
                                    "n_positions": n, "rfq_count": rfq_count.get(g, 0), "included": int(skip is None),
                                    "skip_reason": skip, "fetched_ms": None, "updated_ms": now})
        self.report = report
        return out

    async def _fill_games(self, games: dict[str, str], now: int) -> None:
        todo = [g for g in games if (row := self.store.game(g)) is None or row["fetched_ms"] is None
                or now - row["fetched_ms"] >= self.game_refresh_ms]
        if not todo:
            return
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30, headers={"User-Agent": GAMMA_USER_AGENT})
        sem = asyncio.Semaphore(self.concurrency)

        async def one(g: str) -> None:
            async with sem:
                try:
                    resp = await client.get(f"{self.gamma_url}/events", params={"slug": g})
                    resp.raise_for_status()
                    body = resp.json()
                except Exception as e:
                    log.warning("gamma event %s failed: %s", g, e)
                    return
                ev = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else None)
                event_id = str(ev["id"]) if ev and ev.get("id") else None
                start = None
                rows = []
                for mk in (ev or {}).get("markets") or []:
                    try:
                        toks = json.loads(mk.get("clobTokenIds") or "[]")
                        outs = json.loads(mk.get("outcomes") or "[]")
                    except ValueError:
                        continue
                    start = start or mk.get("gameStartTime")
                    for i, pid in enumerate(mk.get("positionIds") or []):
                        rows.append((str(pid), mk.get("conditionId"), i, outs[i] if i < len(outs) else None,
                                     str(toks[i]) if i < len(toks) else None, mk.get("slug"), event_id,
                                     mk.get("gameStartTime"), mk.get("endDate"), now))
                if rows:
                    self.store.upsert_tokens(rows)
                self.store.upsert_game({"game_prefix": g, "sport": games[g], "event_id": event_id, "game_start_time": start,
                                        "n_positions": None, "rfq_count": None, "included": 0, "skip_reason": None,
                                        "fetched_ms": now, "updated_ms": now})
                log.info("gamma event %s: %d markets, %d tokens, start %s", g, len((ev or {}).get("markets") or []),
                         sum(1 for r in rows if r[4]), start)

        try:
            await asyncio.gather(*(one(g) for g in todo))
        finally:
            if own:
                await client.aclose()

    def close(self) -> None:
        """Close the catalog connection and the wrapped leg universe (this object is its only holder)."""
        self.cat.close()
        close_legs = getattr(self.legs, "close", None)
        if callable(close_legs):
            close_legs()


# ---------------------------------------------------------------- token map
class TokenMap:
    """position_id -> CLOB token_id. Catalog (rfq.db markets) gives condition_id + outcome index;
    Gamma /markets?condition_ids=... gives clobTokenIds. Results are cached in books.db."""

    def __init__(self, store: "BookStore", rfq_db: str | Path, *, gamma_url: str = DEFAULT_GAMMA_URL,
                 retry_unresolved_ms: int = 1_800_000, batch: int = 100):
        self.store = store
        self.gamma_url = gamma_url.rstrip("/")
        self.retry_unresolved_ms = retry_unresolved_ms
        self.batch = batch
        self.cat = ro_connect(rfq_db, rows=False)
        self.unresolved = 0

    async def __call__(self, position_ids: set[str]) -> dict[str, str]:
        cached = self.store.tokens_cached(position_ids)
        now = now_ms()
        out = {p: t for p, (t, _) in cached.items() if t}
        todo = [p for p in position_ids if p not in out
                and (p not in cached or now - cached[p][1] >= self.retry_unresolved_ms)]
        if todo:
            out.update(await self._resolve(todo, now))
        self.unresolved = len(position_ids) - len(out)
        return out

    async def _resolve(self, position_ids: list[str], now: int) -> dict[str, str]:
        rows = {}
        for pid in position_ids:
            r = self.cat.execute("SELECT condition_id, outcome_index, outcome, slug FROM markets WHERE position_id=?",
                                 (pid,)).fetchone()
            rows[pid] = tuple(r) if r else None
        by_cond: dict[str, list[str]] = {}
        for pid, r in rows.items():
            if r and r[0]:
                by_cond.setdefault(r[0], []).append(pid)
        gamma: dict[str, dict] = {}
        conds = list(by_cond)
        async with httpx.AsyncClient(timeout=20) as client:
            for i in range(0, len(conds), self.batch):
                chunk = conds[i:i + self.batch]
                try:
                    resp = await client.get(f"{self.gamma_url}/markets",
                                            params=[("condition_ids", c) for c in chunk] + [("limit", len(chunk))])
                    resp.raise_for_status()
                    for mk in resp.json():
                        gamma[str(mk.get("conditionId", "")).lower()] = mk
                except Exception as e:  # leave these unresolved; retried after retry_unresolved_ms
                    log.warning("gamma lookup failed for %d markets: %s", len(chunk), e)
        out, upserts = {}, []
        for pid, r in rows.items():
            cond, idx, outcome, slug = r if r else (None, None, None, None)
            mk = gamma.get((cond or "").lower())
            token = None
            if mk:
                try:
                    toks = json.loads(mk.get("clobTokenIds") or "[]")
                    outs = json.loads(mk.get("outcomes") or "[]")
                    j = outs.index(outcome) if outcome in outs else idx
                    token = str(toks[j]) if j is not None and j < len(toks) else None
                except (ValueError, TypeError, IndexError):
                    token = None
            ev = (mk.get("events") or [{}])[0] if mk else {}
            upserts.append((pid, cond, idx, outcome, token, slug, str(ev.get("id")) if ev.get("id") else None,
                            (mk or {}).get("gameStartTime"), (mk or {}).get("endDate"), now))
            if token:
                out[pid] = token
        self.store.upsert_tokens(upserts)
        return out

    def close(self) -> None:
        self.cat.close()
