"""Box scores from ESPN's public site API (no key, no auth; verified 2026-10-20).

Why ESPN: stats.nba.com times out and cdn.nba.com answers 403 from this machine, and nba_api
is not installed. ESPN's endpoints are open:
    scoreboard  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD&limit=100
                (one day per call; date ranges answer 400)
    summary     https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={id}
                (~450 KB: header with competitors / linescores / status, boxscore.players with
                 per-athlete stats keyed by `keys`, starter and didNotPlay flags, position)
ESPN season year = the year the season ends (2026 = 2025-26); season.type 1 preseason, 2 regular,
3 postseason, 5 play-in. Team identity: ESPN `team.name` is the nickname, looked up in rfqbot.sports.nba_teams
(ESPN abbreviations differ: GS, SA, NY, NO, UTAH, WSH). Player identity: `nba_players.player_key`
on ESPN's displayName, plus the team. Every response is cached verbatim (gzip) under the raw dir
so reruns and tests never refetch.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

import httpx

from ..sports import nba_players, nba_teams
from .store import PropsStore, now_ms

log = logging.getLogger(__name__)

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
DEFAULT_RAW_DIR = Path("./data/props/raw/espn")
ET = ZoneInfo("America/New_York")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

STATUS = {"STATUS_FINAL": "final", "STATUS_SCHEDULED": "scheduled", "STATUS_IN_PROGRESS": "in_progress",
          "STATUS_POSTPONED": "postponed", "STATUS_CANCELED": "canceled", "STATUS_HALFTIME": "in_progress",
          "STATUS_END_PERIOD": "in_progress", "STATUS_DELAYED": "delayed"}

# ESPN season year -> (first day, last day) to sweep; preseason starts early October, Finals end in June
SEASON_WINDOWS = {2025: ("2024-10-01", "2025-06-30"), 2026: ("2025-10-01", "2026-06-30"), 2027: ("2026-10-01", "2027-06-30")}


class EspnError(RuntimeError):
    pass


def team_code(team: dict[str, Any]) -> str:
    """Canonical code from an ESPN team object (nickname lookup; abbreviations differ between sources)."""
    name = team.get("name")
    if name in nba_teams.BY_NICKNAME:
        return nba_teams.BY_NICKNAME[name].code
    display = team.get("displayName") or ""
    for nick in sorted(nba_teams.BY_NICKNAME, key=len, reverse=True):     # "Trail Blazers" before "Blazers"-like partials
        if display.endswith(nick):
            return nba_teams.BY_NICKNAME[nick].code
    raise EspnError(f"unknown ESPN team {display!r} ({name!r})")


def et_date(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(ET).date().isoformat()


def game_key(iso_utc: str, away: str, home: str) -> str:
    return f"nba|{et_date(iso_utc)}|{away}@{home}"


def _int(s: Any) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _made_att(s: Any) -> tuple[int | None, int | None]:
    if not s or "-" not in str(s):
        return None, None
    a, b = str(s).split("-", 1)
    return _int(a), _int(b)


def _minutes(s: Any) -> float | None:
    if s is None or str(s).strip() in ("", "--"):
        return None
    txt = str(s).strip()
    if ":" in txt:
        m, sec = txt.split(":", 1)
        return (int(m) if m else 0) + (int(sec) if sec else 0) / 60
    return float(txt)


# ------------------------------------------------------------------ parsing
@dataclass(frozen=True)
class EventStub:
    game_id: str
    tip_utc: str
    season: int
    season_type: int
    status: str
    away: str
    home: str

    @property
    def game_key(self) -> str:
        return game_key(self.tip_utc, self.away, self.home)


def parse_scoreboard(doc: dict[str, Any], *, skipped: list[str] | None = None) -> list[EventStub]:
    """Events of one day. Exhibitions against non-NBA teams (Guangzhou, Melbourne United, ...) are skipped,
    their names appended to `skipped`, so one such game never loses the whole day."""
    out = []
    for e in doc.get("events", []):
        comp = e["competitions"][0]
        home = away = None
        try:
            for c in comp["competitors"]:
                if c.get("homeAway") == "home":
                    home = team_code(c["team"])
                else:
                    away = team_code(c["team"])
        except EspnError as err:
            if skipped is not None:
                skipped.append(str(err))
            continue
        if home is None or away is None:
            raise EspnError(f"event {e.get('id')} without home/away")
        out.append(EventStub(str(e["id"]), e["date"], int(e["season"]["year"]), int(e["season"]["type"]),
                             STATUS.get(e["status"]["type"]["name"], e["status"]["type"]["name"].lower()), away, home))
    return out


def parse_summary(doc: dict[str, Any], *, season: int | None = None, season_type: int | None = None,
                  fetched_ms: int | None = None) -> tuple[dict, list[dict], list[dict]]:
    """-> (games row, box_team rows, box_player rows). Non-final games give an empty box."""
    hdr = doc["header"]
    comp = hdr["competitions"][0]
    tip = comp["date"]
    season = season if season is not None else int(hdr["season"]["year"])
    season_type = season_type if season_type is not None else int(hdr["season"]["type"])
    status = STATUS.get(comp["status"]["type"]["name"], comp["status"]["type"]["name"].lower())
    sides: dict[str, dict] = {}
    for c in comp["competitors"]:
        code = team_code(c["team"])
        sides[c["homeAway"]] = {"code": code, "pts": _int(c.get("score")), "n_periods": len(c.get("linescores") or [])}
    home, away = sides["home"], sides["away"]
    gk = game_key(tip, away["code"], home["code"])
    n_periods = max(home["n_periods"], away["n_periods"])
    ot = max(0, n_periods - 4) if n_periods else None
    game = {"game_id": str(hdr["id"]), "game_key": gk, "season": season, "season_type": season_type,
            "date_et": et_date(tip), "tip_utc": tip, "away": away["code"], "home": home["code"],
            "away_pts": away["pts"], "home_pts": home["pts"], "ot_periods": ot, "status": status,
            "fetched_ms": fetched_ms or now_ms()}
    if status != "final" or home["pts"] is None or away["pts"] is None:
        return game, [], []
    teams = []
    for side, opp in ((home, away), (away, home)):
        teams.append({"game_id": game["game_id"], "game_key": gk, "date_et": game["date_et"], "team": side["code"],
                      "opponent": opp["code"], "home": int(side is home), "points": side["pts"], "opp_points": opp["pts"],
                      "margin": side["pts"] - opp["pts"], "ot_periods": ot or 0})
    players = []
    for block in doc.get("boxscore", {}).get("players", []):
        code = team_code(block["team"])
        opp = away["code"] if code == home["code"] else home["code"]
        for stat in block.get("statistics", []):
            keys = stat.get("keys") or []
            for a in stat.get("athletes", []):
                vals = dict(zip(keys, a.get("stats") or []))
                ath = a.get("athlete") or {}
                if not ath.get("id") or not ath.get("displayName"):
                    log.info("game %s: athlete without id/name skipped: %s", game["game_id"], ath.get("shortName"))
                    continue
                dnp = bool(a.get("didNotPlay"))
                fgm, fga = _made_att(vals.get("fieldGoalsMade-fieldGoalsAttempted"))
                f3m, f3a = _made_att(vals.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = _made_att(vals.get("freeThrowsMade-freeThrowsAttempted"))
                pm = vals.get("plusMinus")
                players.append({
                    "game_id": game["game_id"], "game_key": gk, "date_et": game["date_et"], "team": code, "opponent": opp,
                    "home": int(code == home["code"]), "player_id": str(ath["id"]), "player_name": ath["displayName"],
                    "player_key": nba_players.player_key(ath["displayName"]),
                    "position": (ath.get("position") or {}).get("abbreviation"),
                    "starter": int(bool(a.get("starter"))), "played": int(not dnp),
                    "dnp_reason": (a.get("reason") or None) if dnp else None,
                    "minutes": None if dnp else _minutes(vals.get("minutes")),
                    "pts": None if dnp else _int(vals.get("points")), "reb": None if dnp else _int(vals.get("rebounds")),
                    "ast": None if dnp else _int(vals.get("assists")), "fg3m": None if dnp else f3m,
                    "fgm": fgm, "fga": fga, "fg3a": f3a, "ftm": ftm, "fta": fta,
                    "oreb": _int(vals.get("offensiveRebounds")), "dreb": _int(vals.get("defensiveRebounds")),
                    "stl": _int(vals.get("steals")), "blk": _int(vals.get("blocks")), "tov": _int(vals.get("turnovers")),
                    "pf": _int(vals.get("fouls")), "plus_minus": _int(str(pm).replace("+", "")) if pm not in (None, "") else None,
                })
    return game, teams, players


# ------------------------------------------------------------------ client
class EspnClient:
    def __init__(self, *, raw_dir: str | Path = DEFAULT_RAW_DIR, min_gap_s: float = 0.25, timeout: float = 30.0,
                 client: httpx.Client | None = None):
        self.raw_dir = Path(raw_dir)
        self.min_gap_s = min_gap_s
        self._last = 0.0
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.requests = 0

    def close(self) -> None:
        self.client.close()

    def _cache_path(self, kind: str, key: str) -> Path:
        return self.raw_dir / kind / f"{key}.json.gz"

    def get_json(self, kind: str, key: str, url: str, params: dict | None = None, *, refetch: bool = False) -> dict[str, Any]:
        path = self._cache_path(kind, key)
        if path.exists() and not refetch:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        for attempt in range(5):
            gap = self.min_gap_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()
            self.requests += 1
            r = self.client.get(url, params=params)
            if r.status_code == 200:
                path.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    f.write(r.text)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            raise EspnError(f"{url} -> {r.status_code}: {r.text[:200]}")
        raise EspnError(f"{url}: gave up after retries")

    def scoreboard(self, day: date, *, refetch: bool = False, skipped: list[str] | None = None) -> list[EventStub]:
        key = day.strftime("%Y%m%d")
        doc = self.get_json("scoreboard", key, f"{SITE_API}/scoreboard", {"dates": key, "limit": 100}, refetch=refetch)
        return parse_scoreboard(doc, skipped=skipped)

    def summary(self, game_id: str, *, refetch: bool = False) -> dict[str, Any]:
        return self.get_json("summary", game_id, f"{SITE_API}/summary", {"event": game_id}, refetch=refetch)


def days(start: str, end: str) -> Iterator[date]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        yield d
        d += timedelta(days=1)


def pull_box_scores(store: PropsStore, client: EspnClient, *, start: str, end: str, season_types: Iterable[int] = (2, 3),
                    today: date | None = None, refetch_days: bool = False, log_every: int = 50, out=print) -> dict[str, int]:
    """Sweep the scoreboard day by day, then fetch the summary of every final game not yet stored.

    Scoreboard days in the past are cached forever; today's and future days are refetched. A game is
    refetched only when it is not yet `final` in the store.
    """
    today = today or datetime.now(timezone.utc).astimezone(ET).date()
    types = set(season_types)
    n = {"days": 0, "events": 0, "games_fetched": 0, "games_final": 0, "players": 0, "skipped_type": 0}
    stubs: list[EventStub] = []
    for day in days(start, end):
        if day > today:
            break
        n["days"] += 1
        force = refetch_days or day >= today - timedelta(days=1)
        skipped: list[str] = []
        try:
            evs = client.scoreboard(day, refetch=force, skipped=skipped)
        except EspnError as e:
            store.log_fetch("espn_day", day.isoformat(), "error", str(e)[:200])
            out(f"scoreboard {day}: {e}")
            continue
        store.log_fetch("espn_day", day.isoformat(), "ok", f"{len(evs)} events" + (f"; skipped {skipped}" if skipped else ""))
        for ev in evs:
            n["events"] += 1
            if ev.season_type not in types:
                n["skipped_type"] += 1
                continue
            stubs.append(ev)
    have_final = {r["game_id"] for r in store.rows("SELECT game_id FROM games WHERE status='final'")}
    for i, ev in enumerate(stubs, 1):
        if ev.game_id in have_final:
            continue
        if ev.status != "final":
            store.upsert("games", [{"game_id": ev.game_id, "game_key": ev.game_key, "season": ev.season, "season_type": ev.season_type,
                                    "date_et": et_date(ev.tip_utc), "tip_utc": ev.tip_utc, "away": ev.away, "home": ev.home,
                                    "away_pts": None, "home_pts": None, "ot_periods": None, "status": ev.status, "fetched_ms": now_ms()}])
            continue
        try:
            doc = client.summary(ev.game_id)
            game, teams, players = parse_summary(doc, season=ev.season, season_type=ev.season_type)
        except (EspnError, KeyError, ValueError) as e:
            store.log_fetch("espn_game", ev.game_id, "error", str(e)[:200])
            out(f"summary {ev.game_id} {ev.game_key}: {e}")
            continue
        n["games_fetched"] += 1
        store.upsert("games", [game])
        if teams:
            n["games_final"] += 1
            store.upsert("box_team", teams)
            n["players"] += store.upsert("box_player", players)
        store.log_fetch("espn_game", ev.game_id, "ok", f"{len(players)} players")
        if log_every and i % log_every == 0:
            out(f"  {i}/{len(stubs)} games, {client.requests} requests")
    return n
