"""Quote assembly configuration (env ``QUOTE_<KEY>``; all values placeholders for the backtest)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class QuoteConfig:
    base_edge: float = 0.005                # e_b, probability points (flow clears +0.5..0.75 pt over fair; backtest calibrates)
    k_m: float = 1.0                        # multiplier on marginal uncertainty u_m
    k_d: float = 1.0                        # multiplier on dependence uncertainty u_d
    inventory_skew: float = 0.0             # constant added to e_i (stage 3: e_i itself is k_inv * dES1/ds * usage)
    risk: bool = True                       # caps, Kelly sizing and inventory skew on (QUOTE_RISK=0 to disable)
    kelly_fraction: float = 0.25            # size <= fraction x capital x margin / (p(1-p)) shares
    k_inv: float = 0.04                     # e_i = k_inv x dES1/ds (loss per share in the tail) x outcome-cap usage; 1 pt at corr 1, usage 0.5
    risk_refresh_sec: float = 2.0           # live book / exposure rebuild period (refresh thread)
    edge: bool = True                       # margin from rfqbot.edge's fitted table (QUOTE_EDGE=0: base_edge + k_m u_m + k_d u_d)
    edge_table: Path | None = None          # margin table path (default rfqbot/edge/margin_table.json); reloaded on the refresh thread
    max_size_usd: float = 500.0             # per quote, notional at our price
    min_size_usd: float = 5.0               # below this: skip
    tick: float = 0.001                     # price rounding, away from fair
    price_min: float = 0.001
    price_max: float = 0.999
    buy_safety_max: float = 0.90909         # docs: BUY quotes above this are rejected
    tier_size_mult: dict[str, float] = field(default_factory=lambda: {
        "ok": 1.0, "thin": 0.5, "wide": 0.5, "stale": 0.25, "disagree": 0.1, "skip": 0.0})
    legs_size_mult: dict[int, float] = field(default_factory=lambda: {1: 1.0, 2: 1.0, 3: 0.5, 4: 0.25})  # >4: last
    refresh_sec: float = 20.0               # game cache refresh period
    model_rebuild_move: float = 0.005       # rebuild the joint model when any informative mid moved this much
    max_games: int = 400                    # cache size
    decisions_db: Path = Path("./data/decisions.db")
    quote_sports: tuple[str, ...] = ("nfl", "nba")   # sports with ladders in the book recorder

    def legs_mult(self, n: int) -> float:
        return self.legs_size_mult.get(n, self.legs_size_mult[max(self.legs_size_mult)])

    def to_json(self) -> str:
        d = asdict(self)
        d["decisions_db"] = str(self.decisions_db)
        d["edge_table"] = None if self.edge_table is None else str(self.edge_table)
        return json.dumps(d, sort_keys=True, default=str)


def _f(env: dict[str, str], key: str, default: float) -> float:
    v = env.get(f"QUOTE_{key}")
    return float(v) if v not in (None, "") else default


def load_quote_config(env_file: str | os.PathLike | None = ".env", env: dict[str, str] | None = None) -> QuoteConfig:
    if env is None:
        if env_file is not None and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)
    base = QuoteConfig()
    tiers = dict(base.tier_size_mult)
    if env.get("QUOTE_TIER_SIZE_MULT"):
        for kv in env["QUOTE_TIER_SIZE_MULT"].split(","):
            k, v = kv.split("=")
            tiers[k.strip()] = float(v)
    legs = dict(base.legs_size_mult)
    if env.get("QUOTE_LEGS_SIZE_MULT"):
        for kv in env["QUOTE_LEGS_SIZE_MULT"].split(","):
            k, v = kv.split("=")
            legs[int(k)] = float(v)
    return QuoteConfig(
        base_edge=_f(env, "BASE_EDGE", base.base_edge), k_m=_f(env, "K_M", base.k_m), k_d=_f(env, "K_D", base.k_d),
        inventory_skew=_f(env, "INVENTORY_SKEW", base.inventory_skew),
        risk=(env.get("QUOTE_RISK") or "1").strip().lower() not in ("0", "false", "no", "off"),
        kelly_fraction=_f(env, "KELLY_FRACTION", base.kelly_fraction), k_inv=_f(env, "K_INV", base.k_inv),
        risk_refresh_sec=_f(env, "RISK_REFRESH_SEC", base.risk_refresh_sec),
        edge=(env.get("QUOTE_EDGE") or "1").strip().lower() not in ("0", "false", "no", "off"),
        edge_table=Path(env["QUOTE_EDGE_TABLE"]) if env.get("QUOTE_EDGE_TABLE") else None,
        max_size_usd=_f(env, "MAX_SIZE_USD", base.max_size_usd), min_size_usd=_f(env, "MIN_SIZE_USD", base.min_size_usd),
        tick=_f(env, "TICK", base.tick), price_min=_f(env, "PRICE_MIN", base.price_min), price_max=_f(env, "PRICE_MAX", base.price_max),
        buy_safety_max=_f(env, "BUY_SAFETY_MAX", base.buy_safety_max), tier_size_mult=tiers, legs_size_mult=legs,
        refresh_sec=_f(env, "REFRESH_SEC", base.refresh_sec), model_rebuild_move=_f(env, "MODEL_REBUILD_MOVE", base.model_rebuild_move),
        max_games=int(_f(env, "MAX_GAMES", base.max_games)),
        decisions_db=Path(env.get("QUOTE_DECISIONS_DB") or base.decisions_db),
        quote_sports=tuple(s.strip() for s in (env.get("QUOTE_SPORTS") or ",".join(base.quote_sports)).split(",") if s.strip()),
    )
