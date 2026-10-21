"""Calibration layer on the engine's joint probability, measured per market-type pair.

    p_cal = p_indep + lambda_pair * (p_joint - p_indep)
    then tail maps: p < lo_cut -> a_lo + b_lo p,   p > hi_cut -> a_hi + b_hi p   (clipped to [0, 1])

``p_indep`` is the product of the engine's own leg marginals (what quote assembly has at run
time). The unit of measurement is the **family pair** (``family/period x family/period`` of the
two legs, sorted). Rows within one game share that game's outcome, so every estimate carries a
**game-clustered** standard error (sandwich estimator with game clusters) and shrinkage is by
precision, not by row count:

    lambda_used = (lambda_pair / var_pair + lambda_group / var_group) / (1 / var_pair + 1 / var_group)

(same for each tail-map parameter, clustered on the tail rows). Pair classes:
  * ``market_pinned``: both legs full game, different score variables (spread x total,
    spread x team total, ...): the coupling is read from the four FG ladders. lambda defaults to
    1.0; the outcome data may move it only when the clustered 95% interval excludes 1.0
    (``lambda_status`` ``informative``, then the precision shrink applies), else it stays 1.0
    and is ``uninformative``.
  * ``structural``: both legs full game on the same score variable (two spreads, spread x
    moneyline, exact margin x spread, two totals): the joint is a logical function of one
    variable; lambda is fixed at 1.0.
  * ``prior``: any pair with a half leg: the coupling comes from the engine's halves prior
    (2H ladders rarely qualify) and may be shrunk freely.
Groups (``within_FG`` / ``within_half`` / ``cross``) provide the fallback for pairs unseen in
the fit (``calibration_level`` ``group``); without a file the engine runs uncalibrated
(``none``). Each pair also gets a residual model error (RMS over predicted buckets of realized
minus predicted, out of sample, 5-fold by game; group fallback when thin) that ``JointQuote``
adds to its interval. Parameters live in ``rfqbot/pricing/calibration.json``.
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
MIN_PAIR_FIT = 20            # rows; below this a pair has no estimate of its own
MIN_GAMES = 5                # clusters; below this the clustered variance is meaningless
Z95 = 1.96
VAR_FLOOR = 1e-6
STRUCTURAL_FAMILIES = ("spread", "moneyline", "exact_margin")   # all functions of the FG margin


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
    n_games: int = 0
    n_lo: int = 0
    n_hi: int = 0
    lam_pair: float | None = None      # unshrunk estimate
    lam_se: float | None = None        # game-clustered standard error of lam_pair
    lam_group: float | None = None
    group: str = ""
    kind: str = "prior"                # market_pinned | structural | prior
    lam_status: str = "shrunk"         # shrunk | informative | uninformative | fixed | group

    @property
    def lam_ci(self) -> tuple[float, float] | None:
        if self.lam_pair is None or self.lam_se is None:
            return None
        return (self.lam_pair - Z95 * self.lam_se, self.lam_pair + Z95 * self.lam_se)

    def as_dict(self) -> dict[str, Any]:
        return {"lambda": self.lam, "lo": self.lo.as_dict() if self.lo else None, "hi": self.hi.as_dict() if self.hi else None,
                "resid": self.resid, "n": self.n, "n_games": self.n_games, "n_lo": self.n_lo, "n_hi": self.n_hi,
                "lambda_pair": self.lam_pair, "lambda_se": self.lam_se, "lambda_group": self.lam_group, "group": self.group,
                "kind": self.kind, "lambda_status": self.lam_status}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PairParams":
        return cls(lam=float(d["lambda"]), lo=TailMap.from_dict(d["lo"]) if d.get("lo") else None,
                   hi=TailMap.from_dict(d["hi"]) if d.get("hi") else None, resid=float(d.get("resid", 0.0)),
                   n=int(d.get("n", 0)), n_games=int(d.get("n_games", 0)), n_lo=int(d.get("n_lo", 0)), n_hi=int(d.get("n_hi", 0)),
                   lam_pair=d.get("lambda_pair"), lam_se=d.get("lambda_se"), lam_group=d.get("lambda_group"),
                   group=d.get("group", ""), kind=d.get("kind", "prior"), lam_status=d.get("lambda_status", "shrunk"))


def pair_kind(family_pair: str) -> str:
    """market_pinned | structural | prior (see module docstring)."""
    a, b = family_pair.split(" x ")
    fa, pa = a.split("/"); fb, pb = b.split("/")
    if pa != "FG" or pb != "FG":
        return "prior"
    if (fa in STRUCTURAL_FAMILIES and fb in STRUCTURAL_FAMILIES) or (fa == fb == "total"):
        return "structural"
    return "market_pinned"


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
                           "meta": self.meta}, indent=1, sort_keys=True)

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        Path(path).write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path | None = DEFAULT_PATH) -> "Calibration":
        if path is None or not Path(path).exists():
            return cls()
        d = json.loads(Path(path).read_text())
        return cls(pairs={k: PairParams.from_dict(v) for k, v in d.get("pairs", {}).items()},
                   groups={k: PairParams.from_dict(v) for k, v in d.get("groups", {}).items()},
                   meta=d.get("meta", {}), identity=False)


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


@dataclass
class _Est:
    value: float | None
    var: float | None          # game-clustered variance
    n: int
    n_games: int


def _lambda_clustered(pj, pi, won, games) -> _Est:
    """lambda = sum((w - pi) d) / sum(d^2), d = pj - pi, with a game-clustered sandwich variance."""
    n = len(pj)
    ng = len(np.unique(games)) if n else 0
    if n < MIN_PAIR_FIT or ng < MIN_GAMES:
        return _Est(None, None, int(n), int(ng))
    d = pj - pi
    den = float(np.dot(d, d))
    if den < 1e-9:
        return _Est(None, None, int(n), int(ng))
    lam = float(np.dot(won - pi, d) / den)
    u = (won - pi - lam * d) * d                          # score contributions
    meat = sum(float(u[games == g].sum()) ** 2 for g in np.unique(games))
    return _Est(float(np.clip(lam, 0.0, 2.0)), max(meat / den ** 2, VAR_FLOOR), int(n), int(ng))


def _affine_clustered(p, won, games) -> tuple[_Est, _Est]:
    """won ~ a + b p by least squares; returns (a, b) each with a game-clustered variance."""
    n = len(p)
    ng = len(np.unique(games)) if n else 0
    if n < MIN_PAIR_FIT or ng < MIN_GAMES or float(np.var(p)) < 1e-10:
        return _Est(None, None, int(n), int(ng)), _Est(None, None, int(n), int(ng))
    X = np.vstack([np.ones_like(p), p]).T
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ won
    e = won - X @ beta
    meat = np.zeros((2, 2))
    for g in np.unique(games):
        m = games == g
        v = X[m].T @ e[m]
        meat += np.outer(v, v)
    V = xtx_inv @ meat @ xtx_inv
    return (_Est(float(beta[0]), max(float(V[0, 0]), VAR_FLOOR), int(n), int(ng)),
            _Est(float(beta[1]), max(float(V[1, 1]), VAR_FLOOR), int(n), int(ng)))


def _precision_blend(own: _Est, grp: _Est) -> float | None:
    if own.value is None and grp.value is None:
        return None
    if own.value is None or own.var is None:
        return grp.value
    if grp.value is None or grp.var is None:
        return own.value
    wp, wg = 1.0 / own.var, 1.0 / grp.var
    return (own.value * wp + grp.value * wg) / (wp + wg)


@dataclass
class _Unit:
    lam: _Est
    lo: tuple[_Est, _Est]
    hi: tuple[_Est, _Est]


def _fit_unit(pj, pi, won, games, lo_cut, hi_cut, lam_for_tails: float) -> _Unit:
    lam = _lambda_clustered(pj, pi, won, games)
    p = pi + lam_for_tails * (pj - pi)
    sl, sh = p < lo_cut, p > hi_cut
    return _Unit(lam, _affine_clustered(p[sl], won[sl], games[sl]), _affine_clustered(p[sh], won[sh], games[sh]))


def _tail(own: tuple[_Est, _Est], grp: tuple[_Est, _Est], cut: float) -> TailMap | None:
    a = _precision_blend(own[0], grp[0])
    b = _precision_blend(own[1], grp[1])
    return None if a is None or b is None else TailMap(cut, a, b)


def _fit_on(rows: Rows, sel: np.ndarray, *, lo_cut: float, hi_cut: float) -> Calibration:
    cal = Calibration(identity=False)
    gunits: dict[str, _Unit] = {}
    for g in GROUPS:
        m = sel & (rows.group == g)
        if not m.any():
            continue
        lam_g = _lambda_clustered(rows.pj[m], rows.pi[m], rows.won[m], rows.game[m])
        lam_val = 1.0 if lam_g.value is None else lam_g.value
        u = _fit_unit(rows.pj[m], rows.pi[m], rows.won[m], rows.game[m], lo_cut, hi_cut, lam_val)
        gunits[g] = u
        cal.groups[g] = PairParams(lam=lam_val, lo=_tail(u.lo, (_Est(None, None, 0, 0),) * 2, lo_cut), hi=_tail(u.hi, (_Est(None, None, 0, 0),) * 2, hi_cut),
                                   resid=0.0, n=u.lam.n, n_games=u.lam.n_games, n_lo=u.lo[0].n, n_hi=u.hi[0].n,
                                   lam_pair=lam_g.value, lam_se=None if lam_g.var is None else float(np.sqrt(lam_g.var)),
                                   lam_group=lam_val, group=g, kind="group", lam_status="group")
    for fp in np.unique(rows.pair[sel]):
        m = sel & (rows.pair == fp)
        g = group_of_family_pair(str(fp))
        gu = gunits.get(g)
        kind = pair_kind(str(fp))
        own = _lambda_clustered(rows.pj[m], rows.pi[m], rows.won[m], rows.game[m])
        lam_group = cal.groups[g].lam if g in cal.groups else 1.0
        se = None if own.var is None else float(np.sqrt(own.var))
        if kind == "structural":
            lam, status = 1.0, "fixed"
        elif own.value is None:
            lam, status = (1.0, "uninformative") if kind == "market_pinned" else (lam_group, "group")
        elif kind == "market_pinned":
            lo_ci, hi_ci = own.value - Z95 * se, own.value + Z95 * se
            if lo_ci <= 1.0 <= hi_ci:
                lam, status = 1.0, "uninformative"
            else:                       # the data contradicts the ladders: fall through to the precision shrink
                lam = _precision_blend(own, gu.lam if gu else _Est(lam_group, None, 0, 0))
                status = "informative"
        else:
            lam = _precision_blend(own, gu.lam if gu else _Est(lam_group, None, 0, 0))
            status = "shrunk"
        lam = float(np.clip(lam, 0.0, 2.0))
        u = _fit_unit(rows.pj[m], rows.pi[m], rows.won[m], rows.game[m], lo_cut, hi_cut, lam)
        glo = gu.lo if gu else (_Est(None, None, 0, 0),) * 2
        ghi = gu.hi if gu else (_Est(None, None, 0, 0),) * 2
        cal.pairs[str(fp)] = PairParams(lam=lam, lo=_tail(u.lo, glo, lo_cut), hi=_tail(u.hi, ghi, hi_cut), resid=0.0,
                                        n=own.n, n_games=own.n_games, n_lo=u.lo[0].n, n_hi=u.hi[0].n, lam_pair=own.value,
                                        lam_se=se, lam_group=lam_group, group=g, kind=kind, lam_status=status)
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


def _group_only(cal: Calibration) -> Calibration:
    """Pooled-group baseline: every pair uses its group's values."""
    return Calibration(pairs={}, groups=cal.groups, identity=False)


