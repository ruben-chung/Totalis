"""Polymarket player-prop closing lines and game lines, read-only from the Data Engineer's data/history.db.

NBA player props live inside the main game event on Gamma (sportsMarketType points / rebounds /
assists / threes), pulled by `python -m rfqbot.history pull --sport nba` (one writer: the Data
Engineer runs it). Two title grammars exist: "Rui Hachimura: Points O/U 14.5" (parsed by
rfqbot.sports.nba_poly into family player_points, bucket = player_key, line, side) and the older
"Nicolas Claxton: Points Over 13.5" (Dec 2025 - Apr 2026), which the parser rejects. Rejected rows
are recovered here from the slug `...-points-nicolas-claxton-13pt5` + hist_markets.line, with
outcome_index 0 (Yes) = over; the player key comes from the same nba_players normalization.

hist_prices holds the last CLOB price-history point at or before tip minus offset_min; the
15-minute offset is the closing line. Illiquid props can have that point well before the offset,
so `t` is stored and the fit can filter on it.

Game lines: the full-game total and spread ladders per game; the main line is the median strike
of each ladder (Polymarket lists symmetric alternates around it), and the implied team points
line is (total - spread_team) / 2 with spread_team negative for the favourite.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

from ..sports import nba_players, nba_teams
from .store import PropsStore

FAMILY_STAT = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3m"}
MARKET_TYPE_STAT = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m", "3pt": "fg3m"}
_SLUG_PROP = re.compile(r"^nba-[a-z]+-[a-z]+-\d{4}-\d{2}-\d{2}-(points|rebounds|assists|threes)-(.+)-(\d+)pt5$")


def game_key_from_prefix(game_prefix: str) -> str | None:
    """nba-okc-ind-2025-10-23 -> nba|2025-10-23|OKC@IND (NBA slugs already carry the ET date)."""
    parts = game_prefix.split("-")
    if len(parts) < 6 or parts[0] != "nba":
        return None
    away, home, ymd = parts[1], parts[2], "-".join(parts[3:6])
    try:
        return f"nba|{ymd}|{nba_teams.BY_POLY_SLUG[away].code}@{nba_teams.BY_POLY_SLUG[home].code}"
    except KeyError:
        return None


def prop_from_slug(slug: str, title: str | None, market_line: float | None) -> tuple[str, str, str, float] | None:
    """Older-grammar fallback -> (stat, player_key, player_name, line) or None."""
    m = _SLUG_PROP.match(slug or "")
    if not m:
        return None
    stat = MARKET_TYPE_STAT[m.group(1)]
    name = (title or "").split(":", 1)[0].strip() or m.group(2).replace("-", " ")
    line = float(market_line) if market_line is not None else float(m.group(3)) + 0.5
    try:
        key = nba_players.player_key(name)
    except nba_players.PlayerNameError:
        return None
    return stat, key, name, line


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pull_poly_lines(store: PropsStore, *, history_db: str | Path = "./data/history.db", offset_min: int = 15,
                    out=print) -> dict[str, int]:
    n = {"legs": 0, "lines": 0, "parsed": 0, "slug_fallback": 0, "skipped": 0, "no_game_key": 0, "priced": 0,
         "game_lines": 0}
    path = Path(history_db)
    if not path.exists():
        out(f"no history db at {path}")
        return n
    conn = _ro(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(hist_legs)")}
        err_col = "l.parse_error" if "parse_error" in cols else "NULL"
        sql = f"""SELECT l.condition_id, l.outcome_index, l.game_prefix, l.token_id, l.family, l.bucket, l.line, l.side, l.won,
                         {err_col} AS parse_error, m.slug, m.title, m.line AS market_line, m.end_date,
                         p.p AS price, p.t AS price_t
                  FROM hist_legs l JOIN hist_markets m ON m.condition_id = l.condition_id
                  LEFT JOIN hist_prices p ON p.token_id = l.token_id AND p.offset_min = ?
                  WHERE m.sport = 'nba' AND (l.family LIKE 'player_%' OR m.slug LIKE '%-points-%' OR m.slug LIKE '%-rebounds-%'
                        OR m.slug LIKE '%-assists-%' OR m.slug LIKE '%-threes-%')"""
        rows: dict[str, dict] = {}
        for r in conn.execute(sql, (offset_min,)):
            n["legs"] += 1
            stat = FAMILY_STAT.get(r["family"] or "")
            if stat is not None and r["line"] is not None and r["bucket"]:
                if r["side"] != "over":
                    n["skipped"] += 1
                    continue
                key, name, line = r["bucket"], (r["title"] or "").split(":", 1)[0] or None, float(r["line"])
                n["parsed"] += 1
            else:
                fb = prop_from_slug(r["slug"], r["title"], r["market_line"])
                if fb is None or int(r["outcome_index"] or 0) != 0:
                    n["skipped"] += 1
                    continue
                stat, key, name, line = fb
                n["slug_fallback"] += 1
            gk = game_key_from_prefix(r["game_prefix"])
            if gk is None:
                n["no_game_key"] += 1
                continue
            won = r["won"]
            rows[f"{r['condition_id']}:{r['outcome_index']}"] = {
                "source": "polymarket", "ticker": f"{r['condition_id']}:{r['outcome_index']}", "player_key": key,
                "player_name": name, "team": None, "game_key": gk, "stat": stat, "line": line,
                "p_over": r["price"], "t": r["price_t"],
                "result": None if won is None else ("yes" if won else "no"), "actual": None,
                "open_time": None, "close_time": r["end_date"],
            }
            if r["price"] is not None:
                n["priced"] += 1
        n["lines"] = store.upsert("prop_lines", list(rows.values()))
        n["game_lines"] = store.upsert("game_lines", list(_game_lines(conn)))
    finally:
        conn.close()
    return n


def _game_lines(conn: sqlite3.Connection):
    totals: dict[str, list[float]] = defaultdict(list)
    spreads: dict[tuple[str, str], list[float]] = defaultdict(list)   # (game_prefix, favoured team) -> lines (negative)
    for r in conn.execute("""SELECT l.game_prefix, l.family, l.side, l.team, l.line FROM hist_legs l
                             WHERE l.game_prefix LIKE 'nba-%' AND l.period = 'FG' AND l.line IS NOT NULL
                               AND ((l.family = 'total' AND l.side = 'over') OR (l.family = 'spread' AND l.side = l.team))"""):
        if r["family"] == "total":
            totals[r["game_prefix"]].append(float(r["line"]))
        else:
            spreads[(r["game_prefix"], r["team"])].append(float(r["line"]))
    for prefix in set(totals) | {g for g, _ in spreads}:
        gk = game_key_from_prefix(prefix)
        if gk is None:
            continue
        away, home = gk.split("|")[2].split("@")
        total = statistics.median(totals[prefix]) if totals.get(prefix) else None
        fav_lines = {t: statistics.median(v) for (g, t), v in spreads.items() if g == prefix}
        # one favoured team carries the ladder; its median line is negative, the other team gets the opposite sign
        fav = min(fav_lines, key=lambda t: fav_lines[t]) if fav_lines else None
        for team in (away, home):
            if fav is None:
                spread = None
            elif team == fav:
                spread = fav_lines[fav]
            else:
                spread = -fav_lines[fav]
            tpl = None if total is None or spread is None else (total - spread) / 2
            yield {"source": "polymarket", "game_key": gk, "team": team, "total_line": total, "spread_line": spread,
                   "team_pts_line": tpl, "n_total": len(totals.get(prefix, [])), "n_spread": len(fav_lines and spreads.get((prefix, fav), []))}
