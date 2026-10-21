"""Marginal-layer constants, overridable from the environment (MARGINAL_* keys).

Kept in this package so the shared rfqbot/config.py does not need editing. Every number below is
a first-cut placeholder for the backtest to calibrate; the formula they feed is documented in
model.py. Units: probabilities are fractions 0..1 (so 0.01 is one probability point), depth and
sizes are shares, time is seconds.

    sigma_v = half_spread_v * (1 + C_THIN / (1 + depth_v / D0)) + c_stale * staleness_v
    c_stale = C_STALE_PREGAME before kickoff, C_STALE_INPLAY once game_start_time <= now
    staleness_v = seconds since the quote was last confirmed (its row, or the feed's last activity
                  while the instrument was subscribed: both stores are change-only)
    precision_v = 1 / sigma_v^2  (x KALSHI_PRIOR[sport] for Kalshi, x RULES_DIFFER_SCALE if rules differ)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class MarginalConfig:
    # --- sigma: spread term ---
    c_thin: float = 1.0            # spread inflation when depth -> 0 (sigma doubles at zero depth)
    d0: float = 500.0              # shares at which the thinness inflation halves
    # --- sigma: staleness term (probability per second of quote age) ---
    c_stale_pregame: float = 3e-6  # ~1 pt per hour before kickoff
    c_stale_inplay: float = 1e-3   # ~1 pt per 10 s in play
    # --- staleness gates (seconds): warn -> flag stale:<venue>; drop -> venue weight 0 ---
    stale_warn_pregame_s: float = 600.0
    stale_drop_pregame_s: float = 3600.0
    stale_warn_inplay_s: float = 15.0
    stale_drop_inplay_s: float = 60.0
    inplay_fresh_s: float = 15.0   # in play, tier is at least `stale` unless the newest quote is younger than this
    # --- notes ---
    k_wide: float = 0.2            # wide:<venue> when spread > k_wide * p(1-p) + wide_floor
    wide_floor: float = 0.02
    d_min: float = 50.0            # thin:<venue> when depth < d_min shares
    # --- venue prior and match confidence ---
    kalshi_prior: dict[str, float] = field(default_factory=lambda: {"nfl": 2.0, "nba": 1.0})
    kalshi_prior_default: float = 1.0
    rules_differ_scale: float = 0.25
    # --- combination / error ---
    err_floor: float = 0.005
    degenerate_lo: float = 0.015
    degenerate_hi: float = 0.985
    degenerate_err_floor: float = 0.02
    one_sided_sigma: float = 0.15  # sigma for a lone one-sided venue (only the present side is known)
    err_cap: float = 0.5           # err is an absolute probability error; beyond this it carries no information
    gap_skip: float = 0.10         # |p_poly - p_kalshi| beyond this -> disagree_hard, tier skip
    tier_w_min: float = 0.05       # a venue's wide/thin/stale flags set the tier only if its weight is at least this

    def prior_for(self, sport: str | None) -> float:
        return self.kalshi_prior.get((sport or "").lower(), self.kalshi_prior_default)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


_FLOAT_KEYS = (
    "c_thin", "d0", "c_stale_pregame", "c_stale_inplay", "stale_warn_pregame_s", "stale_drop_pregame_s",
    "stale_warn_inplay_s", "stale_drop_inplay_s", "inplay_fresh_s", "k_wide", "wide_floor", "d_min",
    "kalshi_prior_default", "rules_differ_scale", "err_floor", "degenerate_lo", "degenerate_hi",
    "degenerate_err_floor", "one_sided_sigma", "err_cap", "gap_skip", "tier_w_min",
)


def load_config(env_file: str | os.PathLike | None = ".env", env: dict[str, str] | None = None) -> MarginalConfig:
    """Defaults overridden by MARGINAL_<UPPER_KEY> variables; MARGINAL_KALSHI_PRIOR is `nfl=2,nba=1`."""
    if env is None:
        if env_file is not None and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)
    cfg = MarginalConfig()
    overrides: dict = {}
    for key in _FLOAT_KEYS:
        raw = env.get(f"MARGINAL_{key.upper()}", "").strip()
        if raw:
            overrides[key] = float(raw)
    raw = env.get("MARGINAL_KALSHI_PRIOR", "").strip()
    if raw:
        prior = dict(cfg.kalshi_prior)
        for item in raw.split(","):
            if not item.strip():
                continue
            sport, _, val = item.partition("=")
            prior[sport.strip().lower()] = float(val)
        overrides["kalshi_prior"] = prior
    return replace(cfg, **overrides) if overrides else cfg
