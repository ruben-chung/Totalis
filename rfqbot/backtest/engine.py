"""The replay loop: one pass over the recorded RFQ stream, any number of strategies in lock-step.

A *strategy* is one setting of the quoting path (its own Assembler, Inventory and book). All strategies of a
pass share the pinned clock, the as-of book readers and the game cache, because none of those depend on a
setting: the cache refresh is driven by the clock, and the expensive part (marginals plus the joint model
rebuild, ~2 s per NFL game) is paid once for all of them. ``run`` is a pass with one strategy; ``compare``
is a pass with many.

Per quoted-sport RFQ, in order: (1) the clock moves to the frame time; (2) events due by then are applied
to every book (prints that would have filled a pending quote, expiries) and exposures are rebuilt at the
live period; (3) the game cache refreshes the games that recently saw RFQs, on the live cadence; (4) each
strategy runs ``Assembler.evaluate`` and, if it quotes, adds the pending position at the quoted size.

Deviations from the live bot, all recorded in claude-sessions/backtest-notes.md: the refresh happens on the RFQ path
at one-second ticks instead of on a worker thread (a cold game is skipped once, as live, then served from
the next tick); games are refreshed only while they received an RFQ in the last ``refresh_idle_ms``;
exposures are rebuilt only when the book changed.
"""
from __future__ import annotations

import heapq
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from ..edge.table import MarginTable
from ..pricing.calibrate import Calibration, default_calibration
from ..pricing.joint import DEFAULT_SHOCKS, Shock
from ..quoting.assemble import Assembler, Decision
from ..quoting.cache import GameCache, GameEntry
from ..quoting.config import QuoteConfig, load_quote_config
from ..quoting.decisions import decision_row
from ..quoting.inventory import Inventory, position_from_decision_legs
from ..risk.book import FILLED, PENDING
from ..risk.caps import Caps
from ..sports.legspec import canonical_key
from .asof import CatalogRO, Clock, KalshiAsOf, PolyAsOf
from .config import BacktestConfig, WindowConfig
from .feed import SportIndex, iter_requests
from .lazy import ReplayCache
from .fills import E6, FillResult, evaluate_fill, load_print_index
from .store import BacktestStore

log = logging.getLogger(__name__)
PAIR_CLASS_ORDER = ("same_market", "same_group", "same_game", "no_game_key")


# ------------------------------------------------------------------ pricing-switch views of the shared cache
class ModelView:
    """A GameModel seen through a calibration and a shock set (the model itself is shared and unchanged)."""

    def __init__(self, model: Any, calibration: Calibration | None, shocks: tuple[Shock, ...]):
        self.model, self.calibration, self.shocks = model, calibration, shocks

    def joint(self, specs: Iterable[Any]) -> Any:
        return self.model.joint(list(specs), shocks=self.shocks, calibration=self.calibration)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)


class IndependenceModel:
    """Same-game clusters priced as the product of the legs' blended marginals (no dependence, no interval)."""

    def __init__(self, entry: GameEntry):
        self.entry = entry
        self.pid_of = {canonical_key(spec): pid for pid, spec in entry.specs.items()}

    def _p(self, key: str) -> float | None:
        pid = self.pid_of.get(key)
        trip = self.entry.marginals.get(pid) if pid is not None else None
        return None if trip is None else trip[2].p

    def joint(self, specs: Iterable[Any]) -> Any:
        keys = [canonical_key(s) for s in specs]
        missing = [k for k in keys if self._p(k) is None]
        p = 1.0
        for k in keys:
            p *= self._p(k) or 0.0
        return SimpleNamespace(unsupported=missing, p_cal=p, p_joint=p, lo=p, hi=p, calibration_level="independence",
                               family_pair=None, model_error=0.0, p_independent=p, max_marginal_error=None, shocked={})

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def scaled_shocks(scale: float) -> tuple[Shock, ...]:
    return tuple(s if s.kind == "bootstrap" else Shock(s.kind, s.value * scale) for s in DEFAULT_SHOCKS)


def scaled_calibration(cal: Calibration, factor: float) -> Calibration:
    """The fitted calibration with every pair / group lambda multiplied by ``factor`` (tail maps and errors untouched)."""
    return Calibration(pairs={k: replace(v, lam=v.lam * factor) for k, v in cal.pairs.items()},
                       groups={k: replace(v, lam=v.lam * factor) for k, v in cal.groups.items()}, meta=dict(cal.meta), identity=cal.identity)


