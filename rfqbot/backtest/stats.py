"""Game-clustered statistics (Ruben's rule: count evidence in games, not RFQs or pairs).

Combos can span games, so two rows share information when their game sets connect. Clusters are the connected
components of the games-by-rows incidence (union-find): rows in different components depend on different
independent game outcomes, which is the assumption a cluster-robust standard error needs. If cross-game combos
fuse everything into few components the SE is reported with that component count and flagged underpowered.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

MIN_CLUSTERS = 10


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def cluster_ids(games_per_row: Sequence[Sequence[str]]) -> list[str]:
    """One cluster id per row (the smallest game name of its connected component; rows without games are singletons)."""
    uf = _UF()
    for gs in games_per_row:
        for g in gs[1:]:
            uf.union(gs[0], g)
        if gs:
            uf.find(gs[0])
    return [uf.find(gs[0]) if gs else f"row:{i}" for i, gs in enumerate(games_per_row)]


def n_games(games_per_row: Iterable[Sequence[str]]) -> int:
    return len({g for gs in games_per_row for g in gs})


def cluster_mean_se(values: Sequence[float], clusters: Sequence[str]) -> tuple[float, float | None, int]:
    """(mean, cluster-robust SE of the mean, number of clusters); SE is None with fewer than two clusters.

    se^2 = G/(G-1) * sum_c (sum_{i in c} (y_i - ybar))^2 / n^2.
    """
    n = len(values)
    if n == 0:
        return 0.0, None, 0
    mean = sum(values) / n
    sums: dict[str, float] = {}
    for y, c in zip(values, clusters):
        sums[c] = sums.get(c, 0.0) + (y - mean)
    g = len(sums)
    if g < 2:
        return mean, None, g
    var = g / (g - 1) * sum(s * s for s in sums.values()) / (n * n)
    return mean, math.sqrt(var), g


def cluster_total_se(values: Sequence[float], clusters: Sequence[str]) -> tuple[float, float | None, int]:
    mean, se, g = cluster_mean_se(values, clusters)
    n = len(values)
    return mean * n, (None if se is None else se * n), g


def game_total_se(values: Sequence[float], games_per_row: Sequence[Sequence[str]]) -> tuple[float, float | None, int, int]:
    """(total, per-game cluster-robust SE of the total, evidence games, universe games): evidence games are those with a
    non-zero row (what the verdict is gated on), universe games every game any row names (the G in the G/(G-1) factor).

    A row touching k games is attached to each of them with weight 1/k, so the per-game scores add up to the total
    and different game dates are never joined (the union-find components can fuse a whole day through cross-game
    combos). Rows without games are singleton groups. Cross-game rows make neighbouring game scores correlated,
    which this ignores; the union-find SE is the conservative bound, this is the optimistic one.
    se^2 = G/(G-1) * sum_g (sum_i w_ig (y_i - ybar))^2.
    """
    n = len(values)
    if n == 0:
        return 0.0, None, 0, 0
    mean = sum(values) / n
    score: dict[str, float] = {}
    evidence: set[str] = set()
    for i, (y, gs) in enumerate(zip(values, games_per_row)):
        gs = list(dict.fromkeys(gs)) or [f"row:{i}"]
        for g in gs:
            score[g] = score.get(g, 0.0) + (y - mean) / len(gs)
            if y != 0.0:
                evidence.add(g)
    g = len(score)
    if g < 2 or not evidence:
        return mean * n, None, len(evidence), g
    # the G/(G-1) factor counts every game in the universe; the returned count is the games with a non-zero row
    # (evidence in games, not the zero-valued games a comparison universe carries)
    return mean * n, math.sqrt(g / (g - 1) * sum(v * v for v in score.values())), len(evidence), g


def verdict(diff: float, se: float | None, g: int) -> str:
    """'exceeds' / 'within' the clustered SE band (|diff| > 2 SE), or 'underpowered' (too few clusters / no SE)."""
    if se is None or g < MIN_CLUSTERS:
        return "underpowered"
    return "exceeds 2 SE" if abs(diff) > 2 * se else "within 2 SE"
