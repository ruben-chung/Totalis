"""Quote input and the v1 fair-probability rule.

``Quote`` mirrors ``rfqbot.ladders.MarketQuote`` field for field so either can be passed to the
engine (duck typed; only the fields below are read).

v1 fair rule (to be replaced by the marginal layer; keep callers on ``fair_from_quote``):
    fair  = (best_bid + best_ask) / 2
    err   = (best_ask - best_bid) / 2  +  THIN_TERM / sqrt(1 + min(bid_size, ask_size) / THIN_SIZE)
    None when either side is missing or the book is crossed/invalid.
The thin-book term adds up to ``THIN_TERM`` (0.02) for an empty book and decays with the
smaller top-of-book size measured in ``THIN_SIZE`` (100) shares. A quote at exactly 0.50 with no
size on either side is treated as a placeholder (``PLACEHOLDER_ERR`` 0.25): in the resolved-game
history 21% of snapshot legs sat there, all untraded. ``err`` is treated as one
standard deviation of the observation in the ladder fit (weights 1/err^2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

THIN_TERM = 0.02
THIN_SIZE = 100.0
MIN_ERR = 0.002
PLACEHOLDER_ERR = 0.25     # a sizeless quote at exactly 0.50: an untraded placeholder, not a price


@dataclass(frozen=True)
class Quote:
    position_id: str
    token_id: str
    slug: str
    title: str
    outcome: str
    outcome_index: int
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    at_ms: int = 0
    game_start_time: str | None = None


@dataclass(frozen=True)
class Fair:
    p: float
    err: float


def fair_from_quote(q: Any) -> Fair | None:
    bid, ask = q.best_bid, q.best_ask
    if bid is None or ask is None:
        return None
    bid, ask = float(bid), float(ask)
    if not (0.0 <= bid <= ask <= 1.0):
        return None
    sizes = [s for s in (q.bid_size, q.ask_size) if s is not None]
    size = min(sizes) if sizes else 0.0
    if not sizes and abs(bid - 0.5) < 1e-9 and abs(ask - 0.5) < 1e-9:
        # 2,603 of 12,195 historical snapshot legs sat at exactly 0.50 with no book: untraded placeholders
        return Fair(p=0.5, err=PLACEHOLDER_ERR)
    err = (ask - bid) / 2 + THIN_TERM / math.sqrt(1 + max(size, 0.0) / THIN_SIZE)
    return Fair(p=(bid + ask) / 2, err=max(err, MIN_ERR))
