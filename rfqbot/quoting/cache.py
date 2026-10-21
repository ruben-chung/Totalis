"""Per-game cache of quotes, marginals and the joint model, refreshed off the RFQ path.

The RFQ handler only reads ``GameCache.get(prefix)``; refreshes run in a worker thread from
``GameCache.run()`` (an asyncio task next to the listener) or synchronously via
``refresh(prefix)`` in tests. A game is tracked the first time an RFQ names it
(``track(prefix)``) and refreshed every ``refresh_sec``. The joint model (NFL only) is rebuilt
when any informative leg's mid moved by more than ``model_rebuild_move`` since the last build,
or when the build failed before; marginals are recomputed on every refresh (~0.2 s per game).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..marginals import MarginalConfig, Marginal, compute_game, load_config, sport_of
from .config import QuoteConfig

log = logging.getLogger(__name__)


@dataclass
class GameEntry:
    prefix: str
    sport: str | None
    quotes: list[Any] = field(default_factory=list)
    marginals: dict[str, tuple[Any, Any, Marginal]] = field(default_factory=dict)   # position_id -> (quote, match, marginal)
    specs: dict[str, Any] = field(default_factory=dict)                              # position_id -> LegSpec (registered sports)
    model: Any = None                                                                # pricing.GameModel (nfl)
    model_error: str | None = None
    model_mids: dict[str, float] = field(default_factory=dict)
    refreshed_at_ms: int = 0
    refresh_ms: float = 0.0
    model_built_at: float | None = None
    refreshes: int = 0


class GameCache:
    def __init__(self, cfg: QuoteConfig | None = None, *, marginal_cfg: MarginalConfig | None = None,
                 poly=None, kalshi=None, now_ms: Callable[[], int] | None = None, build_models: bool = True,
                 model_builder: Callable[[list[Any]], Any] | None = None):
        self.cfg = cfg or QuoteConfig()
        self.mcfg = marginal_cfg or load_config()
        self._poly = poly
        self._kalshi = kalshi
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.build_models = build_models
        self._model_builder = model_builder
        self.entries: dict[str, GameEntry] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = False
        self.stats = {"refreshes": 0, "model_builds": 0, "errors": 0}
        self.on_refresh: list[Callable[[], Any]] = []     # run after each refresh_due pass, on the worker thread

    # -- read side (RFQ path) -------------------------------------------------------
    def get(self, prefix: str) -> GameEntry | None:
        e = self.entries.get(prefix)
        if e is None:
            self.track(prefix)
        return e

    def track(self, prefix: str) -> None:
        with self._lock:
            if prefix not in self.entries and prefix not in self._pending:
                self._pending.add(prefix)

    # -- refresh side ---------------------------------------------------------------
    def refresh(self, prefix: str) -> GameEntry:
        t0 = time.perf_counter()
        now = self._now()
        sport = sport_of(prefix)
        old = self.entries.get(prefix)
        entry = GameEntry(prefix=prefix, sport=sport)
        # compute_game applies the marginal layer's staleness rule (seconds since the feed last
        # confirmed the quote), which a per-row age cannot see on change-only book stores
        legs = compute_game(prefix, None, cfg=self.mcfg, now_ms=now, poly=self._poly, kalshi=self._kalshi)
        quotes = [l.quote for l in legs]
        entry.quotes = quotes
        for l in legs:
            entry.marginals[l.quote.position_id] = (l.quote, l.match, l.marginal)
        entry.specs = self._parse_specs(sport, quotes)
        if old is not None:
            entry.model, entry.model_built_at, entry.model_mids, entry.model_error = old.model, old.model_built_at, old.model_mids, old.model_error
            entry.refreshes = old.refreshes
        if self.build_models and sport == "nfl" and quotes:
            mids = {q.position_id: q.mid for q in quotes if q.mid is not None and (q.best_ask - q.best_bid) <= 0.1}
            moved = old is None or entry.model is None or any(abs(mids.get(k, m) - m) > self.cfg.model_rebuild_move for k, m in entry.model_mids.items())
            if moved:
                try:
                    builder = self._model_builder or self._default_builder
                    entry.model = builder(quotes)
                    entry.model_built_at = time.time()
                    entry.model_mids = mids
                    entry.model_error = None
                    self.stats["model_builds"] += 1
                except Exception as e:  # keep the old model, record why
                    entry.model_error = f"{type(e).__name__}: {e}"
                    self.stats["errors"] += 1
                    log.warning("joint model build failed for %s: %s", prefix, e)
        entry.refreshed_at_ms = now
        entry.refresh_ms = (time.perf_counter() - t0) * 1000
        entry.refreshes += 1
        with self._lock:
            self.entries[prefix] = entry
            self._pending.discard(prefix)
            if len(self.entries) > self.cfg.max_games:
                oldest = min(self.entries.values(), key=lambda x: x.refreshed_at_ms)
                del self.entries[oldest.prefix]
        self.stats["refreshes"] += 1
        return entry

    @staticmethod
    def _default_builder(quotes: list[Any]) -> Any:
        """Build the joint model and pre-warm its shocked variants so `joint()` on the RFQ path
        is a scenario pass (~1 ms), never a rebuild (~0.5 s)."""
        from ..pricing.joint import DEFAULT_SHOCKS, build_game
        from ..risk.exposure import outcome_matrix
        model = build_game(quotes)
        for sh in DEFAULT_SHOCKS:
            model.shocked(sh)
        model.marginal_check()                      # warms every leg's indicator for the risk hot path
        outcome_matrix(model)                       # and the cap outcomes' indicator matrix
        return model

    @staticmethod
    def _parse_specs(sport: str | None, quotes: list[Any]) -> dict[str, Any]:
        from ..sports import registry
        out: dict[str, Any] = {}
        if sport is None:
            return out
        try:
            sp = registry.get(sport)
        except KeyError:
            return out
        for q in quotes:
            try:
                out[q.position_id] = sp.parse_poly(q.slug, q.title, q.outcome, q.outcome_index, position_id=q.position_id)
            except ValueError:
                continue
        return out

    def due(self) -> list[str]:
        now = self._now()
        with self._lock:
            pend = list(self._pending)
            stale = [p for p, e in self.entries.items() if now - e.refreshed_at_ms >= self.cfg.refresh_sec * 1000]
        return pend + stale

    def refresh_due(self) -> int:
        n = 0
        for p in self.due():
            try:
                self.refresh(p)
                n += 1
            except Exception as e:
                self.stats["errors"] += 1
                log.warning("cache refresh failed for %s: %s", p, e)
                with self._lock:
                    self._pending.discard(p)
                    self.entries.setdefault(p, GameEntry(prefix=p, sport=sport_of(p))).refreshed_at_ms = self._now()
        for hook in self.on_refresh:
            try:
                hook()
            except Exception as e:  # pragma: no cover
                self.stats["errors"] += 1
                log.warning("cache hook failed: %s", e)
        return n

    async def run(self, period_sec: float = 1.0) -> None:
        """Background loop: refresh pending and stale games in a worker thread."""
        loop = asyncio.get_running_loop()
        while not self._stop:
            try:
                await loop.run_in_executor(None, self.refresh_due)
            except Exception as e:  # pragma: no cover
                log.warning("cache loop error: %s", e)
            await asyncio.sleep(period_sec)

    def stop(self) -> None:
        self._stop = True
