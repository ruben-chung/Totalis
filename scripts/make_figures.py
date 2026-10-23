#!/usr/bin/env python
"""Regenerate the two figures in Summary.md (needs the `figures` extra: matplotlib).

    .venv/bin/python scripts/make_figures.py grid nfl-cin-bal-2026-10-25   # img/joint-grid-<game>.png
    .venv/bin/python scripts/make_figures.py fill                           # img/fill-curve.png

`grid` builds the joint score model of one NFL game from the recorded ladders (data/books.db) and
draws the fitted probability of each final score next to its departure from independence.
`fill` reads the latest margin-table fit (data/edge.db) and draws the fill curves and the
expected edge per printed RFQ for four pooled segments.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from math import erf, exp, pi, sqrt
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "img"
INK, MUTED, GRID = "#1a1a19", "#6b6b67", "#d9d8d4"
SEQ = ["#ffffff", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]
DIV = ["#256abf", "#86b6ef", "#f0efec", "#ec9a8a", "#c0392b"]
CAT = ["#3987e5", "#e8863a", "#2e9e6b", "#8a63d2"]


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def smooth(z: np.ndarray, k: int = 3) -> np.ndarray:
    pad = np.pad(z, k // 2, mode="edge")
    out = np.zeros_like(z)
    for i in range(k):
        for j in range(k):
            out += pad[i:i + z.shape[0], j:j + z.shape[1]] / (k * k)
    return out


def grid(prefix: str, n_show: int = 57) -> Path:
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

    from rfqbot.ladders import game_markets
    from rfqbot.pricing.joint import build_game

    plt = _plt()
    model = build_game(game_markets(prefix))
    fg = model.periods["FG"]
    table = fg.table
    away, home = model.game_key.split("|")[-1].split("@")
    h, a = table.sum(axis=1), table.sum(axis=0)
    joint = smooth(table)[:n_show, :n_show] * 100
    diff = smooth(table - np.outer(h, a))[:n_show, :n_show] * 100
    ii, jj = np.meshgrid(np.arange(table.shape[0]), np.arange(table.shape[1]), indexing="ij")
    mh, ma = (ii * table).sum(), (jj * table).sum()
    corr = ((ii - mh) * (jj - ma) * table).sum() / np.sqrt((((ii - mh) ** 2) * table).sum() * (((jj - ma) ** 2) * table).sum())
    m_margin, m_total = fg.means["M"], fg.means["T"]

    fig, axes = plt.subplots(1, 2, figsize=(13, 6.2), dpi=160)
    panels = [
        (axes[0], joint, LinearSegmentedColormap.from_list("seq", SEQ), None,
         "Market-implied probability of each final score", "% probability per score pair"),
        (axes[1], diff, LinearSegmentedColormap.from_list("div", DIV),
         TwoSlopeNorm(vcenter=0, vmin=-np.abs(diff).max(), vmax=np.abs(diff).max()),
         "Joint minus independence (product of team marginals)", "percentage points"),
    ]
    xs = np.arange(n_show)
    for ax, z, cmap, norm, title, label in panels:
        im = ax.imshow(z, origin="lower", cmap=cmap, norm=norm, interpolation="nearest", aspect="equal")
        ax.set_xlabel(f"{away} points", color=INK)
        ax.set_ylabel(f"{home} points", color=INK)
        ax.set_title(title, color=INK, fontsize=11, loc="left", pad=10)
        ax.plot(xs, xs + m_margin, color=MUTED, lw=1, ls="--")
        ax.text(n_show - 3, n_show - 3 + m_margin - 6, f"margin = spread ({home} {m_margin:+.1f})", color=MUTED, fontsize=8, ha="right", va="top")
        ax.plot(xs, m_total - xs, color=MUTED, lw=1, ls=":")
        ax.text(2, m_total - 3, f"total = {m_total:.1f}", color=MUTED, fontsize=8, ha="left", va="top", rotation=-45)
        ax.set_xlim(-0.5, n_show - 0.5)
        ax.set_ylim(-0.5, n_show - 0.5)
        ax.set_xticks(range(0, n_show, 7))
        ax.set_yticks(range(0, n_show, 7))
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.tick_params(colors=MUTED, length=0)
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
        cb.set_label(label, color=MUTED, fontsize=9)
        cb.ax.tick_params(colors=MUTED, length=0)
        cb.outline.set_edgecolor(GRID)
    fig.suptitle(f"{away} at {home}: full-game joint score grid fitted from the spread, total and team-total ladders "
                 f"(correlation of the two team scores {corr:+.2f})", color=INK, fontsize=10, x=0.01, ha="left")
    fig.text(0.01, 0.012, f"Fit is per integer score (81 x 81), shown to {n_show - 1} with a 3-point box average for display. "
             "Dashed: margin equals the market spread. Dotted: total equals the market total. Pre-game Polymarket books.",
             color=MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    out = IMG / f"joint-grid-{prefix.split('-', 1)[1].rsplit('-', 3)[0]}.png"
    IMG.mkdir(exist_ok=True)
    fig.savefig(out, facecolor="white")
    return out


def _curse(m: float, delta: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.0
    z = (delta - m) / sigma
    big_phi = max(0.5 * (1 + erf(z / sqrt(2))), 1e-9)
    return sigma * exp(-z * z / 2) / sqrt(2 * pi) / big_phi


def fill(edge_db: Path = ROOT / "data" / "edge.db", table_path: Path = ROOT / "data" / "margin_table.json") -> Path:
    plt = _plt()
    conn = sqlite3.connect(f"file:{edge_db}?mode=ro", uri=True)
    fit_ms = conn.execute("select max(fitted_at_ms) from curves").fetchone()[0]
    curves = {n: json.loads(j) for n, j in conn.execute("select node, json from curves where fitted_at_ms=?", (fit_ms,))}
    table = json.load(open(table_path))["nodes"]
    nodes = [("nba|2|BUY|disjoint|s0", "NBA, 2 legs, different games, tight fair"),
             ("nba|2|BUY|disjoint|*", "NBA, 2 legs, different games, all"),
             ("nba|*|BUY|*|*", "NBA, all BUY requests"),
             ("*|*|BUY|*|*", "all sports, all BUY requests")]
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.4), dpi=160)
    for (node, label), col in zip(nodes, CAT):
        if node not in curves or node not in table:
            continue
        cv, t = curves[node], table[node]
        g = np.array(cv["grid"]) * 100
        fp = np.array(cv["f_print"])
        axes[0].plot(g, fp, color=col, lw=2, label=f"{label}  (n={cv['n']:,}, printed {cv['n_printed']})")
        ev = np.array([fp[i] * (cv["grid"][i] - _curse(cv["grid"][i], t["delta"], t["sigma"])) for i in range(len(g))]) * 100
        axes[1].plot(g, ev, color=col, lw=2, label=label)
        k = int(np.argmax(ev))
        axes[1].plot(g[k], ev[k], "o", color=col, ms=8, mec="white", mew=1.5)
    axes[0].set_title("Fill curve: P(we win | the RFQ traded) against our margin", loc="left", color=INK, fontsize=11, pad=10)
    axes[0].set_xlabel("margin over fair (pt)", color=INK)
    axes[0].set_ylabel("share of printed RFQs we would have won", color=INK)
    axes[0].set_ylim(0, 1)
    axes[1].set_title("Expected edge per printed RFQ: P(win) x (margin - winner's curse)", loc="left", color=INK, fontsize=11, pad=10)
    axes[1].set_xlabel("margin over fair (pt)", color=INK)
    axes[1].set_ylabel("pt per printed RFQ", color=INK)
    axes[1].axhline(0, color=GRID, lw=1)
    for ax in axes:
        ax.set_xlim(0, 10)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ax.legend(frameon=False, fontsize=8.5, labelcolor=INK)
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.tick_params(colors=MUTED, length=0)
    when = dt.datetime.fromtimestamp(fit_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fig.suptitle(f"Margin table fit of {when}: counterfactual fills of our paper quotes against recorded prints, pooled up the segment ladder",
                 color=INK, fontsize=10, x=0.01, ha="left")
    fig.text(0.01, 0.012, "Win rule: our margin at or below the gap between the print and our fair. Curves are isotonic (non-increasing) fits. "
             "Dots mark the optimiser's margin. Per request, P(win) is about 100x smaller because 99% of RFQs never trade.",
             color=MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0.05, 1, 0.94))
    out = IMG / "fill-curve.png"
    IMG.mkdir(exist_ok=True)
    fig.savefig(out, facecolor="white")
    return out


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "grid":
        print(grid(argv[1]))
        return 0
    if argv[:1] == ["fill"]:
        print(fill())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