def fit(scores_db: str | Path, offset: int = 15, *, folds: int = 5, seed: int = 0, lo_cut: float = 0.05,
        hi_cut: float = 0.80) -> tuple[Calibration, dict[str, Any]]:
    rows = load_rows(scores_db, offset)
    games = np.unique(rows.game)
    rng = np.random.default_rng(seed)
    fold_of_game = {g: i % folds for i, g in enumerate(rng.permutation(games))}
    fold = np.array([fold_of_game[g] for g in rows.game])
    oos = np.empty_like(rows.pj)
    oos_group = np.empty_like(rows.pj)
    for k in range(folds):
        test = fold == k
        cal_k = _fit_on(rows, ~test, lo_cut=lo_cut, hi_cut=hi_cut)
        oos[test] = _apply_vec(cal_k, rows, test)
        oos_group[test] = _apply_vec(_group_only(cal_k), rows, test)
    final = _fit_on(rows, np.ones(len(rows.pj), dtype=bool), lo_cut=lo_cut, hi_cut=hi_cut)
    for g in GROUPS:
        if g not in final.groups:
            continue
        m = rows.group == g
        r = bucket_resid(oos[m], rows.won[m]) if m.sum() >= 100 else None
        final.groups[g].resid = r if r is not None else 0.0
    for fp, pp in final.pairs.items():
        m = rows.pair == fp
        r = bucket_resid(oos[m], rows.won[m]) if m.sum() >= 100 else None
        pp.resid = r if r is not None else final.groups[pp.group].resid

    def block(sel) -> dict[str, float]:
        return {"n": int(sel.sum()), "n_games": int(len(np.unique(rows.game[sel]))),
                "brier_engine": brier(rows.pj[sel], rows.won[sel]), "brier_indep": brier(rows.pi[sel], rows.won[sel]),
                "brier_group_oos": brier(oos_group[sel], rows.won[sel]), "brier_pair_oos": brier(oos[sel], rows.won[sel]),
                "logloss_engine": logloss(rows.pj[sel], rows.won[sel]), "logloss_indep": logloss(rows.pi[sel], rows.won[sel]),
                "logloss_group_oos": logloss(oos_group[sel], rows.won[sel]), "logloss_pair_oos": logloss(oos[sel], rows.won[sel])}
    metrics: dict[str, Any] = {"offset": offset, "folds": folds, "games": int(len(games)), "n": int(len(rows.pj)),
                               "overall": block(np.ones(len(rows.pj), dtype=bool)),
                               "resid_engine": bucket_resid(rows.pj, rows.won), "resid_pair_oos": bucket_resid(oos, rows.won),
                               "groups": {g: {**block(rows.group == g), "lambda": final.groups[g].lam, "lambda_se": final.groups[g].lam_se,
                                              "resid": final.groups[g].resid} for g in GROUPS if g in final.groups},
                               "pairs": {}}
    for fp, pp in sorted(final.pairs.items(), key=lambda kv: -kv[1].n):
        m = rows.pair == fp
        metrics["pairs"][fp] = {**block(m), "group": pp.group, "kind": pp.kind, "lambda_pair": pp.lam_pair, "lambda_se": pp.lam_se,
                                "lambda_ci": pp.lam_ci, "lambda_group": pp.lam_group, "lambda": pp.lam, "lambda_status": pp.lam_status,
                                "n_lo": pp.n_lo, "n_hi": pp.n_hi, "lo": pp.lo.as_dict() if pp.lo else None, "hi": pp.hi.as_dict() if pp.hi else None,
                                "resid": pp.resid}
    metrics["tails"] = {}
    for name, sel in (("lo (<0.05)", rows.pj < lo_cut), ("hi (>0.80)", rows.pj > hi_cut)):
        if sel.any():
            metrics["tails"][name] = {"n": int(sel.sum()), "pred_engine": float(rows.pj[sel].mean()), "realized": float(rows.won[sel].mean()),
                                      "pred_group_oos": float(oos_group[sel].mean()), "pred_pair_oos": float(oos[sel].mean())}
    final.meta = {"fitted_at": time.strftime("%Y-%m-%d %H:%M"), "scores_db": str(scores_db), "offset": offset, "folds": folds,
                  "games": int(len(games)), "n": int(len(rows.pj)), "lo_cut": lo_cut, "hi_cut": hi_cut,
                  "logloss_pair_oos": metrics["overall"]["logloss_pair_oos"], "logloss_group_oos": metrics["overall"]["logloss_group_oos"],
                  "shrinkage": "precision-weighted with game-clustered variances; market_pinned pairs keep 1.0 unless the 95% CI excludes it"}
    return final, metrics


