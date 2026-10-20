"""Share-model fit on synthetic data with known parameters."""
import random

import numpy as np
import pytest

from rfqbot.props.fit import (Row, bucket, cross_stat_teammates, fit_all, fit_stat, implied_line, load_rows, position_group,
                              render_notes, season_shares)
from rfqbot.props.store import PropsStore


def test_implied_line_from_ladder():
    assert implied_line([19.5, 24.5, 29.5], [0.8, 0.5, 0.2]) == 24.5
    assert implied_line([19.5, 24.5, 29.5], [0.9, 0.6, 0.3]) == pytest.approx(24.5 + (0.6 - 0.5) / 0.3 * 5)
    assert implied_line([19.5, 24.5, 29.5], [None, None, None]) == 24.5           # unpriced ladder -> median strike
    assert implied_line([24.5], [None]) == 24.5
    assert implied_line([24.5], [0.6]) == pytest.approx(24.7)                     # one priced strike nudges toward the over
    assert implied_line([9.5, 14.5], [0.95, 0.9]) == pytest.approx(12.0)          # never crosses 0.5 -> median


def test_helpers():
    assert bucket(12.0, (0, 10, 15, 20, 99)) == "10-15" and bucket(45.0, (0, 10, 15, 20, 99)) == "20+" and bucket(0.5, (0, 1, 2, 99)) == "0-1"
    assert position_group("G") == "G" and position_group("PF") == "F" and position_group("C") == "C" and position_group(None) == "?"


def _synthetic(store: PropsStore, *, n_games=300, seed=7, team_corr=0.4, share_sd=0.06):
    """Two 8-man teams per game; stat = s * team_pts + e with e = team factor + noise; a line on half the rows."""
    rng = random.Random(seed)
    teams = ["AAA", "BBB", "CCC", "DDD"]
    players = {t: [(f"{t}{i}", 0.04 + 0.03 * i, "G" if i < 3 else ("F" if i < 6 else "C")) for i in range(8)] for t in teams}
    games, box_t, box_p, lines, glines = [], [], [], [], []
    for g in range(n_games):
        away, home = rng.sample(teams, 2)
        gk = f"nba|2026-{1 + g // 28:02d}-{(g % 28) + 1:02d}|{away}@{home}"
        gid = f"g{g}"
        pts = {away: int(rng.gauss(112, 11)), home: int(rng.gauss(114, 11))}
        games.append({"game_id": gid, "game_key": gk, "season": 2026, "season_type": 2, "date_et": gk.split("|")[1], "tip_utc": "2026-01-01T00:00:00Z",
                      "away": away, "home": home, "away_pts": pts[away], "home_pts": pts[home], "ot_periods": 0, "status": "final", "fetched_ms": 1})
        for team in (away, home):
            opp = home if team == away else away
            box_t.append({"game_id": gid, "game_key": gk, "date_et": gk.split("|")[1], "team": team, "opponent": opp, "home": int(team == home),
                          "points": pts[team], "opp_points": pts[opp], "margin": pts[team] - pts[opp], "ot_periods": 0})
            factor = rng.gauss(0, 1)
            glines.append({"source": "polymarket", "game_key": gk, "team": team, "total_line": 226.0, "spread_line": 0.0, "team_pts_line": 113.0,
                           "n_total": 1, "n_spread": 1})
            for pid, s, pos in players[team]:
                dnp = rng.random() < 0.05
                e = 6.0 * (team_corr ** 0.5 * factor + (1 - team_corr) ** 0.5 * rng.gauss(0, 1))
                val = None if dnp else max(0, round(s * pts[team] + e))
                box_p.append({"game_id": gid, "game_key": gk, "date_et": gk.split("|")[1], "team": team, "opponent": opp, "home": int(team == home),
                              "player_id": pid, "player_name": pid, "player_key": pid.lower(), "position": pos, "starter": int(s > 0.1),
                              "played": int(not dnp), "dnp_reason": "COACH'S DECISION" if dnp else None, "minutes": None if dnp else 30.0,
                              "pts": val, "reb": None if dnp else 5, "ast": None if dnp else 3, "fg3m": None if dnp else 1,
                              "fgm": None, "fga": None, "fg3a": None, "ftm": None, "fta": None, "oreb": None, "dreb": None, "stl": None,
                              "blk": None, "tov": None, "pf": None, "plus_minus": None})
                if g % 2 == 0:
                    line = round((s + rng.gauss(0, share_sd) * 0) * 113.0 * 2) / 2
                    line = line + 0.5 if line == int(line) else line
                    lines.append({"source": "polymarket", "ticker": f"{gid}:{pid}", "player_key": pid.lower(), "player_name": pid, "team": None,
                                  "game_key": gk, "stat": "pts", "line": line, "p_over": 0.5, "t": None, "result": None, "actual": None,
                                  "open_time": None, "close_time": None})
    for table, rows in (("games", games), ("box_team", box_t), ("box_player", box_p), ("prop_lines", lines), ("game_lines", glines)):
        store.upsert(table, rows)


