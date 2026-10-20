"""Calibration layer on the engine's joint probability, measured per market-type pair.

    p_cal = p_indep + lambda_pair * (p_joint - p_indep)
    then tail maps: p < lo_cut -> a_lo + b_lo p,   p > hi_cut -> a_hi + b_hi p   (clipped to [0, 1])

``p_indep`` is the product of the engine's own leg marginals (what quote assembly has at run
time). The unit of measurement is the **family pair**: ``family/period x family/period`` of the
two legs, sorted (e.g. ``exact_margin/FG x spread/FG``, ``moneyline/1H x total/FG``). Tails are
thin per pair, so everything is fitted hierarchically (empirical Bayes):

    value_used = w * value_pair + (1 - w) * value_group,   w = n / (n + n0)

with the group one of ``within_FG`` / ``within_half`` / ``cross`` (both legs full game / both in
the same half / different periods), ``n`` the pair's sample (tail observations for the tail
maps) and ``n0`` chosen by 5-fold cross-validation by game on out-of-sample log-loss. A pair
with no data uses the group value (``calibration_level = "group"``); without a calibration file
the engine runs uncalibrated (``"none"``). Each pair also gets a residual model error (RMS over
predicted buckets of realized minus predicted, out of sample, group fallback when thin) that
``JointQuote`` adds to its interval.

Parameters live in ``rfqbot/pricing/calibration.json`` (``Calibration.load``), keyed by family
pair with the group fallbacks included.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_PATH = Path(__file__).with_name("calibration.json")
GROUPS = ("within_FG", "within_half", "cross")
BUCKET_EDGES = (0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.01)
N0_GRID = (100, 300, 1000, 3000, 10000, 30000)
N0_TAIL_GRID = (30, 100, 300, 1000, 3000)
MIN_PAIR_FIT = 20            # pairs with fewer rows contribute nothing of their own (w ~ 0 anyway)


def group_of(period_a: str, period_b: str) -> str:
    if period_a == period_b == "FG":
        return "within_FG"
    if period_a == period_b:
        return "within_half"
    return "cross"


def family_pair_key(family_a: str, period_a: str, family_b: str, period_b: str) -> str:
    return " x ".join(sorted((f"{family_a}/{period_a}", f"{family_b}/{period_b}")))


def group_of_family_pair(family_pair: str) -> str:
    a, b = family_pair.split(" x ")
    return group_of(a.split("/")[1], b.split("/")[1])


@dataclass
class TailMap:
    cut: float
    a: float
    b: float

    def apply(self, p: float) -> float:
        return self.a + self.b * p

    def as_dict(self) -> dict[str, float]:
        return {"cut": self.cut, "a": self.a, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> "TailMap":
        return cls(float(d["cut"]), float(d["a"]), float(d["b"]))


@dataclass
class PairParams:
    lam: float
    lo: TailMap | None
    hi: TailMap | None
    resid: float
    n: int = 0
    n_lo: int = 0
    n_hi: int = 0
    lam_pair: float | None = None      # unshrunk estimates, for the report
    lam_group: float | None = None
    group: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"lambda": self.lam, "lo": self.lo.as_dict() if self.lo else None, "hi": self.hi.as_dict() if self.hi else None,
                "resid": self.resid, "n": self.n, "n_lo": self.n_lo, "n_hi": self.n_hi, "lambda_pair": self.lam_pair,
                "lambda_group": self.lam_group, "group": self.group}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PairParams":
        return cls(lam=float(d["lambda"]), lo=TailMap.from_dict(d["lo"]) if d.get("lo") else None,
                   hi=TailMap.from_dict(d["hi"]) if d.get("hi") else None, resid=float(d.get("resid", 0.0)),
                   n=int(d.get("n", 0)), n_lo=int(d.get("n_lo", 0)), n_hi=int(d.get("n_hi", 0)),
                   lam_pair=d.get("lambda_pair"), lam_group=d.get("lambda_group"), group=d.get("group", ""))


def _apply_params(pp: PairParams, p_joint: float, p_indep: float) -> float:
    p = p_indep + pp.lam * (p_joint - p_indep)
    if pp.lo is not None and p < pp.lo.cut:
        p = pp.lo.apply(p)
    if pp.hi is not None and p > pp.hi.cut:
        p = pp.hi.apply(p)
    return float(min(max(p, 0.0), 1.0))


@dataclass
class Calibration:
    pairs: dict[str, PairParams] = field(default_factory=dict)
    groups: dict[str, PairParams] = field(default_factory=dict)
    n0: int | None = None
    n0_tail: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    identity: bool = True

    def lookup(self, family_pair: str | None, group: str) -> tuple[PairParams | None, str]:
        """(params, level) with level pair | group | none."""
        if self.identity:
            return None, "none"
        if family_pair is not None and family_pair in self.pairs:
            return self.pairs[family_pair], "pair"
        if group in self.groups:
            return self.groups[group], "group"
        return None, "none"

    def apply(self, family_pair: str | None, group: str, p_joint: float, p_indep: float) -> tuple[float, str]:
        pp, level = self.lookup(family_pair, group)
        if pp is None:
            return float(min(max(p_joint, 0.0), 1.0)), level
        return _apply_params(pp, p_joint, p_indep), level

    def model_error(self, family_pair: str | None, group: str) -> float:
        pp, _ = self.lookup(family_pair, group)
        return 0.0 if pp is None else float(pp.resid)

    def to_json(self) -> str:
        return json.dumps({"pairs": {k: v.as_dict() for k, v in sorted(self.pairs.items())},
                           "groups": {k: v.as_dict() for k, v in self.groups.items()},
                           "n0": self.n0, "n0_tail": self.n0_tail, "meta": self.meta}, indent=1, sort_keys=True)

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        Path(path).write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path | None = DEFAULT_PATH) -> "Calibration":
        if path is None or not Path(path).exists():
            return cls()
        d = json.loads(Path(path).read_text())
        return cls(pairs={k: PairParams.from_dict(v) for k, v in d.get("pairs", {}).items()},
                   groups={k: PairParams.from_dict(v) for k, v in d.get("groups", {}).items()},
                   n0=d.get("n0"), n0_tail=d.get("n0_tail"), meta=d.get("meta", {}), identity=False)


_DEFAULT: Calibration | None = None


def default_calibration() -> Calibration:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Calibration.load(DEFAULT_PATH)
    return _DEFAULT


def set_default_calibration(c: Calibration | None) -> None:
    global _DEFAULT
    _DEFAULT = c


# ----------------------------------------------------------------------------- fitting
@dataclass
class Rows:
    game: np.ndarray
    pair: np.ndarray          # family pair string per row
    group: np.ndarray
    pj: np.ndarray
    pi: np.ndarray
    won: np.ndarray


def load_rows(scores_db: str | Path, offset: int = 15) -> Rows:
    c = sqlite3.connect(f"file:{Path(scores_db)}?mode=ro", uri=True)
    try:
        rs = c.execute("SELECT game_prefix, family_pair, p_joint, p_leg_a, p_leg_b, won FROM hist_pair_scores WHERE offset_min=?"
                       " AND p_leg_a IS NOT NULL AND p_leg_b IS NOT NULL", (offset,)).fetchall()
    finally:
        c.close()
    if not rs:
        raise ValueError(f"no scores at offset {offset} in {scores_db}")
    pair = np.array([r[1] for r in rs])
    return Rows(game=np.array([r[0] for r in rs]), pair=pair, group=np.array([group_of_family_pair(p) for p in pair]),
                pj=np.array([r[2] for r in rs], dtype=float), pi=np.array([r[3] * r[4] for r in rs], dtype=float),
                won=np.array([r[5] for r in rs], dtype=float))


def _fit_lambda(pj, pi, won) -> float | None:
    if len(pj) < MIN_PAIR_FIT:
        return None
    d = pj - pi
    den = float(np.dot(d, d))
    return None if den < 1e-9 else float(np.clip(np.dot(won - pi, d) / den, 0.0, 2.0))


def _fit_affine(p, won) -> tuple[float, float] | None:
    if len(p) < MIN_PAIR_FIT or float(np.var(p)) < 1e-10:
        return None
    A = np.vstack([np.ones_like(p), p]).T
    sol, *_ = np.linalg.lstsq(A, won, rcond=None)
    return float(sol[0]), float(sol[1])


@dataclass
class _Raw:
    """Unshrunk estimates for one unit (pair or group)."""
    n: int
    lam: float | None
    lo: tuple[float, float] | None
    hi: tuple[float, float] | None
    n_lo: int
    n_hi: int


def _raw_fit(pj, pi, won, lo_cut: float, hi_cut: float, lam_for_tails: float) -> _Raw:
    lam = _fit_lambda(pj, pi, won)
    p = pi + lam_for_tails * (pj - pi)
    sel_lo, sel_hi = p < lo_cut, p > hi_cut
    return _Raw(n=int(len(pj)), lam=lam, lo=_fit_affine(p[sel_lo], won[sel_lo]), hi=_fit_affine(p[sel_hi], won[sel_hi]),
                n_lo=int(sel_lo.sum()), n_hi=int(sel_hi.sum()))


def _shrink(n: int, n0: float) -> float:
    return n / (n + n0) if n0 > 0 else 1.0


def _fit_on(rows: Rows, sel: np.ndarray, *, n0: float, n0_tail: float, lo_cut: float, hi_cut: float) -> Calibration:
    cal = Calibration(identity=False, n0=int(n0), n0_tail=int(n0_tail))
    # groups: pooled estimates (lambda first, tails on the lambda-adjusted value)
    graw: dict[str, _Raw] = {}
    for g in GROUPS:
        m = sel & (rows.group == g)
        lam_g = _fit_lambda(rows.pj[m], rows.pi[m], rows.won[m]) if m.any() else None
        lam_g = 1.0 if lam_g is None else lam_g
        graw[g] = _raw_fit(rows.pj[m], rows.pi[m], rows.won[m], lo_cut, hi_cut, lam_g) if m.any() else _Raw(0, None, None, None, 0, 0)
        graw[g].lam = lam_g
        cal.groups[g] = PairParams(lam=lam_g, lo=TailMap(lo_cut, *graw[g].lo) if graw[g].lo else None,
                                   hi=TailMap(hi_cut, *graw[g].hi) if graw[g].hi else None, resid=0.0, n=graw[g].n,
                                   n_lo=graw[g].n_lo, n_hi=graw[g].n_hi, lam_pair=None, lam_group=lam_g, group=g)
    # pairs: own estimate shrunk toward the group
    for fp in np.unique(rows.pair[sel]):
        m = sel & (rows.pair == fp)
        g = group_of_family_pair(str(fp))
        gr = graw[g]
        raw = _raw_fit(rows.pj[m], rows.pi[m], rows.won[m], lo_cut, hi_cut, gr.lam)
        w = _shrink(raw.n, n0) if raw.lam is not None else 0.0
        lam = w * (raw.lam if raw.lam is not None else gr.lam) + (1 - w) * gr.lam
        raw_t = _raw_fit(rows.pj[m], rows.pi[m], rows.won[m], lo_cut, hi_cut, lam)   # tails on the value actually used

        def blend(own: tuple[float, float] | None, grp: tuple[float, float] | None, n_t: int) -> TailMap | None:
            if grp is None and own is None:
                return None
            if own is None:
                return TailMap(0, *grp)
            if grp is None:
                return TailMap(0, *own)
            wt = _shrink(n_t, n0_tail)
            return TailMap(0, wt * own[0] + (1 - wt) * grp[0], wt * own[1] + (1 - wt) * grp[1])
        lo = blend(raw_t.lo, gr.lo, raw_t.n_lo)
        hi = blend(raw_t.hi, gr.hi, raw_t.n_hi)
        if lo:
            lo.cut = lo_cut
        if hi:
            hi.cut = hi_cut
        cal.pairs[str(fp)] = PairParams(lam=lam, lo=lo, hi=hi, resid=0.0, n=raw.n, n_lo=raw_t.n_lo, n_hi=raw_t.n_hi,
                                        lam_pair=raw.lam, lam_group=gr.lam, group=g)
    return cal


def _apply_vec(cal: Calibration, rows: Rows, sel: np.ndarray) -> np.ndarray:
    out = np.empty(int(sel.sum()))
    for i, (fp, g, pj, pi) in enumerate(zip(rows.pair[sel], rows.group[sel], rows.pj[sel], rows.pi[sel])):
        out[i] = cal.apply(str(fp), str(g), float(pj), float(pi))[0]
    return out


def brier(p, w) -> float:
    return float(np.mean((p - w) ** 2)) if len(p) else float("nan")


def logloss(p, w) -> float:
    if not len(p):
        return float("nan")
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(w * np.log(p) + (1 - w) * np.log(1 - p)))


def bucket_resid(p: np.ndarray, w: np.ndarray, min_n: int = 30) -> float | None:
    """RMS over predicted buckets of (realized - predicted), weighted by bucket size; None when thin."""
    num = den = 0.0
    for a, b in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:]):
        m = (p >= a) & (p < b)
        if m.sum() >= min_n:
            num += m.sum() * (w[m].mean() - p[m].mean()) ** 2
            den += m.sum()
    return float(np.sqrt(num / den)) if den else None


def _cv_predict(rows: Rows, fold: np.ndarray, folds: int, **kw) -> np.ndarray:
    oos = np.empty_like(rows.pj)
    for k in range(folds):
        test = fold == k
        cal_k = _fit_on(rows, ~test, **kw)
        oos[test] = _apply_vec(cal_k, rows, test)
    return oos


def fit(scores_db: str | Path, offset: int = 15, *, folds: int = 5, seed: int = 0, lo_cut: float = 0.05,
        hi_cut: float = 0.80, n0_grid=N0_GRID, n0_tail_grid=N0_TAIL_GRID) -> tuple[Calibration, dict[str, Any]]:
    rows = load_rows(scores_db, offset)
    games = np.unique(rows.game)
    rng = np.random.default_rng(seed)
    fold_of_game = {g: i % folds for i, g in enumerate(rng.permutation(games))}
    fold = np.array([fold_of_game[g] for g in rows.game])
    # choose n0 (lambda shrinkage) and n0_tail by out-of-sample log-loss
    grid: dict[tuple[int, int], tuple[float, np.ndarray]] = {}
    for n0 in n0_grid:
        for n0t in n0_tail_grid:
            oos = _cv_predict(rows, fold, folds, n0=n0, n0_tail=n0t, lo_cut=lo_cut, hi_cut=hi_cut)
            grid[(n0, n0t)] = (logloss(oos, rows.won), oos)
    (n0, n0t), (ll_best, oos) = min(grid.items(), key=lambda kv: kv[1][0])
    final = _fit_on(rows, np.ones(len(rows.pj), dtype=bool), n0=n0, n0_tail=n0t, lo_cut=lo_cut, hi_cut=hi_cut)
    # pooled-group baseline (lambda and tails per group only) for comparison, out of sample
    oos_group = np.empty_like(rows.pj)
    for k in range(folds):
        test = fold == k
        cal_k = _fit_on(rows, ~test, n0=1e12, n0_tail=1e12, lo_cut=lo_cut, hi_cut=hi_cut)   # w -> 0: group values
        oos_group[test] = _apply_vec(cal_k, rows, test)
    # residual model error per pair (OOS), group fallback when thin
    for g in GROUPS:
        m = rows.group == g
        r = bucket_resid(oos[m], rows.won[m]) if m.sum() >= 100 else None
        final.groups[g].resid = r if r is not None else 0.0
    for fp, pp in final.pairs.items():
        m = rows.pair == fp
        r = bucket_resid(oos[m], rows.won[m]) if m.sum() >= 100 else None
        pp.resid = r if r is not None else final.groups[pp.group].resid

    def block(sel) -> dict[str, float]:
        return {"n": int(sel.sum()), "brier_engine": brier(rows.pj[sel], rows.won[sel]), "brier_indep": brier(rows.pi[sel], rows.won[sel]),
                "brier_group_oos": brier(oos_group[sel], rows.won[sel]), "brier_pair_oos": brier(oos[sel], rows.won[sel]),
                "logloss_engine": logloss(rows.pj[sel], rows.won[sel]), "logloss_indep": logloss(rows.pi[sel], rows.won[sel]),
                "logloss_group_oos": logloss(oos_group[sel], rows.won[sel]), "logloss_pair_oos": logloss(oos[sel], rows.won[sel])}
    metrics: dict[str, Any] = {"offset": offset, "folds": folds, "games": int(len(games)), "n": int(len(rows.pj)), "n0": n0, "n0_tail": n0t,
                               "n0_grid_logloss": {f"{a},{b}": v[0] for (a, b), v in grid.items()},
                               "overall": block(np.ones(len(rows.pj), dtype=bool)),
                               "resid_engine": bucket_resid(rows.pj, rows.won), "resid_pair_oos": bucket_resid(oos, rows.won),
                               "groups": {g: {**block(rows.group == g), "lambda": final.groups[g].lam, "resid": final.groups[g].resid}
                                          for g in GROUPS if (rows.group == g).any()},
                               "pairs": {}}
    for fp, pp in sorted(final.pairs.items(), key=lambda kv: -kv[1].n):
        m = rows.pair == fp
        metrics["pairs"][fp] = {**block(m), "group": pp.group, "lambda_pair": pp.lam_pair, "lambda_group": pp.lam_group, "lambda": pp.lam,
                                "n_lo": pp.n_lo, "n_hi": pp.n_hi, "lo": pp.lo.as_dict() if pp.lo else None, "hi": pp.hi.as_dict() if pp.hi else None,
                                "resid": pp.resid}
    metrics["tails"] = {}
    for name, sel in (("lo (<0.05)", rows.pj < lo_cut), ("hi (>0.80)", rows.pj > hi_cut)):
        if sel.any():
            metrics["tails"][name] = {"n": int(sel.sum()), "pred_engine": float(rows.pj[sel].mean()), "realized": float(rows.won[sel].mean()),
                                      "pred_group_oos": float(oos_group[sel].mean()), "pred_pair_oos": float(oos[sel].mean())}
    final.meta = {"fitted_at": time.strftime("%Y-%m-%d %H:%M"), "scores_db": str(scores_db), "offset": offset, "folds": folds,
                  "games": int(len(games)), "n": int(len(rows.pj)), "lo_cut": lo_cut, "hi_cut": hi_cut, "n0": n0, "n0_tail": n0t,
                  "logloss_pair_oos": metrics["overall"]["logloss_pair_oos"], "logloss_group_oos": metrics["overall"]["logloss_group_oos"]}
    return final, metrics


def format_metrics(m: dict[str, Any], max_pairs: int = 40) -> str:
    o = m["overall"]
    L = [f"calibration fit: offset {m['offset']} min, {m['games']} games, {m['n']} pairs, {m['folds']}-fold CV by game; "
         f"n0 (lambda) = {m['n0']}, n0 (tails) = {m['n0_tail']} chosen by OOS log-loss",
         f"  Brier   engine {o['brier_engine']:.4f}  indep {o['brier_indep']:.4f}  pooled-group cal (OOS) {o['brier_group_oos']:.4f}  per-pair cal (OOS) {o['brier_pair_oos']:.4f}",
         f"  logloss engine {o['logloss_engine']:.4f}  indep {o['logloss_indep']:.4f}  pooled-group cal (OOS) {o['logloss_group_oos']:.4f}  per-pair cal (OOS) {o['logloss_pair_oos']:.4f}",
         f"  bucket residual (model error) engine {m['resid_engine']:.4f} -> per-pair cal {m['resid_pair_oos']:.4f}",
         "\n  groups (pooled):", f"  {'group':<12} {'n':>7} {'lambda':>7} {'resid':>6} {'brier_e':>8} {'brier_i':>8} {'brier_grp':>9} {'brier_pair':>10} {'ll_e':>7} {'ll_grp':>7} {'ll_pair':>7}"]
    for g, r in m["groups"].items():
        L.append(f"  {g:<12} {r['n']:>7} {r['lambda']:7.3f} {r['resid']:6.3f} {r['brier_engine']:8.4f} {r['brier_indep']:8.4f} {r['brier_group_oos']:9.4f} {r['brier_pair_oos']:10.4f} "
                 f"{r['logloss_engine']:7.4f} {r['logloss_group_oos']:7.4f} {r['logloss_pair_oos']:7.4f}")
    L += ["\n  per family pair (used lambda = shrunk; tails a + b p below 0.05 / above 0.80):",
          f"  {'family pair':<34} {'n':>6} {'n_lo':>5} {'n_hi':>5} {'lam_pair':>8} {'lam_grp':>7} {'lam_used':>8} {'lo a,b':>14} {'hi a,b':>14} {'resid':>6} {'brier_e':>8} {'brier_pair':>10} {'ll_e':>7} {'ll_pair':>7}"]
    for fp, r in list(m["pairs"].items())[:max_pairs]:
        lp = "   -   " if r["lambda_pair"] is None else f"{r['lambda_pair']:7.3f}"
        lo = "-" if not r["lo"] else f"{r['lo']['a']:+.3f},{r['lo']['b']:.3f}"
        hi = "-" if not r["hi"] else f"{r['hi']['a']:+.3f},{r['hi']['b']:.3f}"
        L.append(f"  {fp:<34} {r['n']:>6} {r['n_lo']:>5} {r['n_hi']:>5} {lp:>8} {r['lambda_group']:7.3f} {r['lambda']:8.3f} {lo:>14} {hi:>14} {r['resid']:6.3f} "
                 f"{r['brier_engine']:8.4f} {r['brier_pair_oos']:10.4f} {r['logloss_engine']:7.4f} {r['logloss_pair_oos']:7.4f}")
    for k, t in m["tails"].items():
        L.append(f"  tail {k:<11} n={t['n']:<6} engine {t['pred_engine']:.3f} -> realized {t['realized']:.3f}; pooled-group {t['pred_group_oos']:.3f}; per-pair {t['pred_pair_oos']:.3f} (OOS)")
    return "\n".join(L)
