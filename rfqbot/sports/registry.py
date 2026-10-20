"""Per-sport registry: how to parse each venue and how game dates are encoded.

A sport registers one ``SportSpec``; ``kalshi-sync``, ``coverage`` and the matcher look
everything up here, so adding a sport (NBA, soccer) means adding a module that calls
``register(...)`` and importing it from ``rfqbot.sports.__init__`` (or anywhere before use).

Slug-date convention (verified 2026-10-20 against Gamma ``gameStartTime``):
  * NFL: Polymarket slugs carry the **UTC** date of kickoff (all 10 night games are +1 day).
  * NBA: Polymarket slugs carry the **Eastern** date (25/25 late tips match ET).
  * Kalshi tickers carry the Eastern date for every sport.
``slug_date_basis`` tells the Polymarket parser what ``LegSpec.game_date_basis`` to emit when
it has no kickoff time (``"utc"`` -> ``"utc_slug"``, ``"et"`` -> ``"et"``). The matcher then
resolves the game against Kalshi with the one-day team-pair tolerance either way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .legspec import LegSpec

PolyParser = Callable[..., LegSpec]      # (slug, title, outcome, outcome_index, *, position_id=None, kickoff_utc=None)
KalshiParser = Callable[..., LegSpec]    # (ticker, title, floor_strike, strike_type, rules_primary, expected_expiration_time, *, yes_sub_title=None, rules_secondary=None)


@dataclass(frozen=True)
class SportSpec:
    sport: str                                   # "nfl" | "nba" | ...
    slug_prefix: str                             # Polymarket slug prefix incl. dash, e.g. "nfl-"
    slug_date_basis: str                         # "utc" | "et"  (see module docstring)
    parse_poly: PolyParser
    parse_kalshi: KalshiParser
    kalshi_series: Mapping[str, tuple[str, str, str]]   # series ticker -> (family, period, suffix kind)
    kalshi_series_without_poly: tuple[str, ...] = ()    # known game-level series with no Poly family
    gamma_types: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # Gamma sportsMarketType -> (family, period)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.slug_date_basis not in ("utc", "et"):
            raise ValueError(f"slug_date_basis must be 'utc' or 'et', got {self.slug_date_basis!r}")

    @property
    def game_date_basis(self) -> str:
        """The LegSpec.game_date_basis a slug-only Polymarket parse should carry."""
        return "utc_slug" if self.slug_date_basis == "utc" else "et"


_REGISTRY: dict[str, SportSpec] = {}


def register(spec: SportSpec) -> SportSpec:
    if spec.sport in _REGISTRY and _REGISTRY[spec.sport] is not spec:
        raise ValueError(f"sport {spec.sport!r} already registered")
    _REGISTRY[spec.sport] = spec
    return spec


def get(sport: str) -> SportSpec:
    try:
        return _REGISTRY[sport]
    except KeyError:
        raise KeyError(f"sport {sport!r} not registered; known: {sorted(_REGISTRY)}") from None


def sports() -> list[str]:
    return sorted(_REGISTRY)


def sport_for_slug(slug: str) -> SportSpec | None:
    for s in _REGISTRY.values():
        if slug.startswith(s.slug_prefix):
            return s
    return None
