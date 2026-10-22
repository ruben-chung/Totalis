"""pairs: for every resolved game, every pair of parsed legs with their realized outcomes -> hist_pairs.

Ordering / cap: legs are ranked FG, 1H, 2H first (then quarters), moneyline/spread/total/team_total before
the rest; pairs are generated in that rank order so a per-game cap keeps the pairs the engine validates
first. Both sides of the same market are never paired (won_a == 1 - won_b by construction).
"""
from __future__ import annotations

import logging
from itertools import combinations

from .store import MIN_RESOLVED_SHARE, HistoryStore

log = logging.getLogger(__name__)

PERIOD_RANK = {"FG": 0, "1H": 1, "2H": 2, "1Q": 3, "2Q": 4, "3Q": 5, "4Q": 6}
FAMILY_RANK = {"moneyline": 0, "spread": 1, "total": 2, "team_total": 3}


def _rank(leg) -> tuple:
    return (PERIOD_RANK.get(leg["period"], 9), FAMILY_RANK.get(leg["family"], 9), leg["family"] or "", leg["line"] or 0,
            leg["condition_id"], leg["outcome_index"])


def build_pairs(store: HistoryStore, *, sport: str | None = None, max_pairs_per_game: int = 60_000,
                periods: tuple[str, ...] | None = None, rebuild: bool = False) -> dict[str, int]:
    games = store.rows("SELECT game_prefix, sport FROM hist_games WHERE n_markets > 0 AND n_resolved >= ? * n_markets"
                       + (" AND sport = ?" if sport else "") + " ORDER BY game_start_time", MIN_RESOLVED_SHARE, *([sport] if sport else []))
    counts = {"games": 0, "legs": 0, "pairs": 0, "capped_games": 0}
    capped: list[str] = []
    if rebuild:
        with store:
            store.conn.execute("DELETE FROM hist_pairs" + (" WHERE sport = ?" if sport else ""), *([(sport,)] if sport else []))
    for g, sp in games:   # one transaction per game so other writers (prices, resolve) are never blocked for long
        with store:
            if not rebuild and store.scalar("SELECT 1 FROM hist_pairs WHERE game_prefix=? LIMIT 1", g):
                continue
            legs = [l for l in store.rows(
                "SELECT condition_id, outcome_index, family, period, side, team, line, won FROM hist_legs"
                " WHERE game_prefix=? AND won IS NOT NULL AND parse_error IS NULL AND family IS NOT NULL", g)]
            if periods:
                legs = [l for l in legs if l["period"] in periods]
            legs.sort(key=_rank)
            rows = []
            for a, b in combinations(legs, 2):
                if a["condition_id"] == b["condition_id"]:
                    continue
                rows.append((g, sp, a["condition_id"], a["outcome_index"], b["condition_id"], b["outcome_index"],
                             a["family"], a["period"], a["side"], a["team"], a["line"],
                             b["family"], b["period"], b["side"], b["team"], b["line"], a["won"], b["won"]))
                if max_pairs_per_game and len(rows) >= max_pairs_per_game:
                    counts["capped_games"] += 1
                    capped.append(g)
                    break
            n = store.insert_pairs(rows)
            counts["games"] += 1
            counts["legs"] += len(legs)
            counts["pairs"] += n
    if capped:
        log.warning("pairs: %d games hit the %d-pair cap (later-ranked pairs dropped): %s%s", len(capped), max_pairs_per_game,
                    ", ".join(capped[:10]), " ..." if len(capped) > 10 else "")
    log.info("pairs: %s", counts)
    return counts


def pair_summary(store: HistoryStore, *, sport: str | None = None) -> list[tuple[str, str, str, str, int, int]]:
    """(family_a, period_a, family_b, period_b, pairs, both_won) for a quick look at the table."""
    sql = ("SELECT family_a, period_a, family_b, period_b, COUNT(*), SUM(won_a AND won_b) FROM hist_pairs"
           + (" WHERE sport = ?" if sport else "") + " GROUP BY 1,2,3,4 ORDER BY 5 DESC")
    return [tuple(r) for r in store.rows(sql, *([sport] if sport else []))]
