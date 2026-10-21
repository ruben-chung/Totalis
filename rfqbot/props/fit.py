"""First fit of the player-prop share model on data/props.db.

Model, per stat (pts, reb, ast, fg3m):   stat = s * team_points + e
  s      the player's share of team points. From the lines when both exist:
         s_line = line_hat / team_pts_line (line_hat = implied median of the player's closing ladder,
         team_pts_line = (total - spread) / 2 from the game's Polymarket ladders); otherwise the
         leave-one-out season share s_season = sum(stat over the player's other games) /
         sum(team points over those games), needing at least MIN_GAMES other games.
  e      residual, played games only. Reported: sd by line bucket, Fisher skew, and the
         did-not-play mass (rows with a line where the player did not play).
Dependence summaries the engine extension needs as defaults:
  blowout     residual vs |final margin| (bins + OLS slope), starters, and vs the signed margin
  teammates   correlation of standardized residuals z = e / sd(line bucket) for pairs of
              teammates in the same game, by position pair (G/F/C) and stat; cross-stat pairs pooled
  opponents   the same for pairs across the two teams (pace)
  vs game     corr(stat, team points), corr(stat, game total), corr(stat, margin), and the
              residual's corr with total / margin
Everything is reported with its sample size; nothing is smoothed.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .store import PropsStore

STATS = ("pts", "reb", "ast", "fg3m")
MIN_GAMES = 5
LINE_BUCKETS = {"pts": (0, 10, 15, 20, 25, 30, 99), "reb": (0, 4, 6, 8, 10, 12, 99), "ast": (0, 2, 4, 6, 8, 10, 99), "fg3m": (0, 1, 2, 3, 4, 99)}
MARGIN_BINS = (0, 5, 10, 15, 20, 30, 200)
SOURCE_ORDER = ("polymarket", "kalshi")


def bucket(x: float, edges: tuple) -> str:
    for lo, hi in zip(edges, edges[1:]):
        if lo <= x < hi:
            return f"{lo}-{hi}" if hi < 99 else f"{lo}+"
    return f"{edges[-2]}+"


def position_group(pos: str | None) -> str:
    p = (pos or "").upper()
    if "C" in p:
        return "C"
    if "G" in p:
        return "G"
    return "F" if p else "?"


def implied_line(strikes: Iterable[float], probs: Iterable[float | None]) -> float:
    """Median of a ladder: the strike where P(over) crosses 0.5 (linear), else the median strike."""
    pts = sorted((float(k), p) for k, p in zip(strikes, probs))
    priced = [(k, p) for k, p in pts if p is not None]
    if len(priced) >= 2:
        for (k0, p0), (k1, p1) in zip(priced, priced[1:]):
            if p0 >= 0.5 >= p1 and p0 != p1:
                return k0 + (p0 - 0.5) / (p0 - p1) * (k1 - k0)
    if len(priced) == 1 and len(pts) == 1:
        k, p = priced[0]
        return k + (p - 0.5) * 2 * 1.0          # one priced strike: shift by up to one unit toward the implied side
    return statistics.median(k for k, _ in pts)


def skewness(x: np.ndarray) -> float | None:
    if len(x) < 3:
        return None
    m, sd = float(x.mean()), float(x.std())
    return None if sd == 0 else float(((x - m) ** 3).mean() / sd ** 3)


def corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3 or a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


@dataclass
class Row:
    game_key: str
    team: str
    opponent: str
    player: str            # team:player_id (unique; player_key can collide within a team, e.g. Jalen / Jaylin Williams)
    player_key: str
    position: str
    starter: int
    played: int
    minutes: float | None
    team_pts: int
    opp_pts: int
    margin: int
    stats: dict[str, int | None]
    line: dict[str, float]            # stat -> implied closing line
    line_source: dict[str, str]
    team_pts_line: float | None


def load_rows(store: PropsStore, season_types: tuple[int, ...] = (2, 3, 5)) -> list[Row]:
    """Roster rows with their lines. Lines in a final game whose player has no roster row at all (ESPN omits
    some inactive players) are added as synthetic did-not-play rows so the DNP mass counts them."""
    types = ",".join(str(int(t)) for t in season_types)
    lines: dict[tuple[str, str, str, str], dict[str, list]] = defaultdict(lambda: {"k": [], "p": []})
    # (game_key, player_key, stat, source) -> ladder; Kalshi rows carry the team, Polymarket rows do not
    for r in store.rows("SELECT game_key, player_key, team, stat, line, p_over, source FROM prop_lines"):
        lines[(r["game_key"], r["player_key"], r["stat"], r["source"])]["k"].append(r["line"])
        lines[(r["game_key"], r["player_key"], r["stat"], r["source"])]["p"].append(r["p_over"])
    tpl = {(r["game_key"], r["team"]): r["team_pts_line"] for r in store.rows("SELECT game_key, team, team_pts_line FROM game_lines")}
    box = store.rows(f"""SELECT p.*, t.points AS team_pts, t.opp_points, t.margin FROM box_player p
                         JOIN box_team t ON t.game_id = p.game_id AND t.team = p.team
                         JOIN games g ON g.game_id = p.game_id WHERE g.season_type IN ({types})""")
    key_count: dict[tuple[str, str], int] = defaultdict(int)       # (game_key, player_key) -> roster rows sharing the key
    for r in box:
        key_count[(r["game_key"], r["player_key"])] += 1
    out, ambiguous = [], set()
    for r in box:
        line, src = {}, {}
        for stat in STATS:
            for source in SOURCE_ORDER:
                lad = lines.get((r["game_key"], r["player_key"], stat, source))
                if lad:
                    if key_count[(r["game_key"], r["player_key"])] > 1:      # two rostered players share the key: the line is nobody's
                        ambiguous.add((r["game_key"], r["player_key"], stat))
                        break
                    line[stat] = implied_line(lad["k"], lad["p"])
                    src[stat] = source
                    break
        out.append(Row(r["game_key"], r["team"], r["opponent"], f"{r['team']}:{r['player_id']}", r["player_key"], position_group(r["position"]),
                       int(r["starter"]), int(r["played"]), r["minutes"], int(r["team_pts"]), int(r["opp_points"]), int(r["margin"]),
                       {s: r[s] for s in STATS}, line, src, tpl.get((r["game_key"], r["team"]))))
    load_rows.ambiguous_lines = len(ambiguous)      # join diagnostics for the report
    load_rows.players_multi_team = _multi_team(box)
    seen = {(r.game_key, r.player_key) for r in out}
    finals = {r["game_key"]: (r["away"], r["home"]) for r in store.rows(f"SELECT game_key, away, home FROM games WHERE status='final' AND season_type IN ({types})")}
    missing: dict[tuple[str, str], dict] = {}
    for (gk, pk, stat, source), lad in lines.items():
        if gk in finals and (gk, pk) not in seen:
            m = missing.setdefault((gk, pk), {"line": {}, "src": {}, "team": None})
            if stat not in m["line"] or SOURCE_ORDER.index(source) < SOURCE_ORDER.index(m["src"][stat]):
                m["line"][stat], m["src"][stat] = implied_line(lad["k"], lad["p"]), source
    for (gk, pk), m in missing.items():
        team = m["team"] or "?"
        out.append(Row(gk, team, "?", f"{team}:{pk}", pk, "?", 0, 0, None, 0, 0, 0, {s: None for s in STATS}, m["line"], m["src"], None))
    return out


def _multi_team(box) -> int:
    teams: dict[str, set[str]] = defaultdict(set)
    for r in box:
        teams[r["player_id"]].add(r["team"])
    return sum(1 for v in teams.values() if len(v) > 1)


def season_shares(rows: list[Row], stat: str) -> dict[tuple[str, str], float]:
    """(player, game_key) -> leave-one-out share over the player's other played games (None if < MIN_GAMES)."""
    by_player: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.played and r.stats.get(stat) is not None:
            by_player[r.player].append(r)
    out = {}
    for player, rs in by_player.items():
        tot_s, tot_t = sum(r.stats[stat] for r in rs), sum(r.team_pts for r in rs)
        if len(rs) - 1 < MIN_GAMES:
            continue
        for r in rs:
            den = tot_t - r.team_pts
            if den > 0:
                out[(player, r.game_key)] = (tot_s - r.stats[stat]) / den
    return out


