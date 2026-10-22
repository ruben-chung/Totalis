"""Validate the joint engine on resolved games (claude-sessions/engine-validation-brief.md).

For each resolved NFL game with pre-kickoff snapshots at ``offset_min`` (history.db, Data
Engineer): build the GameModel from the snapshot quotes (bid = ask = p, so every leg carries the
thin-book error 0.02), then score a stratified sample of ``hist_pairs`` with the engine's
``p_joint`` (+ shock interval), the snapshot product ``p_a * p_b`` and the realized
``won_a * won_b``. Scores go to ``hist_pair_scores`` in ``data/engine_validation.db`` (a separate
file: history.db is the Data Engineer's), together with a ``validation_games`` row per game.

Metrics (``report``): by family pair and by lift bucket, mean realized minus mean predicted for
the engine and for independence, Brier, log-loss, counts; calibration by predicted decile with
the mean shock interval; single-leg calibration of the snapshot prices (market error on its own).

Legs the engine does not model (quarters, props, HSQ, ...) are counted as unsupported and left
out of the pair sample; pairs with a leg missing a snapshot are left out too.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..db import ro_connect
from ..sports.legspec import canonical_key
from .joint import DEFAULT_SHOCKS, build_game, interval_of
from .joint_nfl import EngineConfig
from .metrics import brier as _brier, family_pair, logloss as _logloss

DEFAULT_HISTORY_DB = Path("./data/history.db")
DEFAULT_OUT_DB = Path("./data/engine_validation.db")
LIFT_BUCKETS = ((0.0, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, float("inf")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_games (
    game_prefix TEXT NOT NULL, offset_min INTEGER NOT NULL, ok INTEGER NOT NULL, error TEXT,
    n_quotes INTEGER, n_legs_supported INTEGER, fitted_fg TEXT, fitted_1h TEXT, ipf_residual_fg REAL, ipf_residual_1h REAL,
    flags TEXT, max_marginal_error REAL, mean_marginal_error REAL, build_s REAL, n_pairs_scored INTEGER, n_pairs_total INTEGER,
    n_pairs_unsupported INTEGER, n_pairs_unpriced INTEGER, run_ms INTEGER NOT NULL,
    PRIMARY KEY (game_prefix, offset_min)
);
CREATE TABLE IF NOT EXISTS hist_pair_scores (
    game_prefix TEXT NOT NULL, offset_min INTEGER NOT NULL, leg_a TEXT NOT NULL, leg_b TEXT NOT NULL,
    family_pair TEXT NOT NULL, p_joint REAL NOT NULL, p_indep REAL NOT NULL, p_leg_a REAL, p_leg_b REAL,
    mkt_a REAL, mkt_b REAL, lo REAL, hi REAL, won REAL NOT NULL, won_a REAL, won_b REAL,
    PRIMARY KEY (game_prefix, offset_min, leg_a, leg_b)
);
CREATE TABLE IF NOT EXISTS hist_leg_scores (
    game_prefix TEXT NOT NULL, offset_min INTEGER NOT NULL, leg TEXT NOT NULL, family TEXT, period TEXT,
    mkt REAL NOT NULL, p_engine REAL, won REAL NOT NULL,
    PRIMARY KEY (game_prefix, offset_min, leg)
);
CREATE INDEX IF NOT EXISTS ix_pair_scores_fam ON hist_pair_scores(offset_min, family_pair);
"""


@dataclass
class GameValidation:
    game_prefix: str
    offset_min: int
    ok: bool = False
    error: str | None = None
    n_quotes: int = 0
    n_legs_supported: int = 0
    fitted_fg: list[str] = field(default_factory=list)
    fitted_1h: list[str] = field(default_factory=list)
    ipf_residual_fg: float | None = None
    ipf_residual_1h: float | None = None
    flags: list[str] = field(default_factory=list)
    max_marginal_error: float | None = None
    mean_marginal_error: float | None = None
    build_s: float = 0.0
    n_pairs_scored: int = 0
    n_pairs_total: int = 0
    n_pairs_unsupported: int = 0
    n_pairs_unpriced: int = 0


