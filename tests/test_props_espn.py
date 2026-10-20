"""ESPN box-score parsing and the incremental pull against a fake client."""
import gzip
import json
from datetime import date

import pytest

from rfqbot.props.espn import (EspnClient, EspnError, EventStub, game_key, parse_scoreboard, parse_summary, pull_box_scores,
                               team_code)
from rfqbot.props.store import PropsStore


def _team(nick, loc, abbr):
    return {"id": "1", "name": nick, "location": loc, "abbreviation": abbr, "displayName": f"{loc} {nick}"}


def _athlete(aid, name, pos, starter, stats, dnp=False, reason=None):
    a = {"athlete": {"id": aid, "displayName": name, "position": {"abbreviation": pos}}, "starter": starter, "didNotPlay": dnp,
         "stats": stats}
    if reason:
        a["reason"] = reason
    return a


KEYS = ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "freeThrowsMade-freeThrowsAttempted", "rebounds", "assists", "turnovers", "steals", "blocks", "offensiveRebounds",
        "defensiveRebounds", "fouls", "plusMinus"]


def summary_doc(status="STATUS_FINAL", periods=5):
    gs, por = _team("Warriors", "Golden State", "GS"), _team("Trail Blazers", "Portland", "POR")
    comp = {"date": "2025-10-25T02:00Z", "status": {"type": {"name": status}},
            "competitors": [{"homeAway": "home", "team": por, "score": "139", "linescores": [{}] * periods, "winner": True},
                            {"homeAway": "away", "team": gs, "score": "119", "linescores": [{}] * periods, "winner": False}]}
    players = [
        {"team": gs, "statistics": [{"keys": KEYS, "athletes": [
            _athlete("1", "Stephen Curry", "G", True, ["38", "40", "12-25", "6-13", "10-10", "4", "7", "3", "1", "0", "1", "3", "2", "-11"]),
            _athlete("2", "Michael Porter Jr.", "F", False, ["21:30", "11", "4-8", "2-4", "1-1", "5", "1", "0", "0", "1", "2", "3", "4", "+3"]),
            _athlete("3", "Luka Dončić", "G", True, ["--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--"], dnp=True, reason="COACH'S DECISION"),
        ]}]},
        {"team": por, "statistics": [{"keys": KEYS, "athletes": [
            _athlete("4", "Deni Avdija", "F", True, ["36", "30", "10-18", "3-6", "7-8", "8", "6", "2", "1", "1", "2", "6", "3", "+15"]),
        ]}]},
    ]
    return {"header": {"id": "401", "season": {"year": 2026, "type": 2}, "competitions": [comp]}, "boxscore": {"players": players}}


def test_team_code_and_game_key():
    assert team_code(_team("Trail Blazers", "Portland", "POR")) == "POR"
    assert team_code({"displayName": "Golden State Warriors"}) == "GSW"
    with pytest.raises(EspnError):
        team_code(_team("Sonics", "Seattle", "SEA"))
    assert game_key("2025-10-25T02:00Z", "GSW", "POR") == "nba|2025-10-24|GSW@POR"      # 02:00 UTC is the 24th in ET
    assert game_key("2025-10-25T17:00Z", "GSW", "POR") == "nba|2025-10-25|GSW@POR"


def test_parse_summary_rows():
    game, teams, players = parse_summary(summary_doc(), fetched_ms=1)
    assert game == {"game_id": "401", "game_key": "nba|2025-10-24|GSW@POR", "season": 2026, "season_type": 2, "date_et": "2025-10-24",
                    "tip_utc": "2025-10-25T02:00Z", "away": "GSW", "home": "POR", "away_pts": 119, "home_pts": 139, "ot_periods": 1,
                    "status": "final", "fetched_ms": 1}
    assert {(t["team"], t["opponent"], t["home"], t["points"], t["opp_points"], t["margin"], t["ot_periods"]) for t in teams} == {
        ("POR", "GSW", 1, 139, 119, 20, 1), ("GSW", "POR", 0, 119, 139, -20, 1)}
    by = {p["player_id"]: p for p in players}
    c = by["1"]
    assert (c["player_key"], c["team"], c["opponent"], c["home"], c["position"], c["starter"], c["played"]) == ("s.curry", "GSW", "POR", 0, "G", 1, 1)
    assert (c["minutes"], c["pts"], c["reb"], c["ast"], c["fg3m"], c["fgm"], c["fga"], c["fg3a"], c["ftm"], c["fta"], c["plus_minus"]) == (
        38.0, 40, 4, 7, 6, 12, 25, 13, 10, 10, -11)
    assert by["2"]["player_key"] == "m.porter" and by["2"]["minutes"] == 21.5 and by["2"]["plus_minus"] == 3 and by["2"]["starter"] == 0
    d = by["3"]
    assert d["player_key"] == "l.doncic" and d["played"] == 0 and d["dnp_reason"] == "COACH'S DECISION"
    assert d["minutes"] is None and d["pts"] is None and d["reb"] is None
    assert by["4"]["team"] == "POR" and by["4"]["home"] == 1 and by["4"]["pts"] == 30


