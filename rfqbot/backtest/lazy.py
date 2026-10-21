"""A game cache that computes lazily and gives the same numbers as the live one.

``GameCache.refresh`` eagerly computes every leg's marginal (0.3 s for an NFL game) and rebuilds the joint
model (3.6 s with its shocked variants) whenever a mid moved. The RFQ path only needs the legs of the RFQ in
hand and, for same-game combos, the model of one game. ``ReplayCache.refresh`` therefore records the refresh
instant ``T`` and returns an entry whose marginals and model are computed on first use *as of T*:

  * ``LazyMarginals.get(pid)`` runs the marginal layer's per-leg path (``marginal_for_leg``) on the as-of quote,
    match and Kalshi quote at ``T`` with ``now_ms = T`` and the heartbeats at ``T``: exactly what
    ``compute_game(prefix, None, now_ms=T)`` would have returned for that leg (tested leg by leg);
  * ``LazyModel`` calls the same builder on the as-of quotes at ``T`` the first time anything asks for the model,
    under the same rebuild rule (a mid moved by more than ``model_rebuild_move`` since the last build);
    a model nobody prices or holds a position in is never built.

Only the *time of computation* differs from live, never the inputs. Games refresh only while they received an
RFQ recently (``idle_ms``), see engine.py.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..marginals import marginal_for_leg, sport_of
from ..quoting.cache import GameCache, GameEntry
from .asof import GameData, KalshiAsOf, PolyAsOf

log = logging.getLogger(__name__)


class LazyMarginals:
    """position_id -> (quote, match, marginal) for one refresh instant, computed on first access."""

    def __init__(self, poly: PolyAsOf, kalshi: KalshiAsOf, game: GameData, sport: str | None, t_ms: int, mcfg: Any):
        self.poly, self.kalshi, self.game, self.sport, self.t, self.mcfg = poly, kalshi, game, sport, t_ms, mcfg
        self._pos = game.positions(t_ms)
        self._done: dict[str, tuple[Any, Any, Any] | None] = {}
        self._k_hb: int | None | bool = False

    def __bool__(self) -> bool:
        return bool(self.game.pids)

    def __len__(self) -> int:
        return len(self.game.pids)

    def __contains__(self, pid: str) -> bool:
        return pid in self.game.pid_index

    def get(self, pid: str, default: Any = None) -> Any:
        if pid in self._done:
            return self._done[pid]
        i = self.game.pid_index.get(pid)
        if i is None:
            return default
        mq = self.game.quote(i, int(self._pos[i]))
        row, kq = self.kalshi.read([pid], self.t).get(pid, (None, None))
        if self._k_hb is False:
            self._k_hb = self.kalshi.confirm(self.t)
        poly_conf = self.poly.heartbeat().at(self.t) if self.game.subscribed(i, self.t) else None
        m = marginal_for_leg(mq, row, kq, sport=self.sport, now_ms=self.t, cfg=self.mcfg,
                             poly_confirmed_ms=poly_conf, kalshi_confirmed_ms=self._k_hb)
        out = self._done[pid] = (mq, row, m)
        return out

    __getitem__ = get

    def items(self):                       # materializes everything (diagnostics, tests)
        return [(p, self.get(p)) for p in self.game.pids]


class LazyModel:
    """Stands in for ``entry.model`` until something asks the model for anything."""

    def __init__(self, build: Callable[[], Any], cache_stats: dict[str, int]):
        self._build, self._stats = build, cache_stats
        self._model: Any = None
        self.error: str | None = None

    def _get(self) -> Any:
        if self._model is None:
            if self.error is not None:
                raise RuntimeError(self.error)
            t0 = time.perf_counter()
            try:
                self._model = self._build()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self._stats["errors"] += 1
                log.warning("joint model build failed: %s", e)
                raise
            self._stats["model_builds"] += 1
            self._stats["model_build_s"] = self._stats.get("model_build_s", 0) + (time.perf_counter() - t0)
        return self._model

    @property
    def built(self) -> bool:
        return self._model is not None

    def joint(self, *a: Any, **kw: Any) -> Any:
        return self._get().joint(*a, **kw)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in ("_build", "_stats", "_model", "error"):
            raise AttributeError(name)
        return getattr(self._get(), name)


class ReplayCache(GameCache):
    """GameCache with lazy marginals / models, memoized specs and a refresh policy bounded to recently active games."""

    def __init__(self, *a: Any, poly: PolyAsOf, kalshi: KalshiAsOf, idle_ms: int = 300_000, **kw: Any):
        super().__init__(*a, poly=poly, kalshi=kalshi, **kw)
        self.poly_asof, self.kalshi_asof = poly, kalshi
        self.idle_ms = idle_ms
        self.active: dict[str, int] = {}
        self._spec_memo: dict[str, Any] = {}
        self.stats.setdefault("model_build_s", 0.0)

    def touch(self, prefix: str, t_ms: int) -> None:
        self.active[prefix] = t_ms

    def due(self) -> list[str]:
        now = self._now()
        with self._lock:
            pend = list(self._pending)
            stale = [p for p, e in self.entries.items()
                     if now - e.refreshed_at_ms >= self.cfg.refresh_sec * 1000 and now - self.active.get(p, 0) <= self.idle_ms]
        return pend + stale

    def refresh(self, prefix: str) -> GameEntry:
        t0 = time.perf_counter()
        now = self._now()
        sport = sport_of(prefix)
        old = self.entries.get(prefix)
        entry = GameEntry(prefix=prefix, sport=sport)
        game = self.poly_asof.game(prefix)
        entry.marginals = LazyMarginals(self.poly_asof, self.kalshi_asof, game, sport, now, self.mcfg)      # type: ignore[assignment]
        entry.specs = self._specs(sport, game, now)
        if old is not None:
            entry.model, entry.model_built_at, entry.model_mids, entry.model_error = old.model, old.model_built_at, old.model_mids, old.model_error
            entry.refreshes = old.refreshes
        if self.build_models and sport == "nfl" and game.pids:
            mids = game.mids(now)
            moved = old is None or entry.model is None or any(abs(mids.get(k, m) - m) > self.cfg.model_rebuild_move for k, m in entry.model_mids.items())
            if moved:
                builder = self._model_builder or self._default_builder
                entry.model = LazyModel(lambda g=game, n=now, b=builder: b(g.quotes(n)), self.stats)
                entry.model_built_at = time.time()
                entry.model_mids = mids
                entry.model_error = None
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

    def _specs(self, sport: str | None, game: GameData, now: int) -> dict[str, Any]:
        """position_id -> LegSpec, memoized across refreshes (a catalog row never changes)."""
        out: dict[str, Any] = {}
        todo = [i for i, p in enumerate(game.pids) if p not in self._spec_memo]
        if todo:
            quotes = {i: game.quote(i, -1) for i in todo}      # slug / title / outcome only; no book needed
            parsed = self._parse_specs(sport, list(quotes.values()))
            for i, q in quotes.items():
                self._spec_memo[q.position_id] = parsed.get(q.position_id)
        for p in game.pids:
            s = self._spec_memo.get(p)
            if s is not None:
                out[p] = s
        return out