class Validator:
    def __init__(self, history_db: str | Path = DEFAULT_HISTORY_DB, out_db: str | Path | None = DEFAULT_OUT_DB, *,
                 max_pairs_per_game: int = 3000, seed: int = 0, config: EngineConfig | None = None,
                 shocks: Iterable = DEFAULT_SHOCKS):
        self.history_db = Path(history_db)
        self.out = sqlite3.connect(":memory:" if out_db is None else str(out_db), isolation_level=None, timeout=30)
        self.out.row_factory = sqlite3.Row
        if out_db is not None:
            Path(out_db).parent.mkdir(parents=True, exist_ok=True)
            self.out.execute("PRAGMA journal_mode=WAL")
        self.out.executescript(SCHEMA)
        self.max_pairs = max_pairs_per_game
        self.rng = np.random.default_rng(seed)
        self.cfg = config or EngineConfig()
        self.shocks = list(shocks)

    # -- data -------------------------------------------------------------------------
    def games(self, limit: int | None = None) -> list[str]:
        from ..history.export import resolved_games
        g = resolved_games(self.history_db, sport="nfl")
        return g[:limit] if limit else g

    def quotes(self, prefix: str, offset: int) -> list[Any]:
        from ..history.export import game_markets_hist
        return game_markets_hist(prefix, offset, history_db=self.history_db, include_unpriced=False)

    def legs(self, prefix: str) -> dict[tuple[str, int], sqlite3.Row]:
        c = ro_connect(self.history_db)
        try:
            rows = c.execute("SELECT * FROM hist_legs WHERE game_prefix=? AND parse_error IS NULL", (prefix,)).fetchall()
        finally:
            c.close()
        return {(r["condition_id"], int(r["outcome_index"])): r for r in rows}

    def pairs(self, prefix: str) -> list[sqlite3.Row]:
        c = ro_connect(self.history_db)
        try:
            return c.execute("SELECT * FROM hist_pairs WHERE game_prefix=?", (prefix,)).fetchall()
        finally:
            c.close()

    # -- one game ---------------------------------------------------------------------
    def validate_game(self, prefix: str, offset: int = 15) -> GameValidation:
        gv = GameValidation(prefix, offset)
        t0 = time.perf_counter()
        quotes = self.quotes(prefix, offset)
        gv.n_quotes = len(quotes)
        if not quotes:
            gv.error = "no snapshot quotes at this offset"
            self._write_game(gv)
            return gv
        try:
            model = build_game(quotes, config=self.cfg)
            for sh in self.shocks:
                model.shocked(sh)
        except Exception as e:
            gv.error = f"build: {type(e).__name__}: {e}"
            gv.build_s = time.perf_counter() - t0
            self._write_game(gv)
            return gv
        gv.build_s = time.perf_counter() - t0
        gv.ok = True
        fg, h1 = model.periods["FG"], model.periods["1H"]
        gv.fitted_fg, gv.fitted_1h = sorted(fg.pmfs), sorted(h1.pmfs)
        gv.ipf_residual_fg = max(fg.ipf_residual.values(), default=0.0)
        gv.ipf_residual_1h = max(h1.ipf_residual.values(), default=0.0)
        gv.flags = list(model.flags)
        checks = [c for c in model.marginal_check() if c.diff is not None]
        if checks:
            gv.max_marginal_error = max(c.diff for c in checks)
            gv.mean_marginal_error = sum(c.diff for c in checks) / len(checks)
        legs = self.legs(prefix)
        # position_id -> (spec, market price, won)
        by_pid: dict[str, tuple[Any, float, float | None]] = {}
        mkt: dict[str, float] = {q.position_id: float(q.best_bid) for q in quotes if q.best_bid is not None}
        spec_by_pid = {l.spec.venue_ids.get("position_id"): l.spec for l in model.legs}
        leg_rows: list[tuple] = []
        for (cond, idx), r in legs.items():
            pid = r["position_id"]
            spec = spec_by_pid.get(pid)
            if spec is None or pid not in mkt:
                continue
            won = None if r["won"] is None else float(r["won"])
            by_pid[pid] = (spec, mkt[pid], won)
            if won is not None:
                pe = model.p_leg(spec)
                leg_rows.append((prefix, offset, canonical_key(spec), spec.family, spec.period, mkt[pid], pe, won))
        gv.n_legs_supported = sum(1 for s, _, _ in by_pid.values() if model.indicator(s) is not None)
        # a re-run replaces the game's rows for this offset (the sample is re-drawn)
        self.out.execute("DELETE FROM hist_leg_scores WHERE game_prefix=? AND offset_min=?", (prefix, offset))
        self.out.execute("DELETE FROM hist_pair_scores WHERE game_prefix=? AND offset_min=?", (prefix, offset))
        self.out.executemany("INSERT OR REPLACE INTO hist_leg_scores VALUES (?,?,?,?,?,?,?,?)", leg_rows)
        # pairs: filter, stratify, score
        pid_of = {k: r["position_id"] for k, r in legs.items()}
        rows = self.pairs(prefix)
        gv.n_pairs_total = len(rows)
        groups: dict[str, list[tuple[str, str, sqlite3.Row]]] = defaultdict(list)
        for pr in rows:
            pa, pb = pid_of.get((pr["cond_a"], int(pr["idx_a"]))), pid_of.get((pr["cond_b"], int(pr["idx_b"])))
            if pa is None or pb is None or pa not in by_pid or pb not in by_pid or pr["won_a"] is None or pr["won_b"] is None:
                gv.n_pairs_unpriced += 1
                continue
            if model.indicator(by_pid[pa][0]) is None or model.indicator(by_pid[pb][0]) is None:
                gv.n_pairs_unsupported += 1
                continue
            groups[family_pair(pr["family_a"], pr["period_a"], pr["family_b"], pr["period_b"])].append((pa, pb, pr))
        sample = self._stratified(groups)
        scored = []
        for pa, pb, pr in sample:
            sa, ma, _ = by_pid[pa]
            sb, mb, _ = by_pid[pb]
            ia, ib = model.indicator(sa), model.indicator(sb)
            p_joint = float((ia * ib).mean(dtype=np.float64))
            vals = [p_joint]
            for sh in self.shocks:
                m2 = model.shocked(sh)
                vals.append(float((m2.indicator(sa) * m2.indicator(sb)).mean(dtype=np.float64)))
            won_a, won_b = float(pr["won_a"]), float(pr["won_b"])
            lo, hi = interval_of(vals)
            scored.append((prefix, offset, canonical_key(sa), canonical_key(sb),
                           family_pair(pr["family_a"], pr["period_a"], pr["family_b"], pr["period_b"]),
                           p_joint, ma * mb, float(ia.mean(dtype=np.float64)), float(ib.mean(dtype=np.float64)), ma, mb,
                           lo, hi, won_a * won_b, won_a, won_b))
        self.out.executemany("INSERT OR REPLACE INTO hist_pair_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", scored)
        gv.n_pairs_scored = len(scored)
        self._write_game(gv)
        return gv

    def _stratified(self, groups: dict[str, list]) -> list:
        total = sum(len(v) for v in groups.values())
        if total <= self.max_pairs:
            return [x for v in groups.values() for x in v]
        out: list = []
        keys = sorted(groups)
        floor_each = max(1, self.max_pairs // (4 * len(keys)))       # every family pair gets a floor
        remaining = self.max_pairs
        alloc: dict[str, int] = {}
        for k in keys:
            alloc[k] = min(len(groups[k]), floor_each)
            remaining -= alloc[k]
        rest_total = sum(len(groups[k]) - alloc[k] for k in keys)
        for k in keys:
            if remaining > 0 and rest_total > 0:
                extra = int(round(remaining * (len(groups[k]) - alloc[k]) / rest_total))
                alloc[k] = min(len(groups[k]), alloc[k] + extra)
        for k in keys:
            v = groups[k]
            idx = self.rng.choice(len(v), size=alloc[k], replace=False) if alloc[k] < len(v) else range(len(v))
            out.extend(v[i] for i in idx)
        return out

    def _write_game(self, gv: GameValidation) -> None:
        d = asdict(gv)
        self.out.execute(
            "INSERT OR REPLACE INTO validation_games VALUES (:game_prefix,:offset_min,:ok,:error,:n_quotes,:n_legs_supported,"
            ":fitted_fg,:fitted_1h,:ipf_residual_fg,:ipf_residual_1h,:flags,:max_marginal_error,:mean_marginal_error,:build_s,"
            ":n_pairs_scored,:n_pairs_total,:n_pairs_unsupported,:n_pairs_unpriced,:run_ms)",
            {**d, "ok": int(gv.ok), "fitted_fg": ",".join(gv.fitted_fg), "fitted_1h": ",".join(gv.fitted_1h),
             "flags": json.dumps(gv.flags), "run_ms": int(time.time() * 1000)})

    # -- all games --------------------------------------------------------------------
    def run(self, offset: int = 15, *, limit: int | None = None, games: list[str] | None = None, log=None) -> list[GameValidation]:
        out = []
        for g in (games or self.games(limit)):
            gv = self.validate_game(g, offset)
            out.append(gv)
            if log:
                log(f"  {g:<28} {'ok ' if gv.ok else 'FAIL'} quotes={gv.n_quotes:<4} fg={','.join(gv.fitted_fg) or '-':<8} 1h={','.join(gv.fitted_1h) or '-':<8} "
                    f"mme={gv.max_marginal_error if gv.max_marginal_error is not None else float('nan'):.3f} pairs={gv.n_pairs_scored}/{gv.n_pairs_total} "
                    f"unsup={gv.n_pairs_unsupported} build={gv.build_s:.2f}s {gv.error or ''}")
        return out

    # -- report -----------------------------------------------------------------------
    def report(self, offset: int = 15) -> dict[str, Any]:
        q = self.out.execute
        games = q("SELECT * FROM validation_games WHERE offset_min=?", (offset,)).fetchall()
        rows = q("SELECT * FROM hist_pair_scores WHERE offset_min=?", (offset,)).fetchall()
        res: dict[str, Any] = {"offset": offset, "games": len(games), "games_ok": sum(r["ok"] for r in games),
                               "failures": [(r["game_prefix"], r["error"]) for r in games if not r["ok"]],
                               "pairs": len(rows)}
        if games:
            oks = [r for r in games if r["ok"]]
            res["mme_median"] = float(np.median([r["max_marginal_error"] for r in oks if r["max_marginal_error"] is not None])) if oks else None
            res["ipf_fg_max"] = max((r["ipf_residual_fg"] or 0) for r in oks) if oks else None
            res["fitted_fg_all4"] = sum(1 for r in oks if r["fitted_fg"] == "A,H,M,T")
            res["flags"] = _count_flags(oks)
        if not rows:
            return res
        pj = np.array([r["p_joint"] for r in rows]); pi = np.array([r["p_indep"] for r in rows]); w = np.array([r["won"] for r in rows])
        lo = np.array([r["lo"] for r in rows]); hi = np.array([r["hi"] for r in rows])
        fam = np.array([r["family_pair"] for r in rows])
        res["overall"] = _metrics(pj, pi, w)
        res["by_family"] = {f: _metrics(pj[fam == f], pi[fam == f], w[fam == f]) for f in sorted(set(fam))}
        lift = np.where(pi > 0, pj / np.maximum(pi, 1e-12), np.inf)
        res["by_lift"] = {}
        for a, b in LIFT_BUCKETS:
            sel = (lift >= a) & (lift < b)
            if sel.any():
                res["by_lift"][f"[{a:g},{b:g})"] = _metrics(pj[sel], pi[sel], w[sel])
        res["calibration"] = _calibration(pj, w, lo, hi)
        res["calibration_indep"] = _calibration(pi, w)
        legs = q("SELECT mkt, p_engine, won FROM hist_leg_scores WHERE offset_min=?", (offset,)).fetchall()
        if legs:
            m = np.array([r["mkt"] for r in legs]); e = np.array([r["p_engine"] if r["p_engine"] is not None else np.nan for r in legs]); lw = np.array([r["won"] for r in legs])
            ok = ~np.isnan(e)
            res["legs"] = {"n": len(legs), "market": _metrics1(m, lw), "engine": _metrics1(e[ok], lw[ok]),
                           "market_calibration": _calibration(m, lw), "n_half": int(np.sum(np.abs(m - 0.5) < 1e-9))}
            # engine-vs-snapshot error with the 0.5 placeholder snapshots (untraded tokens) left out
            real = ok & (np.abs(m - 0.5) > 1e-9)
            d = np.abs(e[real] - m[real])
            res["legs"]["mme_ex_half"] = float(d.max()) if len(d) else None
            res["legs"]["mean_err_ex_half"] = float(d.mean()) if len(d) else None
        return res


def _count_flags(rows) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        for f in json.loads(r["flags"] or "[]"):
            key = f.split(":")[0] if f.startswith(("1H ", "FG ", "halves")) else f
            key = key.replace(" is", "").strip()
            out[key] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _metrics(pj: np.ndarray, pi: np.ndarray, w: np.ndarray) -> dict[str, float]:
    return {"n": int(len(w)), "realized": float(w.mean()) if len(w) else float("nan"),
            "pred_engine": float(pj.mean()) if len(pj) else float("nan"), "pred_indep": float(pi.mean()) if len(pi) else float("nan"),
            "bias_engine": float((w - pj).mean()) if len(w) else float("nan"), "bias_indep": float((w - pi).mean()) if len(w) else float("nan"),
            "brier_engine": _brier(pj, w), "brier_indep": _brier(pi, w),
            "logloss_engine": _logloss(pj, w), "logloss_indep": _logloss(pi, w)}


def _metrics1(p: np.ndarray, w: np.ndarray) -> dict[str, float]:
    return {"n": int(len(w)), "bias": float((w - p).mean()) if len(w) else float("nan"), "brier": _brier(p, w), "logloss": _logloss(p, w)}


def _calibration(p: np.ndarray, w: np.ndarray, lo: np.ndarray | None = None, hi: np.ndarray | None = None,
                 edges=(0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01)) -> list[dict[str, float]]:
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (p >= a) & (p < b)
        if not sel.any():
            continue
        row = {"bucket": f"[{a:.2f},{min(b, 1):.2f})", "n": int(sel.sum()), "pred": float(p[sel].mean()), "realized": float(w[sel].mean())}
        if lo is not None and hi is not None:
            row["lo"] = float(lo[sel].mean()); row["hi"] = float(hi[sel].mean())
            row["inside"] = bool(row["lo"] - 1e-9 <= row["realized"] <= row["hi"] + 1e-9)
        out.append(row)
    return out


def format_report(res: dict[str, Any]) -> str:
    L = [f"offset {res['offset']} min: games {res['games_ok']}/{res['games']} built, pairs scored {res['pairs']}"]
    if res.get("failures"):
        L.append("  failures: " + "; ".join(f"{g}: {e}" for g, e in res["failures"][:10]))
    if res.get("mme_median") is not None:
        L.append(f"  median max|engine-fair| {res['mme_median']:.3f}, IPF residual max {res['ipf_fg_max']:.4f}, games with all four FG ladders {res['fitted_fg_all4']}")
    if res.get("flags"):
        L.append("  flags: " + ", ".join(f"{k} x{v}" for k, v in list(res["flags"].items())[:8]))
    if "overall" not in res:
        return "\n".join(L)

    def mrow(name, m):
        return (f"  {name:<40} {m['n']:>7} {m['realized']:7.3f} {m['pred_engine']:7.3f} {m['pred_indep']:7.3f} "
                f"{m['bias_engine']:+7.3f} {m['bias_indep']:+7.3f} {m['brier_engine']:7.4f} {m['brier_indep']:7.4f} {m['logloss_engine']:7.4f} {m['logloss_indep']:7.4f}")
    hdr = f"  {'':<40} {'n':>7} {'real':>7} {'eng':>7} {'indep':>7} {'bias_e':>7} {'bias_i':>7} {'brier_e':>7} {'brier_i':>7} {'ll_e':>7} {'ll_i':>7}"
    L += ["\nby family pair", hdr, mrow("ALL", res["overall"])]
    for f, m in sorted(res["by_family"].items(), key=lambda kv: -kv[1]["n"]):
        L.append(mrow(f, m))
    L += ["\nby lift bucket (p_joint / p_indep)", hdr]
    for k, m in res["by_lift"].items():
        L.append(mrow(k, m))
    L += ["\ncalibration by predicted bucket (engine; lo/hi = mean shock interval)",
          f"  {'bucket':<14} {'n':>7} {'pred':>7} {'real':>7} {'lo':>7} {'hi':>7} inside"]
    for r in res["calibration"]:
        L.append(f"  {r['bucket']:<14} {r['n']:>7} {r['pred']:7.3f} {r['realized']:7.3f} {r['lo']:7.3f} {r['hi']:7.3f} {'yes' if r['inside'] else 'no'}")
    L += ["\ncalibration by predicted bucket (independence)", f"  {'bucket':<14} {'n':>7} {'pred':>7} {'real':>7}"]
    for r in res["calibration_indep"]:
        L.append(f"  {r['bucket']:<14} {r['n']:>7} {r['pred']:7.3f} {r['realized']:7.3f}")
    if "legs" in res:
        lg = res["legs"]
        L += [f"\nsingle legs ({lg['n']}, {lg['n_half']} priced exactly 0.5): market bias {lg['market']['bias']:+.4f} brier {lg['market']['brier']:.4f} ll {lg['market']['logloss']:.4f} | "
              f"engine marginal bias {lg['engine']['bias']:+.4f} brier {lg['engine']['brier']:.4f} ll {lg['engine']['logloss']:.4f} | "
              f"|engine - snapshot| excl. 0.5s: max {lg['mme_ex_half'] if lg['mme_ex_half'] is not None else float('nan'):.3f} mean {lg['mean_err_ex_half'] if lg['mean_err_ex_half'] is not None else float('nan'):.4f}",
              f"  {'bucket':<14} {'n':>7} {'pred':>7} {'real':>7}"]
        for r in lg["market_calibration"]:
            L.append(f"  {r['bucket']:<14} {r['n']:>7} {r['pred']:7.3f} {r['realized']:7.3f}")
    return "\n".join(L)


def worst_family_pairs(res: dict[str, Any], min_n: int = 200, k: int = 3) -> list[tuple[str, dict]]:
    """Family pairs where the engine's Brier is worst relative to independence (or largest |bias|)."""
    cands = [(f, m) for f, m in res.get("by_family", {}).items() if m["n"] >= min_n]
    return sorted(cands, key=lambda fm: -(fm[1]["brier_engine"] - fm[1]["brier_indep"]))[:k]