def test_parse_summary_non_final_has_no_box():
    game, teams, players = parse_summary(summary_doc(status="STATUS_SCHEDULED"))
    assert game["status"] == "scheduled" and teams == [] and players == []
    game, _, _ = parse_summary(summary_doc(periods=4))
    assert game["ot_periods"] == 0


def test_parse_scoreboard():
    doc = {"events": [{"id": "9", "date": "2025-10-24T23:00Z", "season": {"year": 2026, "type": 2}, "status": {"type": {"name": "STATUS_FINAL"}},
                       "competitions": [{"competitors": [{"homeAway": "away", "team": _team("Celtics", "Boston", "BOS")},
                                                         {"homeAway": "home", "team": _team("Knicks", "New York", "NY")}]}]}]}
    [ev] = parse_scoreboard(doc)
    assert ev == EventStub("9", "2025-10-24T23:00Z", 2026, 2, "final", "BOS", "NYK") and ev.game_key == "nba|2025-10-24|BOS@NYK"


class FakeClient(EspnClient):
    """Serves canned docs; counts fetches like the real one."""
    def __init__(self, days, summaries, raw_dir):
        super().__init__(raw_dir=raw_dir, min_gap_s=0)
        self.days, self.summaries, self.fetches = days, summaries, []

    def get_json(self, kind, key, url, params=None, *, refetch=False):
        path = self._cache_path(kind, key)
        if path.exists() and not refetch:
            with gzip.open(path, "rt") as f:
                return json.load(f)
        self.fetches.append((kind, key))
        doc = self.days[key] if kind == "scoreboard" else self.summaries[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(doc, f)
        return doc


def test_pull_is_incremental_and_skips_preseason(tmp_path):
    sb = {"events": [
        {"id": "401", "date": "2025-10-25T02:00Z", "season": {"year": 2026, "type": 2}, "status": {"type": {"name": "STATUS_FINAL"}},
         "competitions": [{"competitors": [{"homeAway": "away", "team": _team("Warriors", "Golden State", "GS")},
                                           {"homeAway": "home", "team": _team("Trail Blazers", "Portland", "POR")}]}]},
        {"id": "402", "date": "2025-10-25T00:00Z", "season": {"year": 2026, "type": 1}, "status": {"type": {"name": "STATUS_FINAL"}},
         "competitions": [{"competitors": [{"homeAway": "away", "team": _team("Celtics", "Boston", "BOS")},
                                           {"homeAway": "home", "team": _team("Knicks", "New York", "NY")}]}]},
        {"id": "403", "date": "2025-10-25T01:00Z", "season": {"year": 2026, "type": 2}, "status": {"type": {"name": "STATUS_SCHEDULED"}},
         "competitions": [{"competitors": [{"homeAway": "away", "team": _team("Heat", "Miami", "MIA")},
                                           {"homeAway": "home", "team": _team("Bulls", "Chicago", "CHI")}]}]},
    ]}
    client = FakeClient({"20251024": sb, "20251025": {"events": []}}, {"401": summary_doc()}, tmp_path / "raw")
    store = PropsStore(tmp_path / "props.db")
    n = pull_box_scores(store, client, start="2025-10-24", end="2025-10-25", season_types=(2, 3), today=date(2025, 11, 1), out=lambda *a: None)
    assert n == {"days": 2, "events": 3, "games_fetched": 1, "games_final": 1, "players": 4, "skipped_type": 1}
    assert store.counts()["games"] == 2 and store.scalar("SELECT status FROM games WHERE game_id='403'") == "scheduled"
    assert client.fetches == [("scoreboard", "20251024"), ("scoreboard", "20251025"), ("summary", "401")]
    assert store.scalar("SELECT SUM(points) FROM box_team") == 258 and store.scalar("SELECT COUNT(*) FROM box_player WHERE played=0") == 1
    # rerun: scoreboard days come from the cache, the final game is not refetched
    client.fetches.clear()
    pull_box_scores(store, client, start="2025-10-24", end="2025-10-25", season_types=(2, 3), today=date(2025, 11, 1), out=lambda *a: None)
    assert client.fetches == []
    assert store.fetched("espn_game", "401") == "ok" and store.fetched("espn_day", "2025-10-25") == "ok"
    store.close()