def _resid_summary(e: np.ndarray) -> dict[str, Any]:
    return {"n": int(len(e)), "mean": float(e.mean()) if len(e) else None, "sd": float(e.std()) if len(e) > 1 else None,
            "skew": skewness(e), "p10": float(np.percentile(e, 10)) if len(e) else None, "p90": float(np.percentile(e, 90)) if len(e) else None}


def fit_stat(rows: list[Row], stat: str, *, min_minutes: float = 0.0) -> dict[str, Any]:
    shares = season_shares(rows, stat)
    recs = []     # per played row with an s
    n_line_rows = n_line_dnp = 0
    for r in rows:
        has_line = stat in r.line
        if has_line:
            n_line_rows += 1
            if not r.played:
                n_line_dnp += 1
        if not r.played or r.stats.get(stat) is None or (r.minutes is not None and r.minutes < min_minutes):
            continue
        s_line = r.line[stat] / r.team_pts_line if has_line and r.team_pts_line else None
        s_season = shares.get((r.player, r.game_key))
        s = s_line if s_line is not None else s_season
        if s is None:
            continue
        expected = r.line[stat] if has_line else s * r.team_pts   # bucket key: the line, or the model's own expectation
        recs.append((r, s, "line" if s_line is not None else "season", r.stats[stat] - s * r.team_pts, expected, s_line, s_season))
    res: dict[str, Any] = {"stat": stat, "rows_with_line": n_line_rows, "dnp_with_line": n_line_dnp,
                           "dnp_mass_with_line": (n_line_dnp / n_line_rows) if n_line_rows else None,
                           "fitted_rows": len(recs), "by_share_source": {}, "by_line_bucket": {}, "by_position_line_bucket": {}, "blowout": {},
                           "vs_game": {}, "teammates": {}, "opponents": {}}
    if not recs:
        return res
    e_all = np.array([x[3] for x in recs])
    for src in ("line", "season"):
        sub = np.array([x[3] for x in recs if x[2] == src])
        res["by_share_source"][src] = _resid_summary(sub)
    both = [(x[5], x[6]) for x in recs if x[5] is not None and x[6] is not None]
    if len(both) > 10:
        a, b = np.array([x[0] for x in both]), np.array([x[1] for x in both])
        res["share_line_vs_season"] = {"n": len(both), "corr": corr(a, b), "mean_line": float(a.mean()), "mean_season": float(b.mean())}
    # residual sd by line bucket (the engine's sd(line) default); pooled over share sources
    edges = LINE_BUCKETS[stat]
    by_b: dict[str, list[float]] = defaultdict(list)
    for x in recs:
        by_b[bucket(x[4], edges)].append(x[3])
    for b, v in sorted(by_b.items(), key=lambda kv: edges.index(int(kv[0].split("-")[0].rstrip("+")))):
        res["by_line_bucket"][b] = _resid_summary(np.array(v))
    sd_of = {b: (v["sd"] or 1.0) for b, v in res["by_line_bucket"].items()}
    # the same table split by position (G / F / C): sd(line, position) for the extension
    by_pb: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for x in recs:
        by_pb[x[0].position][bucket(x[4], edges)].append(x[3])
    res["by_position_line_bucket"] = {
        pos: {b: _resid_summary(np.array(v)) for b, v in sorted(bs.items(), key=lambda kv: edges.index(int(kv[0].split("-")[0].rstrip("+")))) if len(v) >= 20}
        for pos, bs in sorted(by_pb.items()) if pos in ("G", "F", "C")}
    # blowout: starters, residual vs |margin| and signed margin
    st = [x for x in recs if x[0].starter]
    if len(st) > 20:
        e, am, m = (np.array([x[3] for x in st]), np.array([abs(x[0].margin) for x in st], dtype=float),
                    np.array([x[0].margin for x in st], dtype=float))
        bins = {}
        for lo, hi in zip(MARGIN_BINS, MARGIN_BINS[1:]):
            sel = (am >= lo) & (am < hi)
            if sel.sum() >= 5:
                bins[f"{lo}-{hi}" if hi < 200 else f"{lo}+"] = {"n": int(sel.sum()), "mean_e": float(e[sel].mean()), "sd_e": float(e[sel].std())}
        res["blowout"] = {"n_starters": len(st), "bins_abs_margin": bins,
                          "slope_abs_margin": float(np.polyfit(am, e, 1)[0]), "slope_signed_margin": float(np.polyfit(m, e, 1)[0]),
                          "corr_abs_margin": corr(am, e), "corr_signed_margin": corr(m, e)}
    # vs the game: raw stat and residual against team points, total, margin
    v = np.array([x[0].stats[stat] for x in recs], dtype=float)
    tp = np.array([x[0].team_pts for x in recs], dtype=float)
    tot = np.array([x[0].team_pts + x[0].opp_pts for x in recs], dtype=float)
    mg = np.array([x[0].margin for x in recs], dtype=float)
    res["vs_game"] = {"n": len(recs), "corr_stat_team_pts": corr(v, tp), "corr_stat_total": corr(v, tot), "corr_stat_margin": corr(v, mg),
                      "corr_resid_total": corr(e_all, tot), "corr_resid_margin": corr(e_all, mg), "corr_resid_team_pts": corr(e_all, tp),
                      "corr_resid_opp_pts": corr(e_all, np.array([x[0].opp_pts for x in recs], dtype=float))}
    # standardized residuals -> teammate / opponent pair correlations by position pair
    z = {(x[0].game_key, x[0].player): x[3] / sd_of[bucket(x[4], edges)] for x in recs}
    pos = {(x[0].game_key, x[0].player): x[0].position for x in recs}
    by_game_team: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for x in recs:
        by_game_team[(x[0].game_key, x[0].team)].append((x[0].game_key, x[0].player))
    mates: dict[str, list[tuple[float, float]]] = defaultdict(list)
    opps: dict[str, list[tuple[float, float]]] = defaultdict(list)
    games: dict[str, list[str]] = defaultdict(list)
    for (gk, team) in by_game_team:
        games[gk].append(team)
    for (gk, team), members in by_game_team.items():
        for a, b in combinations(members, 2):
            pair = "-".join(sorted((pos[a], pos[b])))
            mates[pair].append((z[a], z[b]))
            mates["all"].append((z[a], z[b]))
    for gk, teams in games.items():
        if len(teams) != 2:
            continue
        for a in by_game_team[(gk, teams[0])]:
            for b in by_game_team[(gk, teams[1])]:
                pair = "-".join(sorted((pos[a], pos[b])))
                opps[pair].append((z[a], z[b]))
                opps["all"].append((z[a], z[b]))

    def _pairs(d):
        out = {}
        for k, v in d.items():
            if len(v) >= 30:
                a, b = np.array([p[0] for p in v]), np.array([p[1] for p in v])
                # symmetric pair: use both orderings so the estimate does not depend on member order
                out[k] = {"n_pairs": len(v), "corr": corr(np.concatenate([a, b]), np.concatenate([b, a]))}
        return dict(sorted(out.items()))
    res["teammates"], res["opponents"] = _pairs(mates), _pairs(opps)
    res["_z"] = z     # for the cross-stat matrix (stripped before output)
    return res


