"""Gamma + CLOB HTTP access for the history package: gentle, with a browser User-Agent and backoff.

Verified 2026-10-20:
  GET /events?series_slug=nfl-2026&closed=true&limit=50&offset=N&order=endDate&ascending=false
      -> closed events newest first, main game events plus -player-props / -highest-scoring-quarter /
         -first-td-scorer side events, each with full `markets`.
  GET /markets?condition_ids=a&condition_ids=b...   -> up to 100 markets per call (resolve path).
  GET clob.polymarket.com/prices-history?market={token}&startTs&endTs&fidelity -> {"history": [{"t": s, "p": px}]}
Resolved market: closed true, umaResolutionStatus "resolved", outcomePrices exactly "0"/"1" ("0.5"/"0.5" = void).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Iterable

import httpx

log = logging.getLogger(__name__)

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_URL = "https://clob.polymarket.com"
UA = "Mozilla/5.0 (Macintosh) rfqbot-history/0.1"
SERIES = {"nfl": "nfl-2026", "nba": "nba-2026"}
SIDE_EVENT_SUFFIXES = ("-player-props", "-highest-scoring-quarter", "-first-td-scorer")


class Http:
    """Shared async client with retry/backoff; `pause_s` between calls keeps us polite."""

    def __init__(self, *, gamma_url: str = DEFAULT_GAMMA_URL, clob_url: str = DEFAULT_CLOB_URL, concurrency: int = 4,
                 pause_s: float = 0.05, retries: int = 4, client: httpx.AsyncClient | None = None):
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.pause_s = pause_s
        self.retries = retries
        self._own = client is None
        self.client = client or httpx.AsyncClient(timeout=30, headers={"User-Agent": UA})
        self.sem = asyncio.Semaphore(concurrency)
        self.errors = 0
        self.calls = 0

    async def aclose(self) -> None:
        if self._own:
            await self.client.aclose()

    async def get_json(self, url: str, params: Any = None) -> Any:
        delay = 1.0
        for attempt in range(self.retries + 1):
            async with self.sem:
                try:
                    self.calls += 1
                    r = await self.client.get(url, params=params)
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    if self.pause_s:
                        await asyncio.sleep(self.pause_s)
                    return r.json()
                except (httpx.HTTPError, ValueError) as e:
                    self.errors += 1
                    code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                    if attempt >= self.retries or (code is not None and 400 <= code < 500 and code != 429):
                        raise                                   # a client error will not change on retry (429 is the exception)
                    log.warning("%s %s: %s; retry in %.0fs", url.rsplit("/", 1)[-1], params if isinstance(params, dict) else "", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    # -- gamma
    async def iter_closed_events(self, series_slug: str, *, page: int = 50, max_pages: int = 400) -> AsyncIterator[dict]:
        """Closed events of a series, newest endDate first."""
        offset = 0
        for _ in range(max_pages):
            body = await self.get_json(f"{self.gamma_url}/events", {
                "series_slug": series_slug, "closed": "true", "limit": page, "offset": offset,
                "order": "endDate", "ascending": "false"})
            if not isinstance(body, list) or not body:
                return
            for ev in body:
                yield ev
            if len(body) < page:
                return
            offset += page

    async def markets_by_condition_ids(self, condition_ids: Iterable[str], *, batch: int = 100,
                                       closed: bool | None = None) -> dict[str, dict]:
        """Verified 2026-10-20: without `closed=true` Gamma returns only OPEN markets for condition_ids, so a
        resolved (closed) market is simply absent. Pass closed=True to look up resolved ones."""
        out: dict[str, dict] = {}
        ids = list(condition_ids)
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            try:
                params = [("condition_ids", c) for c in chunk] + [("limit", len(chunk))]
                if closed is not None:
                    params.append(("closed", "true" if closed else "false"))
                body = await self.get_json(f"{self.gamma_url}/markets", params)
            except Exception as e:
                log.warning("markets batch failed (%d ids): %s", len(chunk), e)
                continue
            for mk in body or []:
                cid = str(mk.get("conditionId", "")).lower()
                if cid:
                    out[cid] = mk
        return out

    # -- clob
    async def price_history(self, token_id: str, start_s: int, end_s: int, fidelity_min: int = 1) -> list[dict]:
        body = await self.get_json(f"{self.clob_url}/prices-history",
                                   {"market": token_id, "startTs": start_s, "endTs": end_s, "fidelity": fidelity_min})
        return list((body or {}).get("history") or [])


def is_side_event(slug: str) -> bool:
    return any(slug.endswith(s) for s in SIDE_EVENT_SUFFIXES)
