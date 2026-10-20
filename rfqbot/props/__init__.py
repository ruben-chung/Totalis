"""NBA player-prop dataset (data/props.db) and the first share-model fit for the dependence extension.

    python -m rfqbot.props pull-box   --season 2026            # ESPN box scores for 2025-26 (regular + playoffs)
    python -m rfqbot.props pull-lines --kalshi [--poly]        # settled Kalshi props + pre-tip prices; Polymarket from history.db
    python -m rfqbot.props fit                                 # share model, residuals, correlations -> docs + JSON
    python -m rfqbot.props stats
"""
from .store import PropsStore, DEFAULT_DB  # noqa: F401