class CacheView:
    """What one strategy's Assembler sees: the shared cache, with the joint model replaced per the pricing switches."""

    def __init__(self, cache: GameCache, bcfg: BacktestConfig):
        self.cache, self.bcfg = cache, bcfg
        cal: Calibration | None
        if bcfg.calibration == "fitted":
            cal = None
        elif bcfg.calibration == "ones":
            cal = Calibration()
        else:
            cal = scaled_calibration(default_calibration(), float(bcfg.calibration))
        self.calibration = cal
        self.shocks = DEFAULT_SHOCKS if bcfg.shock_scale == 1.0 else scaled_shocks(bcfg.shock_scale)
        self.trivial = bcfg.pricing == "joint" and cal is None and bcfg.shock_scale == 1.0
        self._memo: dict[tuple[int, int], GameEntry] = {}

    def get(self, prefix: str) -> GameEntry | None:
        e = self.cache.get(prefix)
        if e is None or self.trivial or e.model is None:
            return e
        key = (id(e), id(e.model))
        v = self._memo.get(key)
        if v is None:
            model = IndependenceModel(e) if self.bcfg.pricing == "independence" else ModelView(e.model, self.calibration, self.shocks)
            v = self._memo[key] = replace(e, model=model)
            if len(self._memo) > 4000:
                self._memo.clear()
        return v

    def __getattr__(self, name: str) -> Any:
        return getattr(self.cache, name)


# ------------------------------------------------------------------ one strategy
def pair_class(d: Decision) -> str:
    cls = {c.pair_class for c in d.clusters}
    for k in PAIR_CLASS_ORDER:
        if k in cls:
            return k
    return "single" if d.n_legs == 1 else "disjoint"


def decision_sport(d: Decision) -> str:
    sports = {l.game_key.split("-")[0] for l in d.legs if l.game_key}
    return "mixed" if len(sports) > 1 else (next(iter(sports)) if sports else "")


@dataclass
class Strategy:
    run_id: str
    bcfg: BacktestConfig
    qcfg: QuoteConfig
    cache: ReplayCache
    clock: Clock
    prints: dict[str, Any]
    poly: PolyAsOf
    group_id: str | None = None
    fill_rule: str = "win"
    risk_refresh_ms: int = 2000
    table: MarginTable | None = None
    table_json: str | None = None

    def __post_init__(self) -> None:
        self.view = CacheView(self.cache, self.bcfg)
        self.inv = Inventory(self.cache, self.qcfg, Caps.from_env(), store_path=None, now_ms=self.clock)
        self.asm = Assembler(self.view, self.qcfg, decisions=None, now_ms=self.clock, inventory=self.inv)
        self.cfg_on = self.qcfg
        self.cfg_off = replace(self.qcfg, edge=False)
        if self.table is not None and self.qcfg.edge:
            self.asm._edge_table = self.table              # frozen snapshot, not the file the edge monitor may rewrite
            self.asm.edge_table_fitted_at = self.table.fitted_at_ms
        self.config_json = self.qcfg.to_json()
        self.rows: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.latencies: list[float] = []
        self.events: list[tuple[int, int, str, str]] = []      # (when_ms, seq, kind, rfq_id)
        self.fill_info: dict[str, FillResult] = {}
        self.es1_rows: list[tuple] = []
        self.es1_peak: tuple | None = None          # (at_ms, es1, ...) of the highest sample, kept across flushes
        self.dirty = False
        self.seq = 0
        self.n_filled = 0
        self.n_expired = 0

    # -- per RFQ ----------------------------------------------------------------------
    def evaluate(self, rfq: Any, catalog: Any, t: int) -> Decision:
        if self.table is not None and self.bcfg.table_policy == "after_fit":
            self.asm.cfg = self.cfg_on if t >= self.table.fitted_at_ms else self.cfg_off
        t0 = time.perf_counter()
        d = self.asm.evaluate(rfq, catalog)
        d.latency_ms = (time.perf_counter() - t0) * 1000
        self.latencies.append(d.latency_ms)
        self._relabel_book_skips(d, t)
        self.counts[d.decision] += 1
        fr = FillResult()
        if d.quoted:
            self._on_quoted(d)
            fr = evaluate_fill(d, self.prints, tick=self.qcfg.tick, rule=self.fill_rule)
            if fr.fill_size_e6 > 0:
                self.seq += 1
                heapq.heappush(self.events, (max(fr.executed_at_ms or t, t), self.seq, "fill", d.rfq_id))
        self.fill_info[d.rfq_id] = fr
        row = decision_row(d, self.config_json)
        row.update(fr.as_dict())
        games = sorted({l.game_key for l in d.legs if l.game_key})
        row.update(run_id=self.run_id, sport=decision_sport(d), games_json=json.dumps(games), pair_class=pair_class(d) if d.clusters else None)
        self.rows.append(row)
        return d

    def _relabel_book_skips(self, d: Decision, t: int) -> None:
        """Replay-specific reasons for 'the book was not there': distinct from the live skip reasons (the raw reason is kept)."""
        if d.decision not in ("skipped:leg:no_quote", "skipped:leg:cold_game") and not d.decision.startswith("skipped:leg:stale"):
            return
        bad = next((l for l in d.legs if l.game_key and (l.p is None or l.tier == "skip")), None)
        if bad is None:
            return
        first = self.poly.first_row_ms(bad.game_key)
        if d.decision == "skipped:leg:cold_game":
            if first is None or first > t:
                d.decision = "skipped:replay:no_book_yet"
            return
        if first is None or first > t:
            d.decision = "skipped:replay:no_book_yet"
        elif any(f.startswith("stale") for f in bad.flags):
            d.decision = "skipped:replay:book_too_old"

    def _on_quoted(self, d: Decision) -> None:
        pos, a = d.position, d.assessment
        if pos is None or a is None:                      # caps off: still track the book so ES1 can be reported
            entries = {l.game_key: self.view.cache.entries.get(l.game_key) for l in d.legs if l.game_key}
            sign = -1 if d.maker_action == "sell_yes" else 1
            pos = position_from_decision_legs(d.rfq_id, d.legs, sign, d.size_e6 / E6, d.price_e6 / E6, d.at_ms, d.expires_ms, entries,
                                              requester=d.requester, condition_id=d.condition_id)
            a = self.inv.assess(pos, requester=d.requester, condition_id=d.condition_id)
        self.inv.on_quoted(pos, a, requester=d.requester, condition_id=d.condition_id)
        self.dirty = True

    # -- clock-driven -----------------------------------------------------------------
    def advance(self, t: int) -> None:
        while self.events and self.events[0][0] <= t:
            when, _, kind, rid = heapq.heappop(self.events)
            fr = self.fill_info[rid]
            pos = self.inv.book.positions.get(rid)
            if pos is None or pos.status != PENDING or when >= (pos.expires_ms or 1 << 62):
                continue
            with self.inv._lock:
                self.inv.book.fill(rid, size=fr.fill_size_e6 / E6, price=pos.price)
                self.inv._forget_pending(rid)
            self.inv.stats["filled"] += 1
            self.n_filled += 1
            self.dirty = True
        if t - self.inv.last_refresh_ms >= self.risk_refresh_ms and (self.dirty or self._has_expiring(t)):
            before = sum(1 for p in self.inv.book.positions.values() if p.status == PENDING)
            self.inv.refresh(t, force=True)
            after = sum(1 for p in self.inv.book.positions.values() if p.status == PENDING)
            self.n_expired += max(0, before - after - 0)
            self.dirty = False
            s = self.inv.book_summary()
            row = (t, s["es1"], s["es1_frac"], s["positions"], s["pending"], s["notional"])
            self.es1_rows.append(row)
            if self.es1_peak is None or row[1] > self.es1_peak[1]:
                self.es1_peak = row

    def _has_expiring(self, t: int) -> bool:
        return any(p.status == PENDING and p.expires_ms is not None and p.expires_ms <= t for p in self.inv.book.positions.values())

    def flush(self, store: BacktestStore) -> None:
        store.insert_decisions(self.rows)
        store.insert_es1(self.run_id, self.es1_rows)
        self.rows = []
        self.es1_rows = []


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


