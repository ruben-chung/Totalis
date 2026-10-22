"""margin_table.json and its loader: ``margin_for(segment, requester=...) -> MarginTerms``.

Quote-time rule (quoting replaces ``QUOTE_BASE_EDGE`` and the unit uncertainty multipliers):
    margin = terms.base + model_curse(u_m + u_d, terms.delta, terms.m_star) + terms.info_premium + terms.wallet_premium
    skip the RFQ when terms.blocked
``terms.margin(sigma)`` computes it; a node without ``delta`` / ``m_star`` (older table) uses the
linear slope ``terms.curse_mult * sigma`` instead, and both give exactly m* at the node's sigma.
The table is cached per path and re-read when its mtime changes (checked at most once a second), so
``margin_for`` is cheap enough for the RFQ rate. The table lives at ``data/margin_table.json`` by
default (``EDGE_TABLE_PATH``): a runtime artifact, never in the source tree.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import EdgeConfig
from .curse import model_curse
from .segments import GLOBAL, Segment

log = logging.getLogger(__name__)
DEFAULT_TABLE = EdgeConfig().table_path
TABLE_VERSION = 1


@dataclass(frozen=True)
class MarginTerms:
    base: float
    curse_mult: float
    info_premium: float
    wallet_premium: float
    blocked: bool
    segment: str = ""          # node the margin came from ("" = default, no table)
    level: int = -1            # pooling level of that node (-1 = default)
    n: int = 0
    source: str = "default"    # model | measured | no_prints | no_data | default
    block_reason: str = ""
    delta: float | None = None      # node's fitted competitor gap (model curse at quote time needs it)
    m_star: float | None = None     # node's optimal markup at its own sigma

    def curse(self, sigma: float) -> float:
        """Expected winner's-curse loss at the RFQ's sigma: the model curse when the node carries delta
        and m*, else the linear ``curse_mult * sigma`` extrapolation."""
        sigma = max(0.0, sigma)
        if self.delta is not None and self.m_star is not None:
            return model_curse(sigma, self.delta, self.m_star)
        return self.curse_mult * sigma

    def margin(self, sigma: float) -> float:
        return self.base + self.curse(sigma) + self.info_premium + self.wallet_premium


class MarginTable:
    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self.data = data
        self.path = path
        self.nodes: dict[str, dict[str, Any]] = data.get("nodes", {})
        self.wallets: dict[str, dict[str, Any]] = data.get("wallets", {})
        self.default_edge: float = float(data.get("default_edge", 0.005))

    @property
    def fitted_at_ms(self) -> int:
        return int(self.data.get("fitted_at_ms", 0))

    def resolve(self, seg: Segment) -> dict[str, Any] | None:
        for s in seg.ladder():
            node = self.nodes.get(s.key)
            if node is not None:
                return node
        return self.nodes.get(GLOBAL.key)

    def terms(self, seg: Segment, requester: str | None = None, now_ms: int | None = None) -> MarginTerms:
        node = self.resolve(seg)
        w = self.wallets.get(requester or "", {}) if requester else {}
        blocked = bool(w.get("blocked"))
        until = w.get("blocked_until_ms")
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if blocked and until is not None and until < now and w.get("reason") != "manual":
            blocked = False   # expired since the fit
        premium = 0.0 if blocked else float(w.get("premium", 0.0) or 0.0)
        if node is None:
            return MarginTerms(self.default_edge, 0.0, 0.0, premium, blocked, source="default", block_reason=w.get("reason", "") if blocked else "")
        if node.get("source") in ("no_prints", "no_data"):
            return MarginTerms(self.default_edge, 0.0, float(node.get("info_premium", 0.0)), premium, blocked, node["segment"],
                               int(node.get("level", -1)), int(node.get("n", 0)), node["source"], w.get("reason", "") if blocked else "")
        return MarginTerms(float(node["base"]), float(node["curse_mult"]), float(node.get("info_premium", 0.0)), premium, blocked,
                           node["segment"], int(node.get("level", -1)), int(node.get("n", 0)), node.get("source", "model"),
                           w.get("reason", "") if blocked else "",
                           delta=None if node.get("delta") is None else float(node["delta"]),
                           m_star=None if node.get("m_star") is None else float(node["m_star"]))


def write_table(path: Path, nodes: list[dict[str, Any]], wallets: dict[str, dict[str, Any]], cfg: EdgeConfig, *,
                fitted_at_ms: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {"version": TABLE_VERSION, "fitted_at_ms": fitted_at_ms, "default_edge": cfg.default_edge,
            "n_min": cfg.n_min, "sigma_cuts": list(cfg.sigma_cuts), "min_fill_rate": cfg.min_fill_rate,
            "curse_source": cfg.curse_source, "grid_step": cfg.grid_step, "grid_max": cfg.grid_max,
            "rule": "margin = base + model_curse(u_m + u_d, delta, m_star) + info_premium + wallet_premium; skip when blocked"
                    " (curse_mult * (u_m + u_d) when a node has no delta / m_star)",
            "nodes": {n["segment"]: n for n in nodes},
            "wallets": {pid: {"premium": round(w["premium"], 6), "blocked": bool(w["blocked"]), "blocked_until_ms": w.get("blocked_until_ms"),
                              "reason": w.get("reason", ""), "n": w.get("n", 0), "shrunk": round(w.get("shrunk", 0.0), 6)}
                        for pid, w in wallets.items() if w.get("blocked") or (w.get("premium") or 0) > 0},
            **(extra or {})}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return data


_cache: dict[str, tuple[float, float, MarginTable | None]] = {}   # path -> (mtime, checked_at, table)


def load_table(path: str | Path | None = None, *, max_age_s: float = 1.0) -> MarginTable | None:
    p = Path(path or DEFAULT_TABLE)
    key = str(p)
    now = time.time()
    hit = _cache.get(key)
    if hit is not None and now - hit[1] < max_age_s:
        return hit[2]
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        _cache[key] = (0.0, now, None)
        return None
    if hit is not None and hit[0] == mtime:
        _cache[key] = (mtime, now, hit[2])
        return hit[2]
    try:
        table = MarginTable(json.loads(p.read_text()), p)
    except Exception as e:
        log.warning("margin table %s is malformed (%s: %s); quoting falls back to the default edge", p, type(e).__name__, e)
        table = None
    _cache[key] = (mtime, now, table)
    return table


def margin_for(segment: Segment | str, *, requester: str | None = None, table: MarginTable | None = None,
               path: str | Path | None = None, default_edge: float = 0.005, now_ms: int | None = None) -> MarginTerms:
    """Margin terms for a segment (``Segment`` or its ``sport|n_legs|direction|structure|sigma`` key),
    walking the pooling ladder to the deepest node the table holds. Without a table: the default edge."""
    seg = Segment.parse(segment) if isinstance(segment, str) else segment
    t = table if table is not None else load_table(path)
    if t is None:
        return MarginTerms(default_edge, 0.0, 0.0, 0.0, False)
    return t.terms(seg, requester, now_ms)
