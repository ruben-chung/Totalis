"""BacktestConfig: one run's switches, JSON-stored with the run.

Every switch of claude-briefs/backtest-brief.md deliverable 5 is a field here; ``quote`` holds QuoteConfig overrides
(applied on top of the live config from ``load_quote_config``), so a run is fully described by this object.

    pricing      joint | independence      same-game clusters priced by the joint engine or as the product of marginals
    calibration  fitted | ones | <float>   joint engine calibration: the fitted file, identity, or fitted lambdas x factor
    shock_scale  1.0 | 0.5 | 2.0           dependence shock set (Shock.value x scale; bootstrap seeds unchanged); drives u_d
    table        none | repo | <path>      margin table; none = the stage 2 formula k_m u_m + k_d u_d + base_edge
    table_policy after_fit | always        after_fit: the table only prices RFQs at or after its fitted_at_ms (no lookahead)
    fill_rule    win | strict              which fill flag moves the book (ties count as wins; strict needs a full tick inside the print)
    quote        {"base_edge": 0.01, "risk": false, ...}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..quoting.config import QuoteConfig, load_quote_config


@dataclass(frozen=True)
class BacktestConfig:
    name: str = "base"
    quote: dict[str, Any] = field(default_factory=dict)
    pricing: str = "joint"
    calibration: str | float = "fitted"
    shock_scale: float = 1.0
    table: str = "repo"
    table_policy: str = "after_fit"
    fill_rule: str = "win"            # win | strict: which fill flag drives the book (both are always recorded)
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)

    @classmethod
    def from_json(cls, s: str) -> "BacktestConfig":
        d = json.loads(s)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def quote_config(self, base: QuoteConfig | None = None) -> QuoteConfig:
        """The live QuoteConfig (env / .env) with this run's overrides. ``table`` decides ``edge`` and ``edge_table``."""
        q = base if base is not None else load_quote_config()
        over = dict(self.quote)
        if self.table == "none":
            over.setdefault("edge", False)
        else:
            over.setdefault("edge", True)
            if self.table != "repo":
                over.setdefault("edge_table", Path(self.table))
        if "decisions_db" not in over:
            over["decisions_db"] = Path("/dev/null")        # the backtest never writes a live decisions log
        for k in ("edge_table", "decisions_db"):
            if k in over and over[k] is not None:
                over[k] = Path(over[k])
        if "tier_size_mult" in over:
            over["tier_size_mult"] = dict(over["tier_size_mult"])
        if "legs_size_mult" in over:
            over["legs_size_mult"] = {int(k): v for k, v in over["legs_size_mult"].items()}
        return replace(q, **over)


@dataclass(frozen=True)
class WindowConfig:
    """What is replayed and how the clock-driven parts behave (shared by every strategy in one pass)."""
    start_ms: int
    end_ms: int
    refresh_idle_ms: int = 300_000       # a game is refreshed on cadence only while it received an RFQ this recently
    risk_refresh_ms: int | None = None   # None -> QuoteConfig.risk_refresh_sec
    expiry_grace_ms: int = 20_000        # listener rule: deadline + acceptance + last look + grace (see Assembler.horizon_ms)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)
