"""Sport market identity layer: parse venue markets into sport-agnostic LegSpecs and match them across venues.

Importing this package registers the built-in sports (see registry.py). A new sport adds
its own ``<sport>_poly.py`` / ``<sport>_kalshi.py`` and a ``register(SportSpec(...))`` call,
then is imported here.
"""
from . import registry  # noqa: F401
from . import nfl  # noqa: F401  (registers "nfl")

from . import nba  # noqa: F401  (registers "nba")
