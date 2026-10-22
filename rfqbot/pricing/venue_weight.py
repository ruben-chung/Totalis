"""Kalshi vs Polymarket venue weight from the recorded books (claude-sessions/venue-weight-notes.md).

For every exact-matched leg (kalshi_books.db ``leg_matches``) with two-sided quotes on both venues
before kickoff, build a common 1-second grid of mids (last value carried forward; a second is
dropped when either feed's heartbeat is older than the marginal layer's pre-game stale gate,
600 s, or either side has no two-sided quote yet) and measure, per sport:

1. lead-lag: for horizons h in {1, 5, 15, 60} s, regress the next Polymarket mid change on the
   current Kalshi - Polymarket gap (and vice versa) and the gap change on the gap (half-life of
   convergence). OLS with game-clustered sandwich standard errors; the gap coefficient is the
   share of the gap the venue closes within h seconds.
2. noise: variance of the 1-second and 60-second mid changes per venue, half-spread
   distribution, and the precision ratio var_poly / var_kalshi (the marginal layer's prior
   multiplier for Kalshi), with a game-cluster bootstrap interval.
3. the sanity table against the current prior (2.0 for nfl and nba).

Run: ``python -m rfqbot.pricing.venue_weight [--books data/books.db] [--kalshi data/kalshi_books.db]
[--json out.json] [--max-legs N]``. Read-only.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..db import ro_connect
from .metrics import clustered_ols

HORIZONS = (1, 5, 15, 60)
STALE_S = 600.0            # MarginalConfig.stale_warn_pregame_s
TIGHT_HS = 0.02            # "tight" seconds: both half-spreads at or under this (noise ratio without wide books)
PRIOR = {"nfl": 2.0, "nba": 2.0}


def _parse_ts(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if s.endswith("+00"):
        s = s + ":00"
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def heartbeat_ages(conn: sqlite3.Connection, table: str, t0: int, t1: int) -> np.ndarray:
    """Seconds since the feed's newest row, per grid second from t0 (feed-wide, like the marginal layer)."""
    n = (t1 - t0) // 1000 + 1
    last = np.full(n, -1, dtype=np.int64)
    for (ms,) in conn.execute(f"SELECT at_ms FROM {table} WHERE at_ms BETWEEN ? AND ?", (t0, t1)):
        i = (ms - t0) // 1000
        if ms > last[i]:
            last[i] = ms
    last = np.maximum.accumulate(last)
    grid = t0 + np.arange(n) * 1000
    age = np.where(last < 0, np.inf, (grid - last) / 1000.0)
    return age


def lvcf(grid: np.ndarray, at_ms: np.ndarray, mid: np.ndarray) -> np.ndarray:
    """Last two-sided mid at or before each grid second (nan before the first)."""
    idx = np.searchsorted(at_ms, grid, side="right") - 1
    out = np.where(idx >= 0, mid[np.clip(idx, 0, None)], np.nan)
    return out


class Reg:
    """y = a + b x with game-clustered sandwich SEs (``metrics.clustered_ols``), accumulated from
    per-cluster sufficient statistics so millions of seconds never have to be held."""

    def __init__(self) -> None:
        self.clusters: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.n = 0

    def add(self, cluster: str, x: np.ndarray, y: np.ndarray) -> None:
        X = np.column_stack([np.ones_like(x), x])
        xx, xy = X.T @ X, X.T @ y
        self.n += x.shape[0]
        cx, cy = self.clusters.get(cluster, (np.zeros((2, 2)), np.zeros(2)))
        self.clusters[cluster] = (cx + xx, cy + xy)

    def fit(self) -> dict[str, float]:
        G = len(self.clusters)
        beta, V = (None, None) if self.n < 10 else clustered_ols(self.clusters)
        if beta is None:
            return {"b": float("nan"), "se": float("nan"), "n": self.n, "clusters": G}
        return {"b": float(beta[1]), "se": float(math.sqrt(max(V[1, 1], 0.0))), "n": self.n, "clusters": G}


