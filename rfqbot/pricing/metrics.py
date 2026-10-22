"""Scoring and clustered-regression helpers shared by calibrate.py, validate.py and venue_weight.py.

``clustered_ols`` is the one sandwich estimator in the package. Both callers feed it per-cluster
sufficient statistics (X'X, X'y per game) rather than rows, because venue_weight accumulates
millions of seconds per cluster and must not hold them; calibrate builds the same statistics from
its row arrays with ``cluster_stats``. The variance carries the usual G / (G - 1) small-sample
factor so a 15-game pair does not look more precise than it is.
"""
from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np

MIN_CLUSTERS = 2


def brier(p, w) -> float:
    p = np.asarray(p, dtype=float); w = np.asarray(w, dtype=float)
    return float(np.mean((p - w) ** 2)) if len(p) else float("nan")


def logloss(p, w, eps: float = 1e-4) -> float:
    p = np.asarray(p, dtype=float); w = np.asarray(w, dtype=float)
    if not len(p):
        return float("nan")
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(w * np.log(p) + (1 - w) * np.log(1 - p)))


def family_pair(family_a: str | None, period_a: str | None, family_b: str | None, period_b: str | None) -> str:
    """Canonical ``family/period x family/period`` key of two legs (sorted, so order-free)."""
    a, b = f"{family_a or '?'}/{period_a or '?'}", f"{family_b or '?'}/{period_b or '?'}"
    return " x ".join(sorted((a, b)))


def cluster_stats(X: np.ndarray, y: np.ndarray, groups: np.ndarray) -> dict[object, tuple[np.ndarray, np.ndarray]]:
    """Per-cluster (X'X, X'y) from rows; ``X`` is (n, k)."""
    out: dict[object, tuple[np.ndarray, np.ndarray]] = {}
    for g in np.unique(groups):
        m = groups == g
        Xg = X[m]
        out[g] = (Xg.T @ Xg, Xg.T @ y[m])
    return out


def clustered_ols(stats: Mapping[object, tuple[np.ndarray, np.ndarray]] | Iterable[tuple[np.ndarray, np.ndarray]],
                  *, min_clusters: int = MIN_CLUSTERS) -> tuple[np.ndarray | None, np.ndarray | None]:
    """OLS beta and its cluster-robust covariance from per-cluster sufficient statistics.

    V = (X'X)^-1 [sum_g s_g s_g'] (X'X)^-1 * G / (G - 1), with s_g = X_g'(y_g - X_g beta) the
    cluster score. Returns (None, None) with fewer than ``min_clusters`` clusters or a singular
    design (``pinv`` handles the latter in practice; an all-zero design gives None).
    """
    items = list(stats.values()) if isinstance(stats, Mapping) else list(stats)
    G = len(items)
    if G < min_clusters:
        return None, None
    k = items[0][0].shape[0]
    xx = np.zeros((k, k)); xy = np.zeros(k)
    for cx, cy in items:
        xx += cx; xy += cy
    if not np.any(np.abs(xx) > 1e-12):
        return None, None
    inv = np.linalg.pinv(xx)
    beta = inv @ xy
    meat = np.zeros((k, k))
    for cx, cy in items:
        s = cy - cx @ beta
        meat += np.outer(s, s)
    V = inv @ meat @ inv * (G / (G - 1))
    return beta, V
