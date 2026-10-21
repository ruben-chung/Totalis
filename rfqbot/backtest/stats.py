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


def verdict(diff: float, se: float | None, g: int) -> str:
    """'exceeds' / 'within' the clustered SE band (|diff| > 2 SE), or 'underpowered' (too few clusters / no SE)."""
    if se is None or g < MIN_CLUSTERS:
        return "underpowered"
    return "exceeds 2 SE" if abs(diff) > 2 * se else "within 2 SE"