def cross_stat_teammates(per_stat: dict[str, dict], rows: list[Row]) -> dict[str, dict[str, Any]]:
    """corr(z_stat_a of player i, z_stat_b of teammate j) pooled over position; and same-player cross-stat."""
    out: dict[str, dict[str, Any]] = {"teammate": {}, "same_player": {}}
    members: dict[tuple[str, str], list[str]] = defaultdict(list)
    seen = set()
    for r in rows:
        if r.played and (r.game_key, r.player) not in seen:
            seen.add((r.game_key, r.player))
            members[(r.game_key, r.team)].append(r.player)
    for a, b in combinations(STATS, 2):
        za, zb = per_stat[a].get("_z", {}), per_stat[b].get("_z", {})
        same = [(za[k], zb[k]) for k in za if k in zb]
        if len(same) >= 30:
            x, y = np.array([p[0] for p in same]), np.array([p[1] for p in same])
            out["same_player"][f"{a}-{b}"] = {"n": len(same), "corr": corr(x, y)}
        pairs = []
        for (gk, team), ps in members.items():
            for i in ps:
                for j in ps:
                    if i != j and (gk, i) in za and (gk, j) in zb:
                        pairs.append((za[(gk, i)], zb[(gk, j)]))
        if len(pairs) >= 30:
            x, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
            out["teammate"][f"{a}-{b}"] = {"n_pairs": len(pairs), "corr": corr(x, y)}
    return out


