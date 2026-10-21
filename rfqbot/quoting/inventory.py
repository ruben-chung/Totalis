"""Live book, caps and inventory skew for the quoter (stage 3 part 3, docs/risk-wiring-brief.md).

The quoter keeps a ``Book`` of its own quotes: pending at quoted size from the moment a quote is
emitted until the listener's expiry rule (submission deadline + acceptance + last look + grace),
filled at the executed size once an execution update is in the store, settled from
``market_resolutions``. Expiries, fills and settlements are polled from the RFQ store on the
cache's refresh thread (``refresh``), never on the RFQ path.

Hot path (``assess``, ~1-2 ms): the candidate's per-share exposure is derived on the book's models
and draws; every cap is solved in closed form (outcome deltas, per-game worst loss) or linearised
and checked (ES1); the inventory skew is ``k_inv * dES1/ds * usage``. When a quote goes out
(``on_quoted``) its per-share vectors are absorbed into the hot exposure at the quoted size, so the
next RFQ for the same combo sees it. The refresh thread rebuilds the exposure from the book every
``risk_refresh_sec`` and swaps it in.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..risk.book import EXPIRED, FILLED, PENDING, Book, Position, PositionLeg
from ..risk.caps import Caps, es1_limit, game_limit, notional_limits, outcome_limit
from ..risk.exposure import Exposure, ModelPool, es_at
from .config import QuoteConfig

log = logging.getLogger(__name__)
INF = float("inf")
DEFAULT_HORIZON_MS = 10_000 + 1_000 + 20_000     # acceptance + last look + expiry grace (config.Settings defaults)


def kelly_size(margin: float, p: float, capital: float, fraction: float) -> float:
    """Shares: fraction x capital x edge / variance per share, edge = margin earned (probability
    points), variance = p(1-p) of the combo's payoff."""
    var = p * (1.0 - p)
    if margin <= 0 or var <= 1e-12:
        return 0.0
    return fraction * capital * margin / var


@dataclass
class Assessment:
    allowed_size: float                  # shares every cap allows (no request cap applied)
    binding_cap: str | None              # cap giving allowed_size when it is finite
    headroom: dict[str, float]
    detail: dict[str, str]
    d_es1_per_share: float               # dES1/ds at s -> 0 (loss per share in the book's 1% tail)
    usage: float                         # book's use of the outcome cap on outcomes the candidate adds to
    e_i: float                           # inventory skew (probability points; negative = hedge, tightens)
    pending_same_combo: int
    pending_requesters: int
    book: dict[str, Any]
    cand: Exposure | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"allowed_size": None if self.allowed_size == INF else self.allowed_size, "binding_cap": self.binding_cap,
                "headroom": {k: (None if v == INF else v) for k, v in self.headroom.items()}, "detail": self.detail,
                "d_es1_per_share": self.d_es1_per_share, "usage": self.usage, "e_i": self.e_i,
                "pending_same_combo": self.pending_same_combo, "pending_requesters": self.pending_requesters, "book": self.book}


