"""Thin Kalshi public-API client and the kalshi_markets sync.

Base: https://api.elections.kalshi.com/trade-api/v2 (no auth for reads).
  GET /markets?series_ticker=S&status=open&limit=N[&cursor=C]  (cursor pagination)
  GET /series?category=Sports[&tags=Football]
  GET /events/{event_ticker}

Rate limiting: one request at a time with a minimum gap, exponential backoff on 429 / 5xx.
``kalshi-sync --sport X`` walks the game-level series registered for the sport (``sports.registry``) and stores
every market with its parsed LegSpec fields (or the parse error) in ``kalshi_markets``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import httpx

from .sports import registry
from .sports.legspec import LegSpec, canonical_key
from .store import Store, now_ms

log = logging.getLogger(__name__)

KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, base_url: str = KALSHI_API_URL, *, client: httpx.AsyncClient | None = None,
                 min_gap_sec: float = 0.2, max_retries: int = 6, page_size: int = 200):
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._own = client is None
        self.min_gap_sec = min_gap_sec
        self.max_retries = max_retries
        self.page_size = page_size
        self._last_request = 0.0
        self.requests = 0

    async def __aenter__(self) -> "KalshiClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._client is not None, "use 'async with KalshiClient()'"
        for attempt in range(self.max_retries + 1):
            gap = self.min_gap_sec - (time.monotonic() - self._last_request)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_request = time.monotonic()
            self.requests += 1
            try:
                r = await self._client.get(f"{self.base_url}{path}", params=params)
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("kalshi %s: %s (retry %d)", path, e, attempt + 1)
                await asyncio.sleep(min(30, 1.5 * 2 ** attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    r.raise_for_status()
                wait = float(r.headers.get("Retry-After") or min(30, 1.5 * 2 ** attempt))
                log.warning("kalshi %s: HTTP %d, waiting %.1fs", path, r.status_code, wait)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def markets(self, series_ticker: str, *, status: str | None = "open",
                      max_pages: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"series_ticker": series_ticker, "limit": self.page_size}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            body = await self.get("/markets", params)
            out.extend(body.get("markets") or [])
            cursor = body.get("cursor") or None
            if not cursor or not body.get("markets"):
                break
        return out

    async def series(self, *, category: str = "Sports", tags: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if tags:
            params["tags"] = tags
        return (await self.get("/series", params)).get("series") or []

    async def event(self, event_ticker: str) -> dict[str, Any]:
        return await self.get(f"/events/{event_ticker}")


def market_row(m: dict[str, Any], sport: str, synced_at_ms: int,
               parser: Callable[..., LegSpec] | None = None) -> dict[str, Any]:
    """Flatten one API market into a kalshi_markets row, parsing it and recording the outcome."""
    parser = parser or registry.get(sport).parse_kalshi
    row: dict[str, Any] = {
        "ticker": m["ticker"], "event_ticker": m.get("event_ticker"), "series_ticker": m["ticker"].split("-")[0],
        "sport": sport, "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"),
        "floor_strike": m.get("floor_strike"), "cap_strike": m.get("cap_strike"), "strike_type": m.get("strike_type"),
        "rules_primary": m.get("rules_primary"), "rules_secondary": m.get("rules_secondary"),
        "expected_expiration_time": m.get("expected_expiration_time"), "close_time": m.get("close_time"),
        "status": m.get("status"), "synced_at_ms": synced_at_ms, "parse_ok": 0, "parse_error": None,
        "canonical_key": None, "game_key": None, "away": None, "home": None, "game_date": None, "family": None,
        "period": None, "team": None, "line": None, "side": None, "includes_ot": None, "rules_json": None,
    }
    try:
        spec = parser(m["ticker"], m.get("title") or "", m.get("floor_strike"), m.get("strike_type"),
                      m.get("rules_primary") or "", m.get("expected_expiration_time"),
                      yes_sub_title=m.get("yes_sub_title"), rules_secondary=m.get("rules_secondary"))
    except ValueError as e:
        row["parse_error"] = str(e)
        return row
    row.update({
        "parse_ok": 1, "canonical_key": canonical_key(spec), "game_key": spec.game_key, "away": spec.away,
        "home": spec.home, "game_date": spec.game_date, "family": spec.family, "period": spec.period,
        "team": spec.team, "line": spec.line, "side": spec.side,
        "includes_ot": None if spec.includes_ot is None else int(spec.includes_ot),
        "rules_json": json.dumps(spec.rules, sort_keys=True),
    })
    return row


def spec_from_row(row: Any) -> LegSpec:
    """Re-parse a stored kalshi_markets row (parse_ok must be 1)."""
    parser = registry.get(row["sport"]).parse_kalshi
    return parser(row["ticker"], row["title"] or "", row["floor_strike"], row["strike_type"],
                  row["rules_primary"] or "", row["expected_expiration_time"],
                  yes_sub_title=row["yes_sub_title"], rules_secondary=row["rules_secondary"])


class KalshiSync:
    def __init__(self, store: Store, client: KalshiClient | None = None, *, status: str | None = "open"):
        self.store = store
        self.client = client or KalshiClient()
        self.status = status

    async def sync(self, sport: str = "nfl") -> dict[str, tuple[int, int]]:
        """Fetch every game-level series for the sport. Returns {series: (rows, parse_errors)}."""
        sp = registry.get(sport)
        synced_at = now_ms()
        result: dict[str, tuple[int, int]] = {}
        async with self.client as c:
            for series in sorted(sp.kalshi_series):
                markets = await c.markets(series, status=self.status)
                rows = [market_row(m, sport, synced_at, sp.parse_kalshi) for m in markets]
                with self.store.tx():
                    self.store.upsert_kalshi_markets(rows)
                errs = sum(1 for r in rows if not r["parse_ok"])
                result[series] = (len(rows), errs)
                log.info("kalshi %-18s %4d markets, %d parse errors", series, len(rows), errs)
        log.info("kalshi sync: %d requests in %.0fs", c.requests, (now_ms() - synced_at) / 1000)
        return result