@dataclass
class RunSummary:
    run_id: str
    name: str
    counts: dict[str, int]
    quoted: int
    printed: int
    win: int
    strict: int
    filled: int
    es1_max: float | None
    es1_max_at_ms: int | None
    latency_p50_ms: float | None
    latency_p99_ms: float | None
    elapsed_s: float = 0.0
    prefilter: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ------------------------------------------------------------------ the pass
@dataclass
class Replayer:
    source: Path
    window: WindowConfig
    configs: list[BacktestConfig]
    rfq_db: Path = Path("./data/rfq.db")
    books_db: Path = Path("./data/books.db")
    kalshi_db: Path = Path("./data/kalshi_books.db")
    out_db: Path = Path("./data/backtest.db")
    group_id: str | None = None
    run_prefix: str = ""
    progress: Callable[[str], None] = print
    max_requests: int | None = None
    strategies: list[Strategy] = field(default_factory=list)

    def run(self) -> list[RunSummary]:
        t_start = time.time()
        w = self.window
        clock = Clock(w.start_ms)
        poly = PolyAsOf(self.books_db, self.rfq_db, clock, end_ms=w.end_ms)
        kalshi = KalshiAsOf(self.kalshi_db, clock, end_ms=w.end_ms)
        base_q = load_quote_config()
        self.progress("loading catalog, sport index, prints ...")
        cache = ReplayCache(base_q, poly=poly, kalshi=kalshi, now_ms=clock, idle_ms=w.refresh_idle_ms)
        catalog = CatalogRO(self.rfq_db)
        sports = SportIndex(self.rfq_db, base_q.quote_sports)
        prints = load_print_index(self.rfq_db, w.start_ms, w.end_ms, sports=base_q.quote_sports)
        store = BacktestStore(self.out_db)
        self.progress(f"  {len(sports.quoted_pids)} quoted-sport positions, {len(prints)} prints in the window, {len(self.configs)} strategies")
        poly.preload(base_q.quote_sports, self.progress)
        kalshi.preload(self.progress)
        poly.heartbeat(); kalshi.confirm(w.start_ms)
        self.progress(f"  heartbeats loaded; {time.time() - t_start:.0f}s")
        self.strategies = []
        for i, bc in enumerate(self.configs):
            qc = bc.quote_config(base_q)
            table, table_json = _load_table(bc, qc)
            run_id = f"{self.run_prefix}{bc.name}" if self.run_prefix else bc.name
            st = Strategy(run_id, bc, qc, cache, clock, prints, poly, group_id=self.group_id,
                          risk_refresh_ms=w.risk_refresh_ms or int(qc.risk_refresh_sec * 1000), table=table, table_json=table_json,
                          fill_rule=bc.fill_rule)
            store.start_run(run_id, bc.name, bc.to_json(), qc.to_json(), w.to_json(), group_id=self.group_id, table_json=table_json)
            self.strategies.append(st)
        names = [s.run_id for s in self.strategies]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate run ids: {names}")
        pre: Counter[str] = Counter()
        fcounts: dict[str, int] = {}
        n_eval = 0
        last_tick = -10**12
        last_say = time.time()
        store.begin()
        try:
            for t, rfq in iter_requests(self.source, w.start_ms, w.end_ms, counts=fcounts):
                pre["requests"] += 1
                cls = sports.classify(rfq.leg_position_ids)
                if cls != "quoted":
                    pre[f"pre:{cls}"] += 1
                    continue
                clock.set(t)
                for st in self.strategies:
                    st.advance(t)
                if t - last_tick >= 1000:
                    cache.refresh_due()
                    last_tick = t
                for pid in rfq.leg_position_ids:
                    m = catalog.get_market(pid)
                    gk = _game_of(m)
                    if gk:
                        cache.touch(gk, t)
                for st in self.strategies:
                    st.evaluate(rfq, catalog, t)
                n_eval += 1
                if n_eval % 1000 == 0 or time.time() - last_say > 60:
                    last_say = time.time()
                    done = (t - w.start_ms) / max(1, w.end_ms - w.start_ms)
                    self.progress(f"  {n_eval} RFQs evaluated, window {done:.0%} (t={t}), games={len(cache.entries)}, loads={poly.loads} "
                                  f"({poly.load_s:.0f}s), model builds={cache.stats['model_builds']} ({cache.stats.get('model_build_s', 0):.0f}s), "
                                  f"{time.time() - t_start:.0f}s elapsed")
                    for st in self.strategies:
                        st.flush(store)
                if self.max_requests and n_eval >= self.max_requests:
                    break
            clock.set(max(clock.t, w.end_ms))
            for st in self.strategies:
                st.advance(clock.t)
                st.flush(store)
            out = []
            for st in self.strategies:
                s = self._summary(st, pre, fcounts, time.time() - t_start)
                store.finish_run(st.run_id, {**s.as_dict(), "frames": fcounts, "cache": dict(cache.stats), "games": len(cache.entries),
                                             "poly_game_loads": poly.loads, "inventory": st.inv.summary()})
                out.append(s)
        finally:
            store.commit()
            catalog.close()
        store.close()
        return out

    def _summary(self, st: Strategy, pre: Counter, fcounts: dict[str, int], elapsed: float) -> RunSummary:
        fi = st.fill_info.values()
        es = st.es1_peak
        return RunSummary(
            st.run_id, st.bcfg.name, dict(st.counts), st.counts.get("quoted", 0), sum(1 for f in fi if f.printed), sum(1 for f in fi if f.win),
            sum(1 for f in fi if f.strict), sum(1 for f in fi if f.fill_size_e6 > 0), None if es is None else es[1], None if es is None else es[0],
            _pct(st.latencies, 0.5), _pct(st.latencies, 0.99), elapsed, dict(pre))


def _game_of(m: Any) -> str | None:
    if m is None:
        return None
    from ..quoting.assemble import game_prefix_of
    return game_prefix_of(m["slug"])


def _load_table(bc: BacktestConfig, qc: QuoteConfig) -> tuple[MarginTable | None, str | None]:
    if bc.table == "none" or not qc.edge:
        return None, None
    from ..edge.table import DEFAULT_TABLE
    path = Path(qc.edge_table) if qc.edge_table else DEFAULT_TABLE
    if not path.exists():
        return None, None
    text = path.read_text()
    return MarginTable(json.loads(text), path), text