class Inventory:
    def __init__(self, cache: Any, cfg: QuoteConfig | None = None, caps: Caps | None = None, *,
                 store_path: str | Path | None = None, horizon_ms: int = DEFAULT_HORIZON_MS, now_ms=None,
                 n_draws: int = 20_000, seed: int = 0):
        self.cache = cache
        self.cfg = cfg or QuoteConfig()
        self.caps = caps or Caps.from_env()
        self.store_path = None if store_path in (None, ":memory:") else str(store_path)
        self.horizon_ms = horizon_ms
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.n_draws, self.seed = n_draws, seed
        self.book = Book()
        self.pool = ModelPool(cache=cache, build=False)
        self.state: Exposure = Exposure(self.book, self.pool, n_draws=n_draws, seed=seed)
        self._lock = threading.Lock()
        self._pending_by_combo: dict[str, dict[str, str]] = {}      # condition_id -> {rfq_id: requester}
        self._conn: sqlite3.Connection | None = None
        self.last_refresh_ms = 0
        self.refresh_ms = 0.0
        self.stats: Counter[str] = Counter()
        self.e_i_samples: deque[float] = deque(maxlen=20_000)
        self.assess_ms: deque[float] = deque(maxlen=20_000)

    # -- hot path ---------------------------------------------------------------------------
    def assess(self, candidate: Position, *, requester: str = "", condition_id: str = "") -> Assessment:
        t0 = time.perf_counter()
        base = self.state
        cand = base.derive([replace(candidate, size=1.0)])
        head: dict[str, float] = {}
        detail: dict[str, str] = {}
        head["outcome"], detail["outcome"], usage = outcome_limit(base, cand, self.caps.limit("outcome"))
        head["game"], detail["game"] = game_limit(base, cand, self.caps.limit("game"))
        head["es1"], slope = es1_limit(base, cand, self.caps.limit("es1"), candidate.size or 1.0)
        detail["es1"] = "portfolio"
        for cap, (s, lbl) in notional_limits(self.book, candidate, self.caps).items():
            head[cap], detail[cap] = s, lbl
        allowed = min(head.values())
        binding = min(head, key=head.get) if allowed < INF else None     # type: ignore[arg-type]
        usage = min(usage, 2.0)
        e_i = self.cfg.k_inv * slope * usage + self.cfg.inventory_skew
        pend = self._pending_by_combo.get(condition_id, {})
        a = Assessment(allowed, binding, head, {k: v for k, v in detail.items() if v}, slope, usage, e_i, len(pend),
                       len(set(pend.values())), self.book_summary(base), cand)
        self.assess_ms.append((time.perf_counter() - t0) * 1000)
        return a

    def on_quoted(self, position: Position, assessment: Assessment, *, requester: str = "", condition_id: str = "") -> None:
        """A quote went out: pending at the quoted size, visible to the next RFQ immediately."""
        with self._lock:
            self.book.add(position)
            if condition_id:
                self._pending_by_combo.setdefault(condition_id, {})[position.rfq_id] = requester
            if assessment.cand is not None and position.size > 0:
                self.state.absorb(assessment.cand, position.size)
        self.stats["quoted"] += 1
        self.e_i_samples.append(assessment.e_i)

    # -- refresh thread ---------------------------------------------------------------------
    def refresh(self, now_ms: int | None = None, force: bool = False) -> bool:
        now = self._now() if now_ms is None else now_ms
        if not force and now - self.last_refresh_ms < self.cfg.risk_refresh_sec * 1000:
            return False
        t0 = time.perf_counter()
        try:
            self._sync_store(now)
        except Exception:
            log.exception("inventory store sync failed")
        with self._lock:
            for rid in self.book.expire(now):
                self.stats["expired"] += 1
                self._forget_pending(rid)
            positions = self.book.active()
        state = Exposure(None, self.pool, positions=positions, n_draws=self.n_draws, seed=self.seed)
        state.monte_carlo()
        with self._lock:
            self.state = state
        self.last_refresh_ms = now
        self.refresh_ms = (time.perf_counter() - t0) * 1000
        self.stats["refreshes"] += 1
        return True

    def _forget_pending(self, rfq_id: str) -> None:
        for cond, d in list(self._pending_by_combo.items()):
            if d.pop(rfq_id, None) is not None and not d:
                del self._pending_by_combo[cond]

    def _store(self) -> sqlite3.Connection | None:
        if self.store_path is None or not Path(self.store_path).exists():
            return None
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True, check_same_thread=False, timeout=5)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _sync_store(self, now: int) -> None:
        """Expired RFQs drop, executions fill (at the trade size when the trade is ours), resolutions settle."""
        c = self._store()
        if c is None:
            return
        with self._lock:
            pending = [p for p in self.book.positions.values() if p.status == PENDING]
            filled = [p for p in self.book.positions.values() if p.status == FILLED]
        for p in pending:
            r = c.execute("SELECT status FROM rfqs WHERE rfq_id=?", (p.rfq_id,)).fetchone()
            if r is None:
                continue
            ex = c.execute("SELECT 1 FROM executions WHERE rfq_id=? LIMIT 1", (p.rfq_id,)).fetchone()
            if ex is not None or r["status"] in ("SELECTED", "EXECUTING", "SETTLED"):
                t = c.execute("SELECT size_e6, price_e6 FROM trades WHERE rfq_id=? AND is_ours=1", (p.rfq_id,)).fetchone()
                with self._lock:
                    self.book.fill(p.rfq_id, size=(t["size_e6"] / 1e6) if t and t["size_e6"] else None,
                                   price=(t["price_e6"] / 1e6) if t and t["price_e6"] else None)
                    self._forget_pending(p.rfq_id)
                self.stats["filled"] += 1
            elif r["status"] in ("EXPIRED", "FILLED_BY_OTHER", "CANCELLED", "FAILED"):
                with self._lock:
                    p.status = EXPIRED
                    self._forget_pending(p.rfq_id)
                self.stats["expired"] += 1
        conds = {l.condition_id for p in filled for l in p.legs if l.condition_id}
        if conds:
            res: dict[str, str] = {}
            cl = list(conds)
            for i in range(0, len(cl), 500):
                chunk = cl[i:i + 500]
                for r in c.execute(f"SELECT condition_id, resolved_outcome FROM market_resolutions WHERE resolved_outcome IS NOT NULL"
                                   f" AND condition_id IN ({','.join('?' * len(chunk))})", chunk):
                    res[r[0]] = r[1]
            if res:
                with self._lock:
                    n = len(self.book.settle(res, now))
                self.stats["settled"] += n

    # -- restart --------------------------------------------------------------------------------
    def load(self, decisions_db: str | Path, now_ms: int | None = None) -> int:
        """Rebuild the pending book from the decisions log (real expiries are logged) and the store."""
        now = self._now() if now_ms is None else now_ms
        book = Book.from_decisions(decisions_db, assume_filled=False, now_ms=now, rfq_db=self.store_path)
        with self._lock:
            for p in book:
                self.book.add(p)
                cond = p.condition_id
                if cond:
                    self._pending_by_combo.setdefault(cond, {})[p.rfq_id] = p.requester
        self.refresh(now, force=True)
        return len(book)

    # -- reporting --------------------------------------------------------------------------------
    def book_summary(self, state: Exposure | None = None) -> dict[str, Any]:
        state = state or self.state
        act = self.book.active()
        es1 = es_at(state.pnl_draws(), 0.01) if (state.game_vec or state.factor_delta) else 0.0
        return {"positions": len(act), "pending": sum(1 for p in act if p.status == PENDING),
                "notional": round(sum(p.notional for p in act), 2), "es1": round(es1, 2),
                "es1_frac": round(es1 / self.caps.capital_usd, 4) if self.caps.capital_usd else None}

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.assess_ms)
        ei = sorted(self.e_i_samples)

        def pct(xs, q):
            return None if not xs else xs[min(len(xs) - 1, int(q * len(xs)))]
        return {**self.book_summary(), "stats": dict(self.stats), "refresh_ms": round(self.refresh_ms, 1),
                "assess_p50_ms": pct(lat, 0.5), "assess_p99_ms": pct(lat, 0.99),
                "e_i": {"n": len(ei), "min": pct(ei, 0.0), "p10": pct(ei, 0.1), "p50": pct(ei, 0.5), "p90": pct(ei, 0.9),
                        "max": ei[-1] if ei else None, "negative": sum(1 for x in ei if x < 0)},
                "caps": self.caps.as_dict()}


def position_from_decision_legs(rfq_id: str, legs: list[Any], sign: int, size: float, price: float, at_ms: int,
                                expires_ms: int | None, entries: dict[str, Any], *, requester: str = "",
                                condition_id: str = "") -> Position:
    """Build the candidate Position from the assembler's LegEvals and cache entries."""
    pl = []
    for le in legs:
        entry = entries.get(le.game_key) if le.game_key else None
        spec = entry.specs.get(le.position_id) if entry is not None else None
        pl.append(PositionLeg(le.position_id, le.key, spec, le.p, le.game_key, le.slug, le.outcome, le.condition_id))
    return Position(rfq_id, pl, sign, size, price, PENDING, at_ms, expires_ms, source="quoter",
                    requester=requester, condition_id=condition_id)
