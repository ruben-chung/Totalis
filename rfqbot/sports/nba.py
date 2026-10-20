"""NBA registration (see registry.py). Slug dates are Eastern, so no UTC/ET offset against Kalshi."""
from . import nba_kalshi, nba_poly
from .registry import SportSpec, register

NBA = register(SportSpec(
    sport="nba",
    slug_prefix="nba-",
    slug_date_basis="et",
    parse_poly=nba_poly.parse_poly_nba,
    parse_kalshi=nba_kalshi.parse_kalshi_nba,
    kalshi_series=nba_kalshi.SERIES,
    kalshi_series_without_poly=nba_kalshi.SERIES_WITHOUT_POLY_FAMILY,
    gamma_types=nba_poly.GAMMA_TYPES,
    # Gamma description phrases for the shared check_gamma OT test (merged over coverage.DEFAULT_OT_PHRASES)
    extra={"ot_phrases": {"included": ("including any overtime periods", "including all overtime periods"),
                          "excluded": ("at halftime only",)}},
))
