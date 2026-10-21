"""Fill curves F(m) per segment with hierarchical pooling.

For a node (a segment or one of its pooled ancestors) F(m) is the share of its decisions that
would have won at markup m: unprinted decisions never win, printed ones win iff m <= delta. Each
decision's win set is an interval [0, delta], so the empirical curve is non-increasing by
construction; ``isotonic_decreasing`` (pool-adjacent-violators) is still applied so any later
smoothing or weighting cannot break monotonicity. ``F_print`` is the same share among printed
decisions only (the survival function of delta, used to fit the competitor gap).

Pooling: a full cell resolves to the first node on its ladder (self, drop sigma, drop structure,
drop n_legs, drop sport, global) with at least ``n_min`` decisions; the global node always resolves.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import EdgeConfig
from .fills import Fill
from .segments import GLOBAL, Segment


def isotonic_decreasing(y: Sequence[float], w: Sequence[float] | None = None) -> list[float]:
    """Weighted least-squares fit of a non-increasing sequence (PAVA)."""
    n = len(y)
    if n == 0:
        return []
    w = list(w) if w is not None else [1.0] * n
    blocks: list[list[float]] = []   # [value, weight, count]
    for v, wt in zip(y, w):
        blocks.append([float(v), float(wt), 1])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            b = blocks.pop(); a = blocks.pop()
            tw = a[1] + b[1]
            blocks.append([(a[0] * a[1] + b[0] * b[1]) / tw if tw > 0 else max(a[0], b[0]), tw, a[2] + b[2]])
    out: list[float] = []
    for v, _, c in blocks:
        out.extend([v] * c)
    return out


@dataclass
class FillCurve:
    segment: Segment
    grid: list[float]
    f: list[float]                 # P(win at m) over all decisions in the node
    f_print: list[float]           # P(win at m | printed)
    n: int
    n_printed: int
    n_win_actual: int              # wins at the actual logged quotes
    fill_frac_mean: float          # mean size fraction among actual wins (1.0 when none)
    sigma_mean: float
    margin_mean: float             # mean actual markup in the node
    delta_mean: float | None       # competitor gap among printed decisions
    delta_sd: float | None
    delta_median: float | None
    deltas: list[float] = field(default_factory=list, repr=False)

    @property
    def key(self) -> str:
        return self.segment.key

    def f_at(self, m: float) -> float:
        """Linear interpolation on the grid (clamped)."""
        g = self.grid
        if m <= g[0]:
            return self.f[0]
        if m >= g[-1]:
            return self.f[-1]
        for i in range(1, len(g)):
            if m <= g[i]:
                t = (m - g[i - 1]) / (g[i] - g[i - 1])
                return self.f[i - 1] + t * (self.f[i] - self.f[i - 1])
        return self.f[-1]

    def as_dict(self) -> dict:
        return {"segment": self.key, "level": self.segment.level, "n": self.n, "n_printed": self.n_printed,
                "n_win_actual": self.n_win_actual, "fill_frac_mean": round(self.fill_frac_mean, 4),
                "sigma_mean": round(self.sigma_mean, 5), "margin_mean": round(self.margin_mean, 5),
                "delta_mean": None if self.delta_mean is None else round(self.delta_mean, 5),
                "delta_sd": None if self.delta_sd is None else round(self.delta_sd, 5),
                "delta_median": None if self.delta_median is None else round(self.delta_median, 5),
                "grid": self.grid, "f": [round(x, 5) for x in self.f], "f_print": [round(x, 5) for x in self.f_print]}


def empirical_curve(segment: Segment, fills: Sequence[Fill], grid: Sequence[float]) -> FillCurve:
    n = len(fills)
    printed = [f for f in fills if f.printed and f.delta is not None]
    wins = [0] * len(grid)
    for f in printed:
        for i, m in enumerate(grid):
            if m <= f.delta + 1e-12:
                wins[i] += 1
    f_all = isotonic_decreasing([w / n if n else 0.0 for w in wins])
    f_pr = isotonic_decreasing([w / len(printed) if printed else 0.0 for w in wins])
    deltas = sorted(f.delta for f in printed)
    actual = [f for f in printed if f.win_actual]
    d_mean = sum(deltas) / len(deltas) if deltas else None
    d_sd = math.sqrt(sum((x - d_mean) ** 2 for x in deltas) / (len(deltas) - 1)) if deltas and len(deltas) > 1 else None
    d_med = deltas[len(deltas) // 2] if deltas else None
    return FillCurve(segment=segment, grid=list(grid), f=f_all, f_print=f_pr, n=n, n_printed=len(printed),
                     n_win_actual=len(actual), fill_frac_mean=(sum(f.fill_frac for f in actual) / len(actual)) if actual else 1.0,
                     sigma_mean=(sum(f.sigma for f in fills) / n) if n else 0.0,
                     margin_mean=(sum(f.margin for f in fills) / n) if n else 0.0,
                     delta_mean=d_mean, delta_sd=d_sd, delta_median=d_med, deltas=deltas)


@dataclass
class CurveTable:
    nodes: dict[str, FillCurve]          # every node with data (full cells and their ancestors)
    cells: dict[str, tuple[str, int]]    # full cell key -> (node key it resolves to, pooling level)
    n_min: int

    def resolve(self, seg: Segment) -> tuple[FillCurve | None, int]:
        """The node a segment resolves to: first on its ladder with n >= n_min, else the global node
        (whatever its count), else None when there is no data at all."""
        for s in seg.ladder():
            c = self.nodes.get(s.key)
            if c is not None and (c.n >= self.n_min or s == GLOBAL):
                return c, s.level
        g = self.nodes.get(GLOBAL.key)
        return (g, GLOBAL.level) if g is not None else (None, -1)


def fit_curves(fills: Iterable[Fill], cfg: EdgeConfig) -> CurveTable:
    grid = cfg.grid()
    members: dict[str, list[Fill]] = {}
    segs: dict[str, Segment] = {}
    full_cells: set[str] = set()
    for f in fills:
        cell = f.segment(cfg.sigma_cuts)
        full_cells.add(cell.key)
        for node in cell.ladder():
            members.setdefault(node.key, []).append(f)
            segs[node.key] = node
    nodes = {k: empirical_curve(segs[k], v, grid) for k, v in members.items()}
    table = CurveTable(nodes=nodes, cells={}, n_min=cfg.n_min)
    for k in sorted(full_cells):
        node, level = table.resolve(Segment.parse(k))
        table.cells[k] = (node.key if node else "", level)
    return table
