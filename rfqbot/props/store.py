"""sqlite store for the NBA player-prop dataset (data/props.db).

Tables
  games       one row per game (ESPN event id), with the canonical game key `nba|{ET date}|{AWAY}@{HOME}`
  box_team    one row per team per game: points, opponent points, margin, overtime periods
  box_player  one row per listed player per game: minutes, pts / reb / ast / 3pm, starter, played
  prop_lines  one row per (source, market): player, game, stat, line, pre-tip P(over), settlement
  fetch_log   what was fetched when (incremental reruns)
All team codes are the canonical codes of rfqbot.sports.nba_teams; player_key is
rfqbot.sports.nba_players.player_key (initial.surname) and team disambiguates it.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ..db import ro_connect

DEFAULT_DB = Path("./data/props.db")
DEFAULT_PARAMS = Path(__file__).resolve().parents[2] / "claude-sessions" / "props_fit.json"   # a report; nothing in the package reads it

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,      -- ESPN event id
    game_key     TEXT NOT NULL,         -- nba|YYYY-MM-DD|AWAY@HOME (ET date)
    season       INTEGER NOT NULL,      -- ESPN season year: 2026 = 2025-26
    season_type  INTEGER NOT NULL,      -- 1 preseason | 2 regular | 3 postseason | 5 play-in
    date_et      TEXT NOT NULL,
    tip_utc      TEXT NOT NULL,
    away         TEXT NOT NULL,
    home         TEXT NOT NULL,
    away_pts     INTEGER,
    home_pts     INTEGER,
    ot_periods   INTEGER,
    status       TEXT NOT NULL,         -- final | scheduled | in_progress | postponed | ...
    fetched_ms   INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_games_key ON games(game_key);
CREATE INDEX IF NOT EXISTS ix_games_date ON games(date_et);

CREATE TABLE IF NOT EXISTS box_team (
    game_id    TEXT NOT NULL,
    game_key   TEXT NOT NULL,
    date_et    TEXT NOT NULL,
    team       TEXT NOT NULL,
    opponent   TEXT NOT NULL,
    home       INTEGER NOT NULL,
    points     INTEGER NOT NULL,
    opp_points INTEGER NOT NULL,
    margin     INTEGER NOT NULL,        -- points - opp_points
    ot_periods INTEGER NOT NULL,
    PRIMARY KEY (game_id, team)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS box_player (
    game_id     TEXT NOT NULL,
    game_key    TEXT NOT NULL,
    date_et     TEXT NOT NULL,
    team        TEXT NOT NULL,
    opponent    TEXT NOT NULL,
    home        INTEGER NOT NULL,
    player_id   TEXT NOT NULL,          -- ESPN athlete id
    player_name TEXT NOT NULL,
    player_key  TEXT NOT NULL,          -- initial.surname
    position    TEXT,                   -- G | F | C (ESPN abbreviation)
    starter     INTEGER NOT NULL,
    played      INTEGER NOT NULL,       -- 0 = did not play (inactive / coach's decision / ...)
    dnp_reason  TEXT,
    minutes     REAL,
    pts         INTEGER, reb INTEGER, ast INTEGER, fg3m INTEGER,
    fgm INTEGER, fga INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    oreb INTEGER, dreb INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER, plus_minus INTEGER,
    PRIMARY KEY (game_id, player_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_box_player_key ON box_player(player_key, team);
CREATE INDEX IF NOT EXISTS ix_box_player_game ON box_player(game_key);

CREATE TABLE IF NOT EXISTS prop_lines (
    source      TEXT NOT NULL,          -- kalshi | polymarket
    ticker      TEXT NOT NULL,          -- Kalshi ticker | Polymarket condition_id:outcome_index
    player_key  TEXT NOT NULL,
    player_name TEXT,
    team        TEXT,                   -- player's team when the source carries it (Kalshi does; Polymarket does not)
    game_key    TEXT NOT NULL,
    stat        TEXT NOT NULL,          -- pts | reb | ast | fg3m
    line        REAL NOT NULL,          -- x.5 threshold; P(over) = P(stat >= line + 0.5)
    p_over      REAL,                   -- last pre-tip price of the over / YES side; NULL when none
    t           INTEGER,                -- unix seconds of that price
    result      TEXT,                   -- yes | no | void | NULL (unsettled)
    actual      REAL,                   -- settlement value when the source reports it
    open_time   TEXT,
    close_time  TEXT,
    PRIMARY KEY (source, ticker)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_prop_lines_join ON prop_lines(game_key, player_key, stat);

-- pre-game team lines per game (from the Polymarket ladders in history.db): main total / spread -> implied team points
CREATE TABLE IF NOT EXISTS game_lines (
    source        TEXT NOT NULL,
    game_key      TEXT NOT NULL,
    team          TEXT NOT NULL,
    total_line    REAL,                 -- main full-game total (median of the ladder)
    spread_line   REAL,                 -- this team's handicap (negative = favourite), median of the ladder
    team_pts_line REAL,                 -- (total - spread) / 2
    n_total       INTEGER, n_spread INTEGER,
    PRIMARY KEY (source, game_key, team)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fetch_log (
    kind       TEXT NOT NULL,           -- espn_day | espn_game | kalshi_series | kalshi_candles
    key        TEXT NOT NULL,
    status     TEXT NOT NULL,
    fetched_ms INTEGER NOT NULL,
    note       TEXT,
    PRIMARY KEY (kind, key)
) WITHOUT ROWID;
"""

STATS = ("pts", "reb", "ast", "fg3m")


def now_ms() -> int:
    return int(time.time() * 1000)


class PropsStore:
    def __init__(self, path: str | Path | None = DEFAULT_DB, *, readonly: bool = False):
        self.path = ":memory:" if path is None else str(path)
        if readonly and self.path != ":memory:":
            self.conn = ro_connect(self.path, timeout=5.0)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- writes -----------------------------------------------------------
    def upsert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})")
        with self.conn:
            self.conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        return len(rows)

    def log_fetch(self, kind: str, key: str, status: str, note: str | None = None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)", (kind, key, status, now_ms(), note))

    def fetched(self, kind: str, key: str) -> str | None:
        r = self.conn.execute("SELECT status FROM fetch_log WHERE kind=? AND key=?", (kind, key)).fetchone()
        return None if r is None else r["status"]

    # -- reads ------------------------------------------------------------
    def scalar(self, sql: str, *args) -> Any:
        r = self.conn.execute(sql, args).fetchone()
        return None if r is None else r[0]

    def rows(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def counts(self) -> dict[str, int]:
        return {t: self.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("games", "box_team", "box_player", "prop_lines", "game_lines", "fetch_log")}

    def game_tips(self) -> dict[str, str]:
        """game_key -> tip_utc for every game."""
        return {r["game_key"]: r["tip_utc"] for r in self.rows("SELECT game_key, tip_utc FROM games")}