def dnp_summary(rows: list[Row]) -> dict[str, Any]:
    n = len(rows)
    dnp = [r for r in rows if not r.played]
    unlisted = sum(1 for r in dnp if r.position == "?")
    return {"roster_rows": n - unlisted, "dnp_rows": len(dnp) - unlisted, "dnp_share_of_roster": ((len(dnp) - unlisted) / (n - unlisted)) if n - unlisted else None,
            "lines_without_roster_row": unlisted}


def fit_all(rows: list[Row], *, min_minutes: float = 0.0) -> dict[str, Any]:
    games = {r.game_key for r in rows}
    per_stat = {s: fit_stat(rows, s, min_minutes=min_minutes) for s in STATS}
    cross = cross_stat_teammates(per_stat, rows)
    for s in per_stat:
        per_stat[s].pop("_z", None)
    return {"generated": date.today().isoformat(), "model": "stat = s * team_points + e", "n_games": len(games),
            "join": {"ambiguous_lines": getattr(load_rows, "ambiguous_lines", 0), "players_multi_team": getattr(load_rows, "players_multi_team", 0)},
            "n_player_games": len(rows), "n_played": sum(1 for r in rows if r.played),
            "n_with_team_pts_line": sum(1 for r in rows if r.team_pts_line is not None),
            "min_minutes": min_minutes, "dnp": dnp_summary(rows), "stats": per_stat, "cross_stat": cross}


