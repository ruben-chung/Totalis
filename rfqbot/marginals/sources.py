"""Adapters from the two quote stores to the model.

Polymarket: `rfqbot.ladders.game_markets` over books.db. Kalshi: `rfqbot.kalshi_books.kalshi_for_legs`
over kalshi_books.db (`kalshi_source("auto")`; `"none"` leaves every leg unmatched;
`"synthetic"` fabricates exact matches with a quote jittered around the Polymarket mid). The model
only duck-types MatchRow and KalshiQuote (attribute names from the briefs).

Both stores are change-only (a row per change of the top, not per poll), so a quote's row time is
when it last *changed*. `poly_confirmations` / `kalshi_confirmation` give the time the feed was
last seen alive at or before the evaluation instant, which is when an unchanged quote was last
confirmed; the model measures staleness from there and keeps the row age as a feature.
"""
from __future__ import annotations

import functools
import os
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..ladders import DEFAULT_BOOKS_DB, DEFAULT_RFQ_DB, MarketQuote, _ro, game_markets

DEFAULT_KALSHI_DB = Path("./data/kalshi_books.db")


@dataclass(frozen=True)
class MatchRow:
    kalshi_ticker: str | None
    kalshi_side: str | None            # yes | no
    confidence: str                    # exact | rules_differ | shifted_line | none
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class KalshiQuote:
    yes_bid: float | None              # probabilities 0..1
    yes_ask: float | None
    bid_size: float | None
    ask_size: float | None
    bid_depth: float | None
    ask_depth: float | None
    at_ms: int


KalshiLookup = Callable[[list[str], int | None], dict[str, tuple[object, object | None]]]
NO_MATCH = MatchRow(None, None, "none", ("kalshi_books not available",))


def sport_of(game_prefix: str) -> str | None:
    """Sport of a game prefix via the sports registry, else the slug's first token."""
    try:
        from ..sports import registry
        sp = registry.sport_for_slug(game_prefix)
        if sp is not None:
            return sp.sport
    except Exception:  # registry import problems must not break pricing
        pass
    head = game_prefix.split("-", 1)[0].lower()
    return head or None


# ----------------------------------------------------------------- Polymarket
def poly_source(books_db: str | Path = DEFAULT_BOOKS_DB, rfq_db: str | Path = DEFAULT_RFQ_DB):
    def read(game_prefix: str, at_ms: int | None = None) -> list[MarketQuote]:
        return game_markets(game_prefix, at_ms, books_db=books_db, rfq_db=rfq_db)
    return read


def poly_confirmations(quotes: list[MarketQuote], at_ms: int | None = None, *,
                       books_db: str | Path = DEFAULT_BOOKS_DB) -> dict[str, int]:
    """position_id -> ms the book recorder was last seen alive (<= at_ms) while the token was subscribed.

    Heartbeat = newest book_top row of any asset at or before `at_ms`; a token counts as subscribed at
    `at_ms` when a subscriptions row covers it. Tokens not subscribed then are absent (no confirmation).
    """
    toks = [q.token_id for q in quotes if q.token_id]
    if not toks:
        return {}
    c = _ro(books_db)
    try:
        if at_ms is None:
            hb = c.execute("SELECT MAX(at_ms) FROM book_top").fetchone()[0]
        else:
            hb = c.execute("SELECT MAX(at_ms) FROM book_top WHERE at_ms <= ?", (at_ms,)).fetchone()[0]
        if hb is None:
            return {}
        t = at_ms if at_ms is not None else hb
        subscribed: set[str] = set()
        for i in range(0, len(toks), 500):
            chunk = toks[i:i + 500]
            q = (f"SELECT DISTINCT asset_id FROM subscriptions WHERE subscribed_ms <= ? AND (unsubscribed_ms IS NULL OR unsubscribed_ms > ?)"
                 f" AND asset_id IN ({','.join('?' * len(chunk))})")
            subscribed.update(r[0] for r in c.execute(q, (t, t, *chunk)))
        return {q.position_id: int(hb) for q in quotes if q.token_id in subscribed}
    finally:
        c.close()


# ----------------------------------------------------------------- Kalshi
def _kalshi_db_path(db: str | Path | None) -> Path:
    return Path(db or os.environ.get("KALSHI_BOOKS_DB_PATH") or DEFAULT_KALSHI_DB)