def format_metrics(m: dict[str, Any], max_pairs: int = 45) -> str:
    o = m["overall"]
    L = [f"calibration fit: offset {m['offset']} min, {m['games']} games, {m['n']} pairs, {m['folds']}-fold CV by game; "
         f"shrinkage by precision with game-clustered variances",
         f"  Brier   engine {o['brier_engine']:.4f}  indep {o['brier_indep']:.4f}  pooled-group cal (OOS) {o['brier_group_oos']:.4f}  per-pair cal (OOS) {o['brier_pair_oos']:.4f}",
         f"  logloss engine {o['logloss_engine']:.4f}  indep {o['logloss_indep']:.4f}  pooled-group cal (OOS) {o['logloss_group_oos']:.4f}  per-pair cal (OOS) {o['logloss_pair_oos']:.4f}",
         f"  bucket residual (model error) engine {m['resid_engine']:.4f} -> per-pair cal {m['resid_pair_oos']:.4f}",
         "\n  groups (pooled):", f"  {'group':<12} {'n':>7} {'games':>5} {'lambda':>7} {'se':>6} {'resid':>6} {'brier_e':>8} {'brier_grp':>9} {'brier_pair':>10} {'ll_e':>7} {'ll_grp':>7} {'ll_pair':>7}"]
    for g, r in m["groups"].items():
        se = "  -  " if r["lambda_se"] is None else f"{r['lambda_se']:.3f}"
        L.append(f"  {g:<12} {r['n']:>7} {r['n_games']:>5} {r['lambda']:7.3f} {se:>6} {r['resid']:6.3f} {r['brier_engine']:8.4f} {r['brier_group_oos']:9.4f} {r['brier_pair_oos']:10.4f} "
                 f"{r['logloss_engine']:7.4f} {r['logloss_group_oos']:7.4f} {r['logloss_pair_oos']:7.4f}")
    L += ["\n  per family pair (lam_pair with game-clustered 95% CI; lam_used after the rules; tails a + b p below 0.05 / above 0.80):",
          f"  {'family pair':<34} {'kind':<14} {'n':>6} {'games':>5} {'lam_pair':>8} {'95% CI':>16} {'lam_grp':>7} {'lam_used':>8} {'status':<14} {'lo a,b':>13} {'hi a,b':>13} {'resid':>6} {'ll_e':>7} {'ll_pair':>7}"]
    for fp, r in list(m["pairs"].items())[:max_pairs]:
        lp = "   -   " if r["lambda_pair"] is None else f"{r['lambda_pair']:7.3f}"
        ci = "       -        " if r["lambda_ci"] is None else f"[{r['lambda_ci'][0]:+.2f}, {r['lambda_ci'][1]:+.2f}]"
        lo = "-" if not r["lo"] else f"{r['lo']['a']:+.3f},{r['lo']['b']:.2f}"
        hi = "-" if not r["hi"] else f"{r['hi']['a']:+.3f},{r['hi']['b']:.2f}"
        L.append(f"  {fp:<34} {r['kind']:<14} {r['n']:>6} {r['n_games']:>5} {lp:>8} {ci:>16} {r['lambda_group']:7.3f} {r['lambda']:8.3f} {r['lambda_status']:<14} "
                 f"{lo:>13} {hi:>13} {r['resid']:6.3f} {r['logloss_engine']:7.4f} {r['logloss_pair_oos']:7.4f}")
    for k, t in m["tails"].items():
        L.append(f"  tail {k:<11} n={t['n']:<6} engine {t['pred_engine']:.3f} -> realized {t['realized']:.3f}; pooled-group {t['pred_group_oos']:.3f}; per-pair {t['pred_pair_oos']:.3f} (OOS)")
    return "\n".join(L)
