"""Player-name normalization and the single-candidate player-prop matching rule.

Polymarket player props carry the player's display name and no team ("Michael Porter Jr.:
Points O/U 21.5", slug token ``michael-porter-jr``). Kalshi carries the team, first initial,
surname and jersey number in the ticker (``BKNMPORTER1``) and the display name in the title
("Michael Porter Jr.: 22+ points"). Nothing here guesses: every transform is a fixed function
of the string and the matching rule requires exactly one candidate.

Normalization (``name_key``), applied identically on both venues:
  1. strip accents (NFKD, drop combining marks): "Sengün" -> "Sengun", "Dončić" -> "Doncic"
  2. lowercase
  3. drop every character that is not a letter, digit or whitespace (apostrophes, periods,
     hyphens): "De'Aaron" -> "deaaron", "P.J." -> "pj", "Alexander-Walker" -> "alexanderwalker"
  4. split on whitespace and drop trailing generational suffixes jr / sr / ii / iii / iv / v
  5. join with single spaces
  "Michael Porter Jr." -> "michael porter", "Trey Murphy III" -> "trey murphy".

``initial_surname`` is first letter of the first token + last token, uppercased, which is the
Kalshi ticker convention (``MPORTER``, ``NALEXANDERWALKER``, ``SGILGEOUSALEXANDER``).
``player_key`` is the same identity in lowercase dotted form (``m.porter``) and is what both
parsers put in ``LegSpec.bucket``: the brief's matching identity is initial + surname, and the
venues do differ on first names ("Alexandre Sarr" on Polymarket, "Alex Sarr" on Kalshi,
2026-10-21). The full display name stays in ``venue_ids["player"]``. Because this key can
collide (two players with the same initial and surname in one game, e.g. Jalen and Jaylin
Williams if both get markets), ``bucket_collisions`` finds such cases and the coverage report
prints them; the shared matcher alone cannot tell them apart.
``poly_slug`` reproduces the Polymarket slug token from the title name so the two can be
cross-checked (``michael-porter-jr``, ``karl-anthony-towns``, ``deaaron-fox``).

Matching rule (``match_player_prop``): a Polymarket player-prop leg matches a Kalshi market
only when ALL hold: same game key, same family (stat), same line, and exactly one Kalshi
market in that game/stat/line whose ``initial_surname`` equals the Polymarket name's. Zero or
two candidates -> no match. Settlement notes are the caller's job (see nba_kalshi rules; the
did-not-play rule differs: Polymarket resolves No, Kalshi settles to a fair price).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Iterable, TypeVar

from .legspec import LegSpec

SUFFIX_TOKENS = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
PLAYER_FAMILIES = ("player_points", "player_rebounds", "player_assists", "player_threes",
                   "player_pra", "player_steals", "player_blocks")


class PlayerNameError(ValueError):
    pass


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def name_tokens(name: str) -> list[str]:
    """Normalized tokens with trailing generational suffixes removed. Raises on an empty result."""
    s = re.sub(r"[^a-z0-9\s]", "", strip_accents(name or "").lower())
    toks = s.split()
    while len(toks) > 1 and toks[-1] in SUFFIX_TOKENS:
        toks.pop()
    if not toks:
        raise PlayerNameError(f"player name normalizes to nothing: {name!r}")
    return toks


def name_key(name: str) -> str:
    """Venue-independent player identity used as the LegSpec ``bucket``."""
    return " ".join(name_tokens(name))


def initial_surname(name: str) -> str:
    """Kalshi ticker form: first initial + surname, uppercase letters/digits only."""
    toks = name_tokens(name)
    return (toks[0][0] + toks[-1]).upper()


def player_key(name: str) -> str:
    """LegSpec bucket for a player: ``{initial}.{surname}`` lowercase, e.g. ``m.porter``."""
    toks = name_tokens(name)
    return f"{toks[0][0]}.{toks[-1]}"


def bucket_collisions(specs: Iterable[LegSpec]) -> dict[tuple[str, str], set[str]]:
    """(game_key, bucket) -> the distinct display names sharing it, for every collision (len > 1)."""
    seen: dict[tuple[str, str], set[str]] = {}
    for s in specs:
        if s.family in PLAYER_FAMILIES and s.bucket is not None:
            seen.setdefault((s.game_key, s.bucket), set()).add(s.venue_ids.get("player", s.bucket))
    return {k: v for k, v in seen.items() if len(v) > 1}


def poly_slug(name: str) -> str:
    """The slug token Polymarket derives from a display name (suffixes kept, hyphens kept as
    separators, apostrophes and periods dropped, accents stripped)."""
    s = strip_accents(name or "").lower().replace("'", "").replace(".", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        raise PlayerNameError(f"player name slugifies to nothing: {name!r}")
    return s


T = TypeVar("T")


def single_candidate(poly_name: str, candidates: Iterable[T], name_of: Callable[[T], str]) -> T | None:
    """The one candidate whose initial+surname equals ``poly_name``'s, or None when there are
    zero or several. Never picks among several."""
    want = initial_surname(poly_name)
    hits = [c for c in candidates if initial_surname(name_of(c)) == want]
    return hits[0] if len(hits) == 1 else None


def match_player_prop(poly: LegSpec, kalshi_specs: Iterable[LegSpec]) -> tuple[LegSpec, str] | None:
    """Apply the single-candidate rule to a Polymarket player-prop LegSpec against Kalshi
    YES-side specs. Returns (kalshi_spec, kalshi_side) or None. ``kalshi_side`` is "yes" when
    the Polymarket side is the over, "no" for the under."""
    if poly.family not in PLAYER_FAMILIES or poly.bucket is None or poly.line is None:
        raise PlayerNameError(f"not a player prop: {poly.family} {poly.bucket} {poly.line}")
    poly_name = poly.venue_ids.get("player") or poly.bucket
    pool = [k for k in kalshi_specs
            if k.game_key == poly.game_key and k.family == poly.family and k.line == poly.line and k.bucket is not None]
    hit = single_candidate(poly_name, pool, lambda k: k.venue_ids.get("player") or k.bucket or "")
    if hit is None:
        return None
    if poly.side == "over":
        return hit, "yes"
    if poly.side == "under":
        return hit, "no"
    raise PlayerNameError(f"player prop side must be over/under, got {poly.side!r}")
