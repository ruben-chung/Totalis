"""Combo market catalog: maps leg position IDs to markets.

Source: GET {rfq_api_url}/v1/rfq/combo-markets (public, cursor-paginated).
Each market yields one `markets` row per outcome position (index 0 = Yes, 1 = No).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .store import Store, now_ms

log = logging.getLogger(__name__)


def market_rows(market: dict[str, Any], synced_at_ms: int) -> list[dict[str, Any]]:
    position_ids = [str(p) for p in (market.get("position_ids") or [])]
    outcomes = market.get("outcomes") or []
    prices = market.get("outcome_prices") or []
    tags = json.dumps(market.get("tags") or [])
    rows = []
    for i, pid in enumerate(position_ids):
        price: float | None
        try:
            price = float(prices[i]) if i < len(prices) and prices[i] not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        rows.append({
            "position_id": pid,
            "market_id": str(market.get("id", "")) or None,
            "condition_id": market.get("condition_id"),
            "slug": market.get("slug"),
            "title": market.get("title"),
            "outcome": outcomes[i] if i < len(outcomes) else None,
            "outcome_index": i,
            "outcome_price": price,
            "volume": float(market["volume"]) if market.get("volume") not in (None, "") else None,
            "tags": tags,
            "synced_at_ms": synced_at_ms,
        })
    return rows


class CatalogSync:
    def __init__(self, store: Store, api_url: str, *, refresh_sec: int = 21_600, page_size: int = 100,
                 min_refresh_gap_sec: int = 1_800, client: httpx.AsyncClient | None = None):
        self.store = store
        self.api_url = api_url.rstrip("/")
        self.refresh_sec = refresh_sec
        self.min_refresh_gap_sec = min_refresh_gap_sec
        self.page_size = page_size
        self._client = client
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self.last_sync_ms: int | None = None

    async def sync(self) -> int:
        """Fetch every page and upsert. Returns number of position rows written."""
        async with self._lock:
            own = self._client is None
            client = self._client or httpx.AsyncClient(timeout=20)
            try:
                total = 0
                cursor: str | None = None
                synced_at = now_ms()
                for _page in range(10_000):  # hard stop against a cursor loop
                    params: dict[str, Any] = {"limit": self.page_size}
                    if cursor:
                        params["cursor"] = cursor
                    r = await client.get(f"{self.api_url}/v1/rfq/combo-markets", params=params)
                    r.raise_for_status()
                    body = r.json()
                    rows: list[dict[str, Any]] = []
                    for mkt in body.get("markets") or []:
                        rows.extend(market_rows(mkt, synced_at))
                    with self.store.tx():
                        total += self.store.upsert_markets(rows)
                    nxt = body.get("next_cursor")
                    if not nxt or nxt == cursor or not body.get("markets"):
                        break
                    cursor = nxt
                self.last_sync_ms = synced_at
                log.info("catalog sync: %d position rows in %.0fs", total, (now_ms() - synced_at) / 1000)
                return total
            finally:
                if own:
                    await client.aclose()

    def request_refresh(self) -> None:
        """Ask the background loop to refresh soon (unknown leg seen). A full sync is ~200k
        markets / several minutes, so this is rate-limited to once per min_refresh_gap_sec."""
        if self.last_sync_ms is None or now_ms() - self.last_sync_ms >= self.min_refresh_gap_sec * 1000:
            self._wake.set()

    async def run(self) -> None:
        """Background loop: sync now, then every refresh_sec or on request_refresh()."""
        while True:
            try:
                await self.sync()
            except Exception as e:  # network errors must not kill the listener
                log.warning("catalog sync failed: %s", e)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.refresh_sec)
            except asyncio.TimeoutError:
                pass