def test_fit_recovers_synthetic_parameters(tmp_path):
    store = PropsStore(tmp_path / "props.db")
    _synthetic(store)
    rows = load_rows(store, (2,))
    assert len(rows) == 300 * 16 and sum(1 for r in rows if "pts" in r.line) == 150 * 16
    assert all(r.team_pts_line == 113.0 for r in rows)
    shares = season_shares(rows, "pts")
    r0 = next(r for r in rows if r.played and (r.player, r.game_key) in shares)
    assert abs(shares[(r0.player, r0.game_key)] - float(r0.player[-1]) * 0.03 - 0.04) < 0.02   # LOO share near the truth

    res = fit_stat(rows, "pts")
    assert res["fitted_rows"] > 4000 and 0.03 < res["dnp_mass_with_line"] < 0.07
    for src in ("line", "season"):
        assert res["by_share_source"][src]["n"] > 1500
        assert 5.0 < res["by_share_source"][src]["sd"] < 7.5                       # residual sd 6 (rounding + share noise)
        assert abs(res["by_share_source"][src]["mean"]) < 0.8
    assert res["share_line_vs_season"]["corr"] > 0.95
    assert res["blowout"]["n_starters"] > 1500 and abs(res["blowout"]["slope_abs_margin"]) < 0.06    # no blowout effect by construction
    assert "0-5" in res["blowout"]["bins_abs_margin"]
    vs = res["vs_game"]
    assert vs["corr_stat_team_pts"] > 0.15 and abs(vs["corr_resid_team_pts"]) < 0.08                   # residual is orthogonal to team points
    mates, opps = res["teammates"], res["opponents"]
    assert 0.28 < mates["all"]["corr"] < 0.52 and abs(opps["all"]["corr"]) < 0.08                     # team factor 0.4, none across teams
    assert set(mates) >= {"G-G", "F-G", "C-G", "F-F", "C-F", "C-C", "all"} and all(0.2 < v["corr"] < 0.6 for v in mates.values())
    assert sorted(res["by_line_bucket"]) and all(v["n"] > 0 for v in res["by_line_bucket"].values())
    assert set(res["by_position_line_bucket"]) == {"G", "F", "C"}
    big = [v for bs in res["by_position_line_bucket"].values() for v in bs.values() if v["n"] >= 200]
    assert big and all(4.0 < v["sd"] < 8.0 for v in big)

    full = fit_all(rows)
    assert full["join"] == {"ambiguous_lines": 0, "players_multi_team": 0}
    assert full["n_games"] == 300 and full["stats"]["pts"]["fitted_rows"] == res["fitted_rows"] and "_z" not in full["stats"]["pts"]
    assert full["dnp"]["dnp_rows"] > 0 and full["stats"]["reb"]["fitted_rows"] > 0
    md = render_notes(full, counts=store.counts())
    assert "## pts" in md and "Teammates residual correlation" in md and "| G-G |" in md and "by position and line bucket" in md
    store.close()


def test_shared_key_lines_are_dropped_and_unlisted_lines_count_as_dnp(tmp_path):
    store = PropsStore(tmp_path / "props.db")
    _synthetic(store, n_games=40)
    # two rostered AAA players share a key in every game: their lines become ambiguous
    store.conn.execute("UPDATE box_player SET player_key='aaa0' WHERE player_id='AAA1'")
    store.conn.execute("UPDATE prop_lines SET player_key='aaa0' WHERE player_key='aaa1'")
    # a line for a player with no roster row in a final game -> synthetic DNP row
    gk = store.scalar("SELECT game_key FROM games LIMIT 1")
    store.upsert("prop_lines", [{"source": "polymarket", "ticker": "x:1", "player_key": "ghost", "player_name": "Ghost", "team": None,
                                 "game_key": gk, "stat": "pts", "line": 20.5, "p_over": None, "t": None, "result": None, "actual": None,
                                 "open_time": None, "close_time": None}])
    store.conn.commit()
    rows = load_rows(store, (2,))
    ghost = [r for r in rows if r.player_key == "ghost"]
    assert len(ghost) == 1 and ghost[0].played == 0 and ghost[0].line == {"pts": 20.5} and ghost[0].position == "?"
    assert all("pts" not in r.line for r in rows if r.player_key == "aaa0")
    res = fit_all(rows, min_minutes=0)
    assert res["join"]["ambiguous_lines"] > 0 and res["dnp"]["lines_without_roster_row"] == 1
    assert res["stats"]["pts"]["dnp_with_line"] >= 1
    store.close()


def test_fit_handles_empty_store(tmp_path):
    store = PropsStore(tmp_path / "props.db")
    res = fit_all(load_rows(store))
    assert res["n_games"] == 0 and res["stats"]["pts"]["fitted_rows"] == 0 and "## pts" in render_notes(res)
    store.close()
