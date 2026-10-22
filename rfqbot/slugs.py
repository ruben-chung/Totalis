"""Polymarket slug grammar shared by quoting, risk, edge and backtest.

A game slug is ``{league}-{away}-{home}-{YYYY-MM-DD}`` followed by an optional market suffix
(``-spread-away-3pt5``, ``-total-44pt5``, ...). ``game_prefix_of`` returns the game part, which is
the key the ladder recorder and the game cache use. It is sport agnostic; ``rfqbot.clusters``
builds the stricter per-sport game key from a parsed LegSpec.
"""
from __future__ import annotations

import re

GAME_RE = re.compile(r"^([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")
DOUBLES_RE = re.compile(r"^([a-z]+-doubles-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")

# Fixed-point scale of the RFQ protocol: prices and share counts travel as integers x 1e6.
E6 = 1_000_000


def game_prefix_of(slug: str | None) -> str | None:
    """``nfl-dal-phi-2026-10-27-spread-away-3pt5`` -> ``nfl-dal-phi-2026-10-27``; None if no match."""
    if not slug:
        return None
    m = GAME_RE.match(slug) or DOUBLES_RE.match(slug)
    return m.group(1) if m else None
