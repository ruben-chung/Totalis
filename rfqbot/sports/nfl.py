"""NFL registration (see registry.py)."""
from . import nfl_kalshi, nfl_poly
from .registry import SportSpec, register

NFL = register(SportSpec(
    sport="nfl",
    slug_prefix="nfl-",
    slug_date_basis="utc",
    parse_poly=nfl_poly.parse_poly_nfl,
    parse_kalshi=nfl_kalshi.parse_kalshi_nfl,
    kalshi_series=nfl_kalshi.SERIES,
    kalshi_series_without_poly=nfl_kalshi.SERIES_WITHOUT_POLY_FAMILY,
    gamma_types=nfl_poly.GAMMA_TYPES,
))