# ------------------------------------------------------------------ report
FINDINGS = """## Findings (first run, 2026-10-20)

* **Sources.** stats.nba.com times out and cdn.nba.com answers 403 from this machine, so box scores come
  from ESPN's public site API (one scoreboard call per day, one summary per game; raw JSON cached under
  `data/props/raw/espn`). Full 2025-26: 1,231 regular-season, 6 play-in and 81 playoff games, plus the
  2026-27 preseason and opening night; 4 games answer 404 on ESPN. Team points equal the sum of player
  points in every game. ESPN lists inactive and coach's-decision players as did-not-play rows but omits
  some injured players entirely, which is why lined player-games without a roster row count as DNP.
* **Lines.** Polymarket props exist only from 2025-12-07 (none in Oct-Nov 2025), one line per player-stat
  (a second line on ~5%); they come from the Data Engineer's `history.db` (read-only, one writer). Rows in
  the older "X: Points Over 13.5" title grammar (Dec-Apr) are recovered from the slug. Pre-tip prices
  (`hist_prices`, 15-min offset) were still being pulled when this ran, so `priced` is 0 for Polymarket
  and the share comes from the line itself; the implied-median logic kicks in once prices exist. Kalshi
  KXNBAPTS/REB/AST/3PT settled markets are public with the actual stat in `expiration_value`, but the
  series only started 2026-10-06, so they cover a few hundred preseason / opening-night player-games.
  Polymarket has no threes ladder (20 markets all season), so `fg3m` is season-share only.
* **Share model.** `s = line / team_pts_line` and the leave-one-out season share agree (corr 0.86-0.88,
  same means), so the line is a fair share proxy. Residuals are right-skewed at low lines (median line
  below the mean) and widen with the line: points sd 5.1 at lines under 10 to 8.3 at 30+, rebounds 2.2
  to 4.2, assists 1.3 to 3.4. Lined rows show a positive mean residual (+0.5 pts) because the line is a
  median of a skewed stat; the engine should centre on the mean, not the line.
* **Blowouts.** Starters' points residual falls from about +1.2 in games decided by 5 or less to -0.9
  at 30+ margins (slope -0.07 pts per point of |margin|); rebounds -0.03 per point; assists flat.
  The effect is symmetric in the sign of the margin (winning big and losing big both cut minutes).
* **Dependence.** Teammate residuals are nearly independent for points (-0.02; centres -0.10 with each
  other) and assists (0.00), slightly positive for rebounds (+0.04). Opponent-pair residuals are
  positive everywhere (points +0.02, rebounds +0.05, centre-centre +0.11): pace. Rebound residuals are
  negatively tied to the game total (-0.16: made shots are not rebounded) and to opponent points;
  points and assists residuals are orthogonal to the total and margin by construction of the share.
  These are the defaults the extension should start from; the share already carries the player-vs-team
  dependence (corr(stat, team pts) 0.13 for points).
* **Join problems.** (1) Name key collisions on one roster (`j.williams` on OKC: Jalen and Jaylin
  Williams): the fit keys players by ESPN id and drops lines whose key matches two rostered players in
  that game (`join.ambiguous_lines`). (2) Mid-season trades: the leave-one-out share is per (team, player)
  and restarts after a trade (`join.players_multi_team`). (3) Rescheduled games: Polymarket slugs carry the
  original date; props for a game moved to another day do not join (counted under lines whose game is not
  final). (4) ESPN exhibition games against non-NBA teams (Guangzhou, Melbourne United) are skipped.
"""


