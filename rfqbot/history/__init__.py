"""Historical resolved-games dataset (data/history.db) and resolution tracking.

    python -m rfqbot.history pull    --sport nfl --since 2026-09-01   # closed Gamma events -> hist_games/markets/legs
    python -m rfqbot.history prices  --minutes-before 60,15,5         # pre-kickoff CLOB price snapshots -> hist_prices
    python -m rfqbot.history resolve                                   # rfq.db market_resolutions for RFQ legs + history
    python -m rfqbot.history pairs   --sport nfl                       # per resolved game, every pair of legs -> hist_pairs
    python -m rfqbot.history stats

`game_markets_hist(game_prefix, offset_min)` returns `rfqbot.ladders.MarketQuote` rows built from the
snapshots (best_bid = best_ask = p) so the joint engine runs on history unchanged.
"""
from .export import game_markets_hist  # noqa: F401
from .store import HistoryStore  # noqa: F401
