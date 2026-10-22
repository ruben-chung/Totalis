"""Segments: (sport, n_legs bucket, direction, structure, sigma bucket) and the pooling ladder.

A segment key is ``sport|n_legs|direction|structure|sigma`` (``nba|2|BUY|disjoint|s1``). ``*`` in a
field means "pooled over that field". The pooling ladder drops, in order: sigma bucket, structure
(same_game | disjoint), n_legs bucket, sport; the last node is ``*|*|direction|*|*`` and, beyond it,
the global ``*|*|*|*|*``. ``level`` counts how many fields were dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator, Sequence

N_LEGS_BUCKETS = ("2", "3-4", "5+")
STRUCTURES = ("disjoint", "same_game")
DIRECTIONS = ("BUY", "SELL")
ANY = "*"
LEVEL_NAMES = ("full", "sigma", "structure", "n_legs", "sport", "global")


def n_legs_bucket(n: int) -> str:
    """1- and 2-leg combos share the ``2`` bucket (1-leg RFQs are rare and priced like a 2-leg disjoint)."""
    if n <= 2:
        return "2"
    if n <= 4:
        return "3-4"
    return "5+"


def rfq_sigma(u_m: float | None, u_d: float | None) -> float:
    """The one sigma definition the segments bucket on: ``u_m + u_d`` (model and dependence uncertainty
    of the combo fair). Decisions carry both; a print has no quoting-time u_d, so the markout monitor
    passes ``u_d = 0`` as its proxy and buckets through this same function."""
    return (u_m or 0.0) + (u_d or 0.0)


def sigma_bucket(sigma: float | None, cuts: Sequence[float]) -> str:
    """``s0`` below the first cut, ``s1`` between, ... ``s<len(cuts)>`` at or above the last."""
    if sigma is None:
        return ANY
    i = 0
    for c in cuts:
        if sigma >= c:
            i += 1
    return f"s{i}"


@dataclass(frozen=True, order=True)
class Segment:
    sport: str
    n_legs: str
    direction: str
    structure: str
    sigma: str

    @property
    def key(self) -> str:
        return "|".join((self.sport, self.n_legs, self.direction, self.structure, self.sigma))

    @classmethod
    def parse(cls, key: str) -> "Segment":
        parts = key.split("|")
        if len(parts) != 5:
            raise ValueError(f"segment key needs 5 fields: {key!r}")
        return cls(*parts)

    @classmethod
    def of(cls, sport: str, n_legs: int, direction: str, same_game: bool, sigma: float | None,
           cuts: Sequence[float]) -> "Segment":
        return cls(sport or ANY, n_legs_bucket(n_legs), direction, "same_game" if same_game else "disjoint",
                   sigma_bucket(sigma, cuts))

    @property
    def level(self) -> int:
        """0 = fully specified; the ladder index otherwise (see module docstring)."""
        if self.direction == ANY:
            return 5
        if self.sport == ANY:
            return 4
        if self.n_legs == ANY:
            return 3
        if self.structure == ANY:
            return 2
        if self.sigma == ANY:
            return 1
        return 0

    def parent(self) -> "Segment | None":
        if self.sigma != ANY:
            return replace(self, sigma=ANY)
        if self.structure != ANY:
            return replace(self, structure=ANY)
        if self.n_legs != ANY:
            return replace(self, n_legs=ANY)
        if self.sport != ANY:
            return replace(self, sport=ANY)
        if self.direction != ANY:
            return replace(self, direction=ANY)
        return None

    def ladder(self) -> Iterator["Segment"]:
        """Self, then each parent up to the global node."""
        s: Segment | None = self
        while s is not None:
            yield s
            s = s.parent()

    def contains(self, other: "Segment") -> bool:
        """True when ``other`` (usually a full cell) falls under this (possibly pooled) node."""
        return all(a == ANY or a == b for a, b in zip(self.key.split("|"), other.key.split("|")))


GLOBAL = Segment(ANY, ANY, ANY, ANY, ANY)