def _f(x, nd=3):
    return "-" if x is None else f"{x:.{nd}f}"


def render_notes(res: dict[str, Any], *, counts: dict[str, int] | None = None, coverage: list[tuple] | None = None) -> str:
    L = [f"# NBA player-prop share model: first fit ({res['generated']})", "",
         "Built by `python -m rfqbot.props fit` on `data/props.db` (box scores: ESPN public API; lines: Polymarket "
         "closing ladders from `data/history.db`, Kalshi settled props). Model `stat = s * team_points + e` with `s` from "
         "the line when a line and a team-points line exist (`line`), else the leave-one-out season share (`season`). "
         "Residual sds are by line bucket; pair correlations are on standardized residuals `z = e / sd(bucket)`. "
         "Parameters: `rfqbot/props/props_fit.json`.", ""]
    if counts:
        L += ["## Dataset", "", "| table | rows |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in counts.items()] + [""]
    if coverage:
        L += ["Lines per source / stat (rows, with a pre-tip price, settled, joined to a box row):", "", "| source | stat | lines | priced | settled | joined |", "|---|---|---|---|---|---|"]
        L += [f"| {a} | {b} | {c:,} | {d:,} | {e:,} | {f:,} |" for a, b, c, d, e, f in coverage] + [""]
    L += FINDINGS.splitlines() + [""]
    j = res.get("join") or {}
    L += [f"Join diagnostics: {j.get('ambiguous_lines', 0):,} lines dropped for a key shared by two rostered players; "
          f"{j.get('players_multi_team', 0):,} players seen on more than one team.", ""]
    L += [f"Fit sample: {res['n_games']:,} games, {res['n_player_games'] - res['dnp'].get('lines_without_roster_row', 0):,} roster rows, {res['n_played']:,} played, "
          f"{res['n_with_team_pts_line']:,} rows with a team-points line; min minutes {res['min_minutes']}.", ""]
    d = res["dnp"]
    L += [f"Did-not-play mass on the roster: {d['dnp_rows']:,} / {d['roster_rows']:,} = {_f(d['dnp_share_of_roster'])} "
          f"(ESPN lists inactive and coach's-decision players); a further {d.get('lines_without_roster_row', 0):,} lined player-games "
          "had no roster row at all (player out, not listed) and count as did-not-play below.", ""]
    for s, r in res["stats"].items():
        L += [f"## {s}", "", f"Rows with a line {r['rows_with_line']:,}, of which did not play {r['dnp_with_line']:,} "
              f"(DNP mass {_f(r['dnp_mass_with_line'])}); fitted rows {r['fitted_rows']:,}.", ""]
        if r["by_share_source"]:
            L += ["| share source | n | mean e | sd e | skew | p10 | p90 |", "|---|---|---|---|---|---|---|"]
            L += [f"| {k} | {v['n']:,} | {_f(v['mean'], 2)} | {_f(v['sd'], 2)} | {_f(v['skew'], 2)} | {_f(v['p10'], 1)} | {_f(v['p90'], 1)} |"
                  for k, v in r["by_share_source"].items()] + [""]
        if r.get("share_line_vs_season"):
            x = r["share_line_vs_season"]
            L += [f"Line share vs season share on {x['n']:,} rows: corr {_f(x['corr'])}, means {_f(x['mean_line'])} / {_f(x['mean_season'])}.", ""]
        if r["by_line_bucket"]:
            L += ["Residual by line bucket (sd is the engine's default sd(line)):", "", "| line | n | mean e | sd e | skew |", "|---|---|---|---|---|"]
            L += [f"| {k} | {v['n']:,} | {_f(v['mean'], 2)} | {_f(v['sd'], 2)} | {_f(v['skew'], 2)} |" for k, v in r["by_line_bucket"].items()] + [""]
        pb = r.get("by_position_line_bucket") or {}
        if pb:
            buckets = list(r["by_line_bucket"])
            L += ["Residual sd by position and line bucket (n in brackets):", "", "| position | " + " | ".join(buckets) + " |", "|---|" + "---|" * len(buckets)]
            for pos, bs in pb.items():
                L.append(f"| {pos} | " + " | ".join(f"{_f(bs[b]['sd'], 2)} ({bs[b]['n']:,})" if b in bs else "-" for b in buckets) + " |")
            L.append("")
        b = r.get("blowout") or {}
        if b:
            L += [f"Blowout (starters, n {b['n_starters']:,}): slope of e on |margin| {_f(b['slope_abs_margin'], 4)}/pt "
                  f"(corr {_f(b['corr_abs_margin'])}), on signed margin {_f(b['slope_signed_margin'], 4)}/pt (corr {_f(b['corr_signed_margin'])}).", "",
                  "| abs margin | n | mean e | sd e |", "|---|---|---|---|"]
            L += [f"| {k} | {v['n']:,} | {_f(v['mean_e'], 2)} | {_f(v['sd_e'], 2)} |" for k, v in b["bins_abs_margin"].items()] + [""]
        g = r.get("vs_game") or {}
        if g:
            L += [f"Vs the game (n {g['n']:,}): corr(stat, team pts) {_f(g['corr_stat_team_pts'])}, corr(stat, total) {_f(g['corr_stat_total'])}, "
                  f"corr(stat, margin) {_f(g['corr_stat_margin'])}; residual: corr(e, total) {_f(g['corr_resid_total'])}, "
                  f"corr(e, margin) {_f(g['corr_resid_margin'])}, corr(e, opp pts) {_f(g['corr_resid_opp_pts'])}.", ""]
        for name in ("teammates", "opponents"):
            t = r.get(name) or {}
            if t:
                L += [f"{name.title()} residual correlation by position pair:", "", "| pair | n pairs | corr |", "|---|---|---|"]
                L += [f"| {k} | {v['n_pairs']:,} | {_f(v['corr'])} |" for k, v in t.items()] + [""]
    c = res.get("cross_stat") or {}
    if c.get("teammate") or c.get("same_player"):
        L += ["## Cross-stat", "", "Same player (z of one stat vs z of another, same game) and teammate pairs (stat a of i vs stat b of j):", "",
              "| stats | same player n / corr | teammate pairs n / corr |", "|---|---|---|"]
        keys = sorted(set(c.get("same_player", {})) | set(c.get("teammate", {})))
        for k in keys:
            sp, tm = c.get("same_player", {}).get(k), c.get("teammate", {}).get(k)
            L.append(f"| {k} | {(str(sp['n']) + ' / ' + _f(sp['corr'])) if sp else '-'} | {(str(tm['n_pairs']) + ' / ' + _f(tm['corr'])) if tm else '-'} |")
        L.append("")
    return "\n".join(L)


def run_fit(store: PropsStore, *, out_json: str | Path, out_md: str | Path, season_types: tuple[int, ...] = (2, 3, 5),
            min_minutes: float = 0.0, out=print) -> dict[str, Any]:
    rows = load_rows(store, season_types)
    res = fit_all(rows, min_minutes=min_minutes)
    coverage = [tuple(r) for r in store.rows("""SELECT l.source, l.stat, COUNT(*), SUM(l.p_over IS NOT NULL), SUM(l.result IN ('yes','no')),
                                                   SUM(b.player_id IS NOT NULL) FROM prop_lines l
                                                   LEFT JOIN box_player b ON b.game_key=l.game_key AND b.player_key=l.player_key AND (l.team IS NULL OR b.team=l.team)
                                                   GROUP BY 1,2 ORDER BY 1,2""")]
    res["line_coverage"] = [{"source": a, "stat": b, "lines": c, "priced": d, "settled": e, "joined": f} for a, b, c, d, e, f in coverage]
    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(res, indent=1, sort_keys=True))
    Path(out_md).write_text(render_notes(res, counts=store.counts(), coverage=coverage))
    for s, r in res["stats"].items():
        src = r["by_share_source"]
        out(f"{s}: fitted {r['fitted_rows']:,}  sd(line) {_f(src.get('line', {}).get('sd'), 2)}  sd(season) {_f(src.get('season', {}).get('sd'), 2)}  "
            f"dnp {_f(r['dnp_mass_with_line'])}  mates {_f((r['teammates'].get('all') or {}).get('corr'))}  opps {_f((r['opponents'].get('all') or {}).get('corr'))}  "
            f"e~total {_f((r['vs_game'] or {}).get('corr_resid_total'))}  e~margin {_f((r['vs_game'] or {}).get('corr_resid_margin'))}")
    out(f"wrote {out_json} and {out_md}")
    return res