def kalshi_confirmation(at_ms: int | None = None, *, db: str | Path | None = None) -> int | None:
    """ms the Kalshi feed was last seen alive (newest kalshi_top row at or before at_ms); None when no store."""
    path = _kalshi_db_path(db)
    if not path.exists():
        return None
    c = _ro(path)
    try:
        if at_ms is None:
            r = c.execute("SELECT MAX(at_ms) FROM kalshi_top").fetchone()
        else:
            r = c.execute("SELECT MAX(at_ms) FROM kalshi_top WHERE at_ms <= ?", (at_ms,)).fetchone()
        return None if r is None or r[0] is None else int(r[0])
    except sqlite3.OperationalError:   # no kalshi_top table yet
        return None
    finally:
        c.close()


def kalshi_ticker_confirmations(tickers: list[str], *, db: str | Path | None = None) -> dict[str, int]:
    """ticker -> ms the Kalshi recorder last saw that ticker (ticker_seen.last_seen_ms). Live mode only: the table holds the
    current state, so a point-in-time replay cannot use it. Absent tickers fall back to the feed heartbeat."""
    path = _kalshi_db_path(db)
    tickers = sorted({t for t in tickers if t})
    if not tickers or not path.exists():
        return {}
    c = _ro(path)
    try:
        out: dict[str, int] = {}
        for i in range(0, len(tickers), 500):
            chunk = tickers[i:i + 500]
            out.update({r[0]: int(r[1]) for r in c.execute(
                f"SELECT ticker, last_seen_ms FROM ticker_seen WHERE ticker IN ({','.join('?' * len(chunk))})", chunk)})
        return out
    except sqlite3.OperationalError:
        return {}
    finally:
        c.close()


def no_kalshi(position_ids: list[str], at_ms: int | None = None) -> dict[str, tuple[MatchRow, None]]:
    return {pid: (NO_MATCH, None) for pid in position_ids}


def synthetic_kalshi(quotes: list[MarketQuote], *, seed: int = 1, jitter: float = 0.01, now_ms: int | None = None,
                     confidence: str = "exact") -> KalshiLookup:
    """Fabricate a Kalshi book per Poly leg: mid +- jitter, half the depth, 5 s old. Marked by ticker `SYN-...`."""
    by_pid = {q.position_id: q for q in quotes}

    def lookup(position_ids: list[str], at_ms: int | None = None) -> dict[str, tuple[MatchRow, KalshiQuote | None]]:
        rng = random.Random(seed)
        out: dict[str, tuple[MatchRow, KalshiQuote | None]] = {}
        for pid in position_ids:
            q = by_pid.get(pid)
            row = MatchRow(f"SYN-{pid[:8]}", "yes", confidence, ("synthetic",))
            if q is None or q.mid is None:
                out[pid] = (row, None)
                continue
            mid = min(0.99, max(0.01, q.mid + rng.uniform(-jitter, jitter)))
            hs = max(0.005, (q.best_ask - q.best_bid) / 2)
            t = (at_ms if at_ms is not None else (now_ms if now_ms is not None else q.at_ms)) - 5000
            out[pid] = (row, KalshiQuote(round(mid - hs, 4), round(mid + hs, 4), (q.bid_size or 0) / 2, (q.ask_size or 0) / 2,
                                         (q.bid_depth or 0) / 2, (q.ask_depth or 0) / 2, t))
        return out
    return lookup


def kalshi_source(mode: str = "auto", *, quotes: list[MarketQuote] | None = None, now_ms: int | None = None,
                  db: str | Path | None = None) -> KalshiLookup:
    """auto -> rfqbot.kalshi_books.kalshi_for_legs (over `db`); none -> every leg unmatched; synthetic -> fabricated exact books."""
    if mode == "none":
        return no_kalshi
    if mode == "synthetic":
        return synthetic_kalshi(quotes or [], now_ms=now_ms)
    if mode != "auto":
        raise ValueError(f"kalshi source must be auto|none|synthetic, got {mode!r}")
    from ..kalshi_books import kalshi_for_legs      # lazy: keeps `import rfqbot.marginals` free of websockets / httpx
    return functools.partial(kalshi_for_legs, db=db) if db is not None else kalshi_for_legs
