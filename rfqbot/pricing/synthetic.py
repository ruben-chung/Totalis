"""Synthetic NFL games with a known truth, for tests and the --synthetic CLI demo.

Truth: (H, A) full game ~ discretised bivariate normal; (H1, A1) first half ~ the same with
means halved and sds scaled by 1/sqrt(2), same correlation. Quotes are generated for the
exact ladder shapes Polymarket lists (lines copied from nfl-dal-phi-2026-10-27 on 2026-10-20),
priced at the true probability with a bid/ask spread and optional noise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..sports import nfl_teams
from .joint_nfl import GRID, M_IDX, T_IDX, _A_IDX, _H_IDX, bivariate_normal_table
from .quotes import Quote

SPREAD_LINES = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 13.5, 14.5, 16.5, 17.5, 19.5, 20.5, 21.5]
TOTAL_LINES = [24.5 + 2 * i for i in range(21)]
TEAM_TOTAL_LINES = [10.5, 12.5, 13.5, 16.5, 19.5, 20.5, 23.5, 26.5, 27.5, 30.5, 34.5]
H1_SPREAD_LINES = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 13.5, 14.5, 16.5, 17.5, 20.5, 21.5]
H1_TOTAL_LINES = [16.5, 19.5, 22.5, 25.5, 28.5]
H1_TEAM_TOTAL_LINES = [6.5, 10.5, 12.5, 13.5, 16.5, 17.5, 19.5, 20.5]


@dataclass
class Truth:
    fg: np.ndarray      # GRID x GRID over (H, A)
    h1: np.ndarray
    mu_h: float
    mu_a: float
    sd_h: float
    sd_a: float
    rho: float

    def p(self, period: str, var: str, gt: float) -> float:
        """P(var > gt) under the truth. var in H A M T. 2H uses the 1H table (iid increments)."""
        t = self.fg if period == "FG" else self.h1
        v = {"H": _H_IDX.ravel(), "A": _A_IDX.ravel(), "M": M_IDX - (GRID - 1), "T": T_IDX}[var]
        return float(t.ravel()[v > gt].sum())

    def corr(self, period: str, a: str, b: str) -> float:
        t = (self.fg if period == "FG" else self.h1).ravel()
        vv = {"H": _H_IDX.ravel(), "A": _A_IDX.ravel(), "M": M_IDX - (GRID - 1), "T": T_IDX}
        va, vb = vv[a].astype(float), vv[b].astype(float)
        ma, mb = np.dot(va, t), np.dot(vb, t)
        return float(np.dot((va - ma) * (vb - mb), t) / math.sqrt(np.dot((va - ma) ** 2, t) * np.dot((vb - mb) ** 2, t)))


def make_truth(mu_h: float = 24.0, mu_a: float = 21.0, sd_h: float = 10.0, sd_a: float = 9.0, rho: float = 0.3) -> Truth:
    """Halves are iid discretised bivariate normals (truncated at 0); the full game is their
    convolution, so FG = 1H + 2H holds exactly on the grid (means, sds and correlation)."""
    cov = rho * sd_h * sd_a
    k = 1 / math.sqrt(2)
    h1, _ = bivariate_normal_table(mu_h / 2, mu_a / 2, (sd_h * k) ** 2, (sd_a * k) ** 2, cov * k * k)
    n = 2 * GRID - 1
    f = np.fft.rfft2(h1, s=(n, n))
    fg = np.fft.irfft2(f * f, s=(n, n))[:GRID, :GRID]
    fg = np.clip(fg, 0, None)
    fg /= fg.sum()
    return Truth(fg, h1, mu_h, mu_a, sd_h, sd_a, rho)


def quotes_from_truth(truth: Truth, *, game: str = "nfl-dal-phi-2026-10-27", spread: float = 0.02,
                      noise_sd: float = 0.0, seed: int = 0, team_totals: bool = True, first_half: bool = True,
                      second_half: bool = False, size: float = 500.0, at_ms: int = 1_000_000) -> list[Quote]:
    rng = np.random.default_rng(seed)
    _, away_code, home_code, date = game.split("-", 3)
    away, home = nfl_teams.by_poly_slug(away_code), nfl_teams.by_poly_slug(home_code)
    out: list[Quote] = []
    n = 0

    def add(slug: str, title: str, outcomes: tuple[str, str], p_first: float) -> None:
        nonlocal n
        for i, (o, p) in enumerate(zip(outcomes, (p_first, 1 - p_first))):
            mid = float(np.clip(p + rng.normal(0, noise_sd) if noise_sd else p, spread / 2 + 1e-3, 1 - spread / 2 - 1e-3))
            n += 1
            out.append(Quote(position_id=f"{n}", token_id=f"tok{n}", slug=slug, title=title, outcome=o, outcome_index=i,
                             best_bid=round(mid - spread / 2, 4), best_ask=round(mid + spread / 2, 4),
                             bid_size=size, ask_size=size, at_ms=at_ms, game_start_time=f"{date} 17:00:00+00"))

    def ladders(period: str, pfx: str, tpfx: str, sl, tl, ttl) -> None:
        for x in sl:
            n_ = f"{int(x)}pt5"
            add(f"{game}-{pfx}spread-home-{n_}", f"{tpfx}Spread: {home.nickname} (-{x})", (home.nickname, away.nickname), truth.p(period, "M", x))
            add(f"{game}-{pfx}spread-away-{n_}", f"{tpfx}Spread: {away.nickname} (-{x})", (away.nickname, home.nickname), 1 - truth.p(period, "M", -x))
        for x in tl:
            add(f"{game}-{pfx}total-{int(x)}pt5", f"{away.nickname} vs. {home.nickname}: {tpfx}O/U {x}", ("Over", "Under"), truth.p(period, "T", x))
        if team_totals:
            for x in ttl:
                add(f"{game}-{pfx}team-total-{home_code}-{int(x)}pt5", f"{home.nickname} {tpfx}Team Total: O/U {x}", ("Over", "Under"), truth.p(period, "H", x))
                add(f"{game}-{pfx}team-total-{away_code}-{int(x)}pt5", f"{away.nickname} {tpfx}Team Total: O/U {x}", ("Over", "Under"), truth.p(period, "A", x))

    ml = truth.p("FG", "M", 0) + 0.5 * (truth.p("FG", "M", -0.5) - truth.p("FG", "M", 0.5))
    add(game, f"{away.nickname} vs. {home.nickname}", (away.nickname, home.nickname), 1 - ml)
    ladders("FG", "", "", SPREAD_LINES, TOTAL_LINES, TEAM_TOTAL_LINES)
    if first_half:
        ml1 = truth.p("1H", "M", 0) + 0.5 * (truth.p("1H", "M", -0.5) - truth.p("1H", "M", 0.5))
        add(f"{game}-1h-moneyline", f"{away.nickname} vs. {home.nickname}: 1H Moneyline", (away.nickname, home.nickname), 1 - ml1)
        ladders("1H", "1h-", "1H ", H1_SPREAD_LINES, H1_TOTAL_LINES, H1_TEAM_TOTAL_LINES)
    if second_half:   # same shapes as the first half (iid increments in the synthetic truth)
        ladders("2H", "2h-", "2H ", H1_SPREAD_LINES, H1_TOTAL_LINES, H1_TEAM_TOTAL_LINES)
    return out