def analyse(books: str, kalshi: str, *, max_legs: int | None = None, stale_s: float = STALE_S, log=print) -> dict[str, Any]:
    t_start = time.perf_counter()
    kb, bb = ro_connect(kalshi), ro_connect(books)
    k0, k1 = kb.execute("SELECT MIN(at_ms), MAX(at_ms) FROM kalshi_top").fetchone()
    b0, b1 = bb.execute("SELECT MIN(at_ms), MAX(at_ms) FROM book_top").fetchone()
    t0, t1 = max(k0, b0), min(k1, b1)
    t0 -= t0 % 1000
    n_grid = (t1 - t0) // 1000 + 1
    grid = t0 + np.arange(n_grid) * 1000
    log(f"overlap {datetime.fromtimestamp(t0 / 1000, timezone.utc):%Y-%m-%d %H:%M}Z .. {datetime.fromtimestamp(t1 / 1000, timezone.utc):%H:%M}Z = {n_grid / 3600:.2f} h")
    age_p = heartbeat_ages(bb, "book_top", t0, t1)
    age_k = heartbeat_ages(kb, "kalshi_top", t0, t1)
    feed_ok = (age_p <= stale_s) & (age_k <= stale_s)
    log(f"feed heartbeat ok on {feed_ok.mean():.1%} of seconds (poly max age {np.max(age_p[np.isfinite(age_p)]):.0f}s, kalshi {np.max(age_k[np.isfinite(age_k)]):.0f}s)")

    legs = kb.execute("SELECT position_id, sport, canonical_key, game_key, kalshi_ticker, kalshi_side FROM leg_matches WHERE confidence='exact'").fetchall()
    if max_legs:
        legs = legs[:max_legs]
    tok = {r["position_id"]: (r["token_id"], _parse_ts(r["game_start_time"])) for r in
           bb.execute("SELECT position_id, token_id, game_start_time FROM leg_tokens")}

    regs: dict[tuple, Reg] = defaultdict(Reg)            # (sport, kind, h) -> Reg
    defaultdict(list)       # (sport, venue, h) -> per-game [sum d2, n]
    spreads: dict[tuple, list] = defaultdict(list)       # (sport, venue) -> half spreads (per leg median)
    per_sport = defaultdict(lambda: {"legs": 0, "legs_used": 0, "seconds": 0, "games": set(), "skipped": defaultdict(int)})
    game_sec: dict[tuple, dict] = defaultdict(dict)      # (sport, venue, h) -> game -> [sum d2, n]
    for i, m in enumerate(legs):
        sp = m["sport"]
        st = per_sport[sp]
        st["legs"] += 1
        token, kickoff = tok.get(m["position_id"], (None, None))
        if token is None:
            st["skipped"]["no_token"] += 1
            continue
        end = t1 if kickoff is None else min(t1, kickoff)
        if end <= t0 + 60_000:
            st["skipped"]["in_play_or_over"] += 1
            continue
        prow = bb.execute("SELECT at_ms, best_bid, best_ask FROM book_top WHERE asset_id=? AND at_ms<=? AND best_bid IS NOT NULL AND best_ask IS NOT NULL ORDER BY at_ms", (token, end)).fetchall()
        krow = kb.execute("SELECT at_ms, yes_bid, yes_ask FROM kalshi_top WHERE ticker=? AND at_ms<=? AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL ORDER BY at_ms", (m["kalshi_ticker"], end)).fetchall()
        if len(prow) < 2 or len(krow) < 2:
            st["skipped"]["one_sided_or_empty"] += 1
            continue
        pa = np.array([r[0] for r in prow], dtype=np.int64)
        pb, pk = np.array([r[1] for r in prow], dtype=float) / 1e4, np.array([r[2] for r in prow], dtype=float) / 1e4
        ka = np.array([r[0] for r in krow], dtype=np.int64)
        kb_, kk = np.array([r[1] for r in krow], dtype=float), np.array([r[2] for r in krow], dtype=float)
        pmid, phs = (pb + pk) / 2, (pk - pb) / 2
        kmid, khs = (kb_ + kk) / 2, (kk - kb_) / 2
        if m["kalshi_side"] == "no":
            kmid = 1.0 - kmid
        n_leg = (end - t0) // 1000 + 1
        g = grid[:n_leg]
        p = lvcf(g, pa, pmid)
        k = lvcf(g, ka, kmid)
        ok = feed_ok[:n_leg] & np.isfinite(p) & np.isfinite(k)
        if ok.sum() < 120:
            st["skipped"]["under_2min_overlap"] += 1
            continue
        st["legs_used"] += 1
        st["seconds"] += int(ok.sum())
        st["games"].add(m["game_key"])
        gk = m["game_key"]
        hs_p, hs_k = lvcf(g, pa, phs), lvcf(g, ka, khs)
        spreads[(sp, "poly")].append(float(np.median(hs_p[ok])))
        spreads[(sp, "kalshi")].append(float(np.median(hs_k[ok])))
        tight = ok & (hs_p <= TIGHT_HS) & (hs_k <= TIGHT_HS)
        st["tight_seconds"] = st.get("tight_seconds", 0) + int(tight.sum())
        gap = k - p
        gap_dm = gap - np.nanmean(np.where(ok, gap, np.nan))        # leg-demeaned gap: deviations from the persistent offset
        for h in HORIZONS:
            if n_leg <= h:
                continue
            valid = ok[:-h] & ok[h:]
            if valid.sum() < 30:
                continue
            dp = (p[h:] - p[:-h])[valid]
            dk = (k[h:] - k[:-h])[valid]
            g0 = gap[:-h][valid]
            regs[(sp, "poly_follows_kalshi", h)].add(gk, g0, dp)
            regs[(sp, "kalshi_follows_poly", h)].add(gk, -g0, dk)
            regs[(sp, "gap_decay", h)].add(gk, g0, (gap[h:] - gap[:-h])[valid])
            g0d = gap_dm[:-h][valid]
            regs[(sp, "poly_follows_kalshi_dm", h)].add(gk, g0d, dp)
            regs[(sp, "kalshi_follows_poly_dm", h)].add(gk, -g0d, dk)
            regs[(sp, "gap_decay_dm", h)].add(gk, g0d, (gap_dm[h:] - gap_dm[:-h])[valid])
            if h in (1, 60):
                tv = tight[:-h] & tight[h:]
                for venue, d, dt in (("poly", dp, (p[h:] - p[:-h])[tv]), ("kalshi", dk, (k[h:] - k[:-h])[tv])):
                    acc = game_sec[(sp, venue, h)].setdefault(gk, [0.0, 0])
                    acc[0] += float(d @ d)
                    acc[1] += int(d.shape[0])
                    acc2 = game_sec[(sp, venue + "_tight", h)].setdefault(gk, [0.0, 0])
                    acc2[0] += float(dt @ dt)
                    acc2[1] += int(dt.shape[0])
        if (i + 1) % 500 == 0:
            log(f"  {i + 1}/{len(legs)} legs, {time.perf_counter() - t_start:.0f}s")

    out: dict[str, Any] = {"window": {"t0": int(t0), "t1": int(t1), "hours": n_grid / 3600}, "stale_s": stale_s, "sports": {}}
    rng = np.random.default_rng(0)
    for sp, st in sorted(per_sport.items()):
        res: dict[str, Any] = {"legs": st["legs"], "legs_used": st["legs_used"], "games": len(st["games"]), "seconds": st["seconds"],
                               "tight_seconds": st.get("tight_seconds", 0), "skipped": dict(st["skipped"]), "prior": PRIOR.get(sp),
                               "leadlag": {}, "noise": {}}
        for h in HORIZONS:
            row = {}
            for kind in ("poly_follows_kalshi", "kalshi_follows_poly", "gap_decay", "poly_follows_kalshi_dm", "kalshi_follows_poly_dm", "gap_decay_dm"):
                row[kind] = regs[(sp, kind, h)].fit() if (sp, kind, h) in regs else None
            for kind, name in (("gap_decay", "half_life_s"), ("gap_decay_dm", "half_life_dm_s")):
                gd = row[kind]
                if gd and np.isfinite(gd["b"]) and -1 < gd["b"] < 0:
                    row[name] = h * math.log(0.5) / math.log(1 + gd["b"])
                else:
                    row[name] = None
            res["leadlag"][h] = row
        for h, suffix in ((1, ""), (60, ""), (1, "_tight"), (60, "_tight")):
            vp, vk = game_sec.get((sp, "poly" + suffix, h), {}), game_sec.get((sp, "kalshi" + suffix, h), {})
            games = sorted(g_ for g_ in set(vp) & set(vk) if vp[g_][1] > 0 and vk[g_][1] > 0)
            if not games:
                continue
            sp_ = np.array([vp[g_] for g_ in games]); sk = np.array([vk[g_] for g_ in games])
            var_p, var_k = sp_[:, 0].sum() / max(sp_[:, 1].sum(), 1), sk[:, 0].sum() / max(sk[:, 1].sum(), 1)
            ratios = []
            for _ in range(400):
                pick = rng.integers(0, len(games), len(games))
                a, b = sp_[pick], sk[pick]
                vk_ = b[:, 0].sum() / max(b[:, 1].sum(), 1)
                if vk_ > 0:
                    ratios.append((a[:, 0].sum() / max(a[:, 1].sum(), 1)) / vk_)
            lo, hi = (float(np.quantile(ratios, 0.025)), float(np.quantile(ratios, 0.975))) if ratios else (float("nan"),) * 2
            res["noise"][f"{h}{suffix}"] = {"var_poly": var_p, "var_kalshi": var_k, "sd_poly": math.sqrt(var_p), "sd_kalshi": math.sqrt(var_k),
                                            "ratio": var_p / var_k if var_k > 0 else float("nan"), "ratio_ci": [lo, hi], "games": len(games),
                                            "seconds": int(sp_[:, 1].sum())}
        for venue in ("poly", "kalshi"):
            hs = spreads.get((sp, venue), [])
            if hs:
                res["noise"][f"half_spread_{venue}"] = {"p25": float(np.quantile(hs, 0.25)), "p50": float(np.median(hs)), "p75": float(np.quantile(hs, 0.75))}
        out["sports"][sp] = res
    out["seconds_total"] = sum(s["seconds"] for s in per_sport.values())
    out["elapsed_s"] = time.perf_counter() - t_start
    return out


