"""Point-in-time combo fairs from the marginal layer, for markouts and the measured curse.

``compute_game(prefix, at_ms)`` never sees quotes after ``at_ms`` and applies the layer's staleness
and in-play rules against it, so a leg whose book stopped (game started, feed gap) comes back with
tier ``skip`` and the combo fair is None for that reference. Three references per combo:
  model  : product of blended leg fairs (what quote assembly prices on)
  poly   : product of Polymarket-only leg fairs
  kalshi : product of Kalshi-only leg fairs, only when every leg is matched (exact | rules_differ)
Legs of one combo are multiplied (independence); same-game combos get the same treatment and are
flagged, since the joint engine is the NFL session's and the move of the product is still the
move we want to see.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Sequence

from .config import EdgeConfig

log = logging.getLogger(__name__)
KALSHI_OK = ("exact", "rules_differ")


@dataclass(frozen=True)
class LegFair:
    position_id: str
    p: float | None
    p_poly: float | None
    p_kalshi: float | None
    err: float | None
    tier: str
    kalshi_confidence: str


@dataclass(frozen=True)
class ComboFair:
    model: float | None
    poly: float | None
    kalshi: float | None
    sigma_m: float | None        # u_m-style uncertainty of the model product (p * sqrt(sum (err/p)^2))
    n_legs: int
    n_found: int                 # legs the marginal layer returned at all
    n_kalshi: int                # legs with a usable Kalshi fair

    def ref(self, name: str) -> float | None:
        return getattr(self, name)


def _product(xs: Sequence[float | None]) -> float | None:
    out = 1.0
    for x in xs:
        if x is None:
            return None
        out *= x
    return out


class FairSource:
    """Caches ``compute_game`` per (game prefix, minute bucket). A failed call (sqlite lock, feed gap) is
    NOT cached: it returns {} for this call and is retried next time, so one transient error cannot
    poison every markout, curse and wallet observation of that (game, minute) for the process lifetime."""

    def __init__(self, cfg: EdgeConfig | None = None, *, compute: Callable | None = None, bucket_ms: int = 60_000):
        self.cfg = cfg or EdgeConfig()
        self.bucket_ms = bucket_ms
        self._compute = compute or self._default_compute
        self._cache: dict[tuple[str, int], dict[str, LegFair]] = {}
        self.calls = 0
        self.failures = 0

    def _default_compute(self, prefix: str, at_ms: int) -> dict[str, LegFair]:
        from ..marginals import compute_game
        out: dict[str, LegFair] = {}
        for lm in compute_game(prefix, at_ms, books_db=self.cfg.books_db, rfq_db=self.cfg.rfq_db,
                               kalshi_mode=self.cfg.kalshi_mode, kalshi_db=self.cfg.kalshi_db):
            mg = lm.marginal
            out[lm.quote.position_id] = LegFair(lm.quote.position_id, mg.p, mg.p_poly, mg.p_kalshi, mg.err, mg.tier,
                                                mg.kalshi_confidence or "none")
        return out

    def legs(self, prefix: str, at_ms: int) -> dict[str, LegFair]:
        key = (prefix, at_ms // self.bucket_ms)
        hit = self._cache.get(key)
        if hit is None:
            self.calls += 1
            try:
                hit = self._compute(prefix, at_ms)
            except Exception as e:
                self.failures += 1
                log.warning("compute_game(%s, %d) failed (not cached, will retry): %s: %s", prefix, at_ms, type(e).__name__, e)
                return {}
            self._cache[key] = hit
        return hit

    def combo(self, position_ids: Sequence[str], prefixes: Sequence[str | None], at_ms: int) -> ComboFair:
        ps: list[float | None] = []
        polys: list[float | None] = []
        kals: list[float | None] = []
        errs: list[tuple[float, float]] = []
        found = 0
        n_k = 0
        for pid, prefix in zip(position_ids, prefixes):
            lf = self.legs(prefix, at_ms).get(pid) if prefix else None
            if lf is None:
                ps.append(None); polys.append(None); kals.append(None)
                continue
            found += 1
            usable = lf.tier != "skip" and lf.p is not None
            ps.append(lf.p if usable else None)
            polys.append(lf.p_poly if usable and lf.p_poly is not None else None)
            k_ok = usable and lf.p_kalshi is not None and lf.kalshi_confidence in KALSHI_OK
            kals.append(lf.p_kalshi if k_ok else None)
            n_k += int(k_ok)
            if usable and lf.err is not None and lf.p:
                errs.append((lf.p, lf.err))
        model = _product(ps)
        sigma = None
        if model is not None and errs:
            sigma = model * math.sqrt(sum((e / max(p, 1e-9)) ** 2 for p, e in errs))
        return ComboFair(model=model, poly=_product(polys), kalshi=_product(kals), sigma_m=sigma,
                         n_legs=len(position_ids), n_found=found, n_kalshi=n_k)
