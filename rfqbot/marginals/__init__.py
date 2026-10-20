"""Marginal layer: per-leg fair probability and expected error from Polymarket + Kalshi books."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..ladders import DEFAULT_BOOKS_DB, DEFAULT_RFQ_DB, MarketQuote
from .config import MarginalConfig, load_config
from .model import Marginal, VenueEstimate, VenueQuote, combine, kalshi_quote, marginal_for_leg, poly_quote
from .sources import (KalshiQuote, MatchRow, kalshi_confirmation, kalshi_source, poly_confirmations, poly_source,
                      sport_of)

__all__ = ["Marginal", "MarginalConfig", "VenueEstimate", "VenueQuote", "KalshiQuote", "MatchRow", "LegMarginal",
           "combine", "kalshi_quote", "kalshi_source", "load_config", "marginal_for_leg", "poly_quote", "poly_source",
           "sport_of", "compute_game"]


@dataclass(frozen=True)
class LegMarginal:
    quote: MarketQuote          # the Polymarket leg and its book
    match: Any | None           # MatchRow (duck-typed) or None when the leg has no match row
    kalshi: Any | None          # KalshiQuote (duck-typed) or None
    marginal: Marginal


def compute_game(game_prefix: str, at_ms: int | None = None, *, cfg: MarginalConfig | None = None,
                 books_db: str | Path = DEFAULT_BOOKS_DB, rfq_db: str | Path = DEFAULT_RFQ_DB,
                 kalshi_mode: str = "auto", kalshi_db: str | Path | None = None,
                 poly: Callable | None = None, kalshi: Callable | None = None,
                 poly_confirm: Callable | None = None, kalshi_confirm: Callable | None = None,
                 now_ms: int | None = None) -> list[LegMarginal]:
    """Marginals for every leg of a game.

    `at_ms` is the evaluation instant for the point-in-time path (quotes after it are never seen,
    staleness is measured against it); None evaluates the latest quotes against the wall clock.
    `poly` / `kalshi` override the store readers (tests, synthetic); when overridden, confirmations
    default to none unless `poly_confirm(quotes, at_ms)` / `kalshi_confirm(at_ms)` are given too.
    """
    cfg = cfg or load_config()
    eval_ms = at_ms if at_ms is not None else (now_ms if now_ms is not None else int(time.time() * 1000))
    if poly is None:
        poly = poly_source(books_db, rfq_db)
        poly_confirm = poly_confirm or (lambda qs, t: poly_confirmations(qs, t, books_db=books_db))
    quotes = poly(game_prefix, at_ms)
    if kalshi is None:
        kalshi = kalshi_source(kalshi_mode, quotes=quotes, now_ms=eval_ms, db=kalshi_db)
        if kalshi_mode == "auto" and kalshi_confirm is None:
            kalshi_confirm = lambda t: kalshi_confirmation(t, db=kalshi_db)
    matches = kalshi([q.position_id for q in quotes], at_ms)
    confirmed = poly_confirm(quotes, at_ms) if poly_confirm else {}
    k_confirmed = kalshi_confirm(at_ms) if kalshi_confirm else None
    sport = sport_of(game_prefix)
    out: list[LegMarginal] = []
    for q in quotes:
        row, kq = matches.get(q.position_id, (None, None))
        m = marginal_for_leg(q, row, kq, sport=sport, now_ms=eval_ms, cfg=cfg,
                             poly_confirmed_ms=confirmed.get(q.position_id), kalshi_confirmed_ms=k_confirmed)
        out.append(LegMarginal(q, row, kq, m))
    return out
