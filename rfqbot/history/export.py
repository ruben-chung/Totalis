"""Engine-shaped export: a historical game's markets as rfqbot.ladders.MarketQuote rows."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..ladders import MarketQuote

DEFAULT_HISTORY_DB = Path("./data/history.db")


def game_markets_hist(game_prefix: str, offset_min: int = 5, *, history_db: str | Path = DEFAULT_HISTORY_DB,
                      include_unpriced: bool = True) -> list[MarketQuote]:
    """Every leg of the game with the pre-kickoff snapshot at `offset_min` as best_bid = best_ask = p
    (sizes None, so the engine's thin-book term applies). Legs without a snapshot come back with None
    prices and at_ms=0 unless include_unpriced=False. Same MarketQuote type as the live ladders."""
    c = sqlite3.connect(f"file:{Path(history_db)}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT l.condition_id, l.outcome_index, l.token_id, l.position_id, l.outcome, m.slug, m.title, m.game_start_time,"
            " p.t, p.p FROM hist_legs l JOIN hist_markets m USING(condition_id)"
            " LEFT JOIN hist_prices p ON p.token_id = l.token_id AND p.offset_min = ?"
            " WHERE l.game_prefix = ? ORDER BY m.slug, l.outcome_index", (int(offset_min), game_prefix)).fetchall()
    finally:
        c.close()
    out: list[MarketQuote] = []
    for r in rows:
        if r["p"] is None and not include_unpriced:
            continue
        px = None if r["p"] is None else float(r["p"])
        out.append(MarketQuote(
            position_id=r["position_id"] or "", token_id=r["token_id"] or "", slug=r["slug"] or "", title=r["title"] or "",
            outcome=r["outcome"] or "", outcome_index=int(r["outcome_index"]),
            best_bid=px, best_ask=px, bid_size=None, ask_size=None, bid_depth=None, ask_depth=None,
            at_ms=int(r["t"]) * 1000 if r["t"] is not None else 0, game_start_time=r["game_start_time"],
        ))
    return out


def resolved_games(history_db: str | Path = DEFAULT_HISTORY_DB, *, sport: str | None = None,
                   min_resolved_share: float = 0.95) -> list[str]:
    """Game prefixes whose markets are (almost all) resolved, newest first."""
    c = sqlite3.connect(f"file:{Path(history_db)}?mode=ro", uri=True)
    try:
        sql = "SELECT game_prefix FROM hist_games WHERE n_markets > 0 AND n_resolved >= ? * n_markets"
        args: list = [min_resolved_share]
        if sport:
            sql += " AND sport = ?"; args.append(sport)
        return [r[0] for r in c.execute(sql + " ORDER BY game_start_time DESC", args)]
    finally:
        c.close()