def format_report(out: dict[str, Any]) -> str:
    L = [f"window {out['window']['hours']:.2f} h, stale gate {out['stale_s']:.0f} s, {out['elapsed_s']:.0f} s to compute"]
    for sp, r in out["sports"].items():
        L.append(f"\n[{sp}] exact legs {r['legs']}, used {r['legs_used']} in {r['games']} games, {r['seconds']:,} leg-seconds "
                 f"({r['tight_seconds']:,} with both half-spreads <= {TIGHT_HS}); skipped {r['skipped']}")
        L.append("  lead-lag (coefficient = share of the gap closed within h; game-clustered SE; dm = leg-demeaned gap):")
        L.append("   h    poly->kalshi      kalshi->poly      gap decay        half-life | dm: poly->kalshi   kalshi->poly    half-life   n")
        for h, row in r["leadlag"].items():
            def f(x):
                return "   n/a         " if not x or not np.isfinite(x["b"]) else f"{x['b']:+.3f} ± {x['se']:.3f} "
            hl, hld = row["half_life_s"], row["half_life_dm_s"]
            L.append(f"  {h:3d}s  {f(row['poly_follows_kalshi'])} {f(row['kalshi_follows_poly'])} {f(row['gap_decay'])} "
                     f"{'    n/a' if hl is None else f'{hl:7.0f}s'} | {f(row['poly_follows_kalshi_dm'])} {f(row['kalshi_follows_poly_dm'])} "
                     f"{'    n/a' if hld is None else f'{hld:7.0f}s'}  {row['gap_decay']['n'] if row['gap_decay'] else 0:,}")
        for key in ("1", "60", "1_tight", "60_tight"):
            n = r["noise"].get(key)
            if n:
                L.append(f"  noise {key:>8s}: sd poly {n['sd_poly']:.5f} kalshi {n['sd_kalshi']:.5f}; var_poly/var_kalshi = {n['ratio']:.2f} "
                         f"[{n['ratio_ci'][0]:.2f}, {n['ratio_ci'][1]:.2f}] over {n['games']} games, {n['seconds']:,} s")
        for venue in ("poly", "kalshi"):
            s = r["noise"].get(f"half_spread_{venue}")
            if s:
                L.append(f"  half-spread {venue}: p25 {s['p25']:.4f} p50 {s['p50']:.4f} p75 {s['p75']:.4f}")
        L.append(f"  current prior: {r['prior']}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--books", default="data/books.db")
    ap.add_argument("--kalshi", default="data/kalshi_books.db")
    ap.add_argument("--max-legs", type=int, default=None)
    ap.add_argument("--stale-s", type=float, default=STALE_S)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    out = analyse(a.books, a.kalshi, max_legs=a.max_legs, stale_s=a.stale_s, log=lambda s: print(s, file=sys.stderr))
    print(format_report(out))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())
