"""Ladder read API: every market of one game with its latest (or point-in-time) top of book.

Read-only over two sqlite files: the listener's rfq.db (`markets` catalog: slug, title, outcome)
and the book recorder's books.db (`leg_tokens`: position -> CLOB token + game start;
`book_top`: change-only top-of-book rows, prices stored as integers x 10 000).

    game_markets("nfl-pit-no-2026-10-25")                -> latest row per token
    game_markets("nfl-pit-no-2026-10-25", at_ms=t)       -> latest row at or before t (never a future row)

Markets whose position has a token but no book_top row yet are returned with None prices and
at_ms=0 so the caller sees the gap. Markets with no token mapping yet are left out (they cannot
be subscribed, so there is nothing to read); `game_coverage` reports how many.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db import SQLITE_IN_CHUNK, chunks, ro_connect

PRICE_SCALE = 10_000

DEFAULT_BOOKS_DB = Path("./data/books.db")
DEFAULT_RFQ_DB = Path("./data/rfq.db")


@dataclass(frozen=True)
class MarketQuote:
    position_id: str
    token_id: str
    slug: str
    title: str
    outcome: str
    outcome_index: int
    best_bid: float | None          # probabilities 0..1
    best_ask: float | None
    bid_size: float | None          # shares at best
    ask_size: float | None
    bid_depth: float | None         # shares within 0.05 of best
    ask_depth: float | None
    at_ms: int                      # time of the book_top row used; 0 when there is no row yet
    game_start_time: str | None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


# Deprecated alias kept for one release: rfqbot.marginals.sources imports `_ro` from here. Use rfqbot.db.ro_connect.
_ro = ro_connect


def _px(v: int | None) -> float | None:
    return None if v is None else v / PRICE_SCALE


def _catalog_rows(cat: sqlite3.Connection, game_prefix: str) -> list[sqlite3.Row]:
    return cat.execute(
        "SELECT position_id, slug, title, outcome, outcome_index FROM markets WHERE slug = ? OR slug LIKE ?"
        " ORDER BY slug, outcome_index", (game_prefix, f"{game_prefix}-%")).fetchall()


def game_markets(game_prefix: str, at_ms: int | None = None, *,
                 books_db: str | Path = DEFAULT_BOOKS_DB, rfq_db: str | Path = DEFAULT_RFQ_DB) -> list[MarketQuote]:
    """All catalog markets under `game_prefix` with the latest book_top row per token (at or before `at_ms`)."""
    cat, books = ro_connect(rfq_db), ro_connect(books_db)
    try:
        rows = _catalog_rows(cat, game_prefix)
        if not rows:
            return []
        pids = [r["position_id"] for r in rows]
        tokens: dict[str, sqlite3.Row] = {}
        for chunk in chunks(pids, SQLITE_IN_CHUNK):
            for t in books.execute(f"SELECT position_id, token_id, game_start_time FROM leg_tokens"
                                   f" WHERE token_id IS NOT NULL AND position_id IN ({','.join('?' * len(chunk))})", chunk):
                tokens[t["position_id"]] = t
        if at_ms is None:
            top_sql = "SELECT * FROM book_top WHERE asset_id = ? ORDER BY at_ms DESC LIMIT 1"
        else:
            top_sql = "SELECT * FROM book_top WHERE asset_id = ? AND at_ms <= ? ORDER BY at_ms DESC LIMIT 1"
        out: list[MarketQuote] = []
        for r in rows:
            t = tokens.get(r["position_id"])
            if t is None:
                continue
            top = books.execute(top_sql, (t["token_id"],) if at_ms is None else (t["token_id"], at_ms)).fetchone()
            out.append(MarketQuote(
                position_id=r["position_id"], token_id=t["token_id"], slug=r["slug"] or "", title=r["title"] or "",
                outcome=r["outcome"] or "", outcome_index=int(r["outcome_index"] or 0),
                best_bid=_px(top["best_bid"]) if top else None, best_ask=_px(top["best_ask"]) if top else None,
                bid_size=top["bid_size"] if top else None, ask_size=top["ask_size"] if top else None,
                bid_depth=top["bid_depth"] if top else None, ask_depth=top["ask_depth"] if top else None,
                at_ms=int(top["at_ms"]) if top else 0, game_start_time=t["game_start_time"],
            ))
        return out
    finally:
        cat.close(); books.close()


def game_coverage(game_prefix: str, *, books_db: str | Path = DEFAULT_BOOKS_DB,
                  rfq_db: str | Path = DEFAULT_RFQ_DB) -> dict[str, int]:
    """Counts for one game: catalog positions, positions with a token, tokens with any book row."""
    quotes = game_markets(game_prefix, books_db=books_db, rfq_db=rfq_db)
    cat = ro_connect(rfq_db)
    try:
        n_cat = len(_catalog_rows(cat, game_prefix))
    finally:
        cat.close()
    return {"catalog_positions": n_cat, "with_token": len(quotes), "with_book": sum(1 for q in quotes if q.at_ms)}
