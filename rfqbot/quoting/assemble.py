"""RFQ -> PaperQuote. Every term is computed separately, logged, and printed in the reason.

Pipeline (docs/quote-assembly-brief.md):
  legs (catalog + cache marginals) -> clusters -> per-cluster probability (marginal product, or
  the joint engine for same-game clusters) -> p_fair = product over clusters -> uncertainty
  terms u_m (leg errors through the product) and u_d (half cluster interval width through the
  product) -> margin m = k_m u_m + k_d u_d + e_b + e_i -> price for the maker's side -> size.

Hard invariants: a quote never sells the combo below p_fair nor buys above it (the live flow
round-trips any such quote within seconds); unknown or unpriceable legs skip the RFQ.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..marginals import Marginal
from ..normalizer import PaperQuote
from ..sports.legspec import canonical_key
from .cache import GameCache, GameEntry
from .config import QuoteConfig
from .decisions import DecisionLog, decision_row
from .semantics import SHARE_E6, UnsupportedRfq, maker_view, notional_e6, requested_shares_e6

try:  # real classifier (rfqbot.clusters) when it lands, else the local stub
    from ..clusters import cluster  # type: ignore
except ImportError:  # pragma: no cover
    from ._cluster_stub import cluster
from ._cluster_stub import game_key_of as game_prefix_of   # slug -> ladder/game prefix (regex, sport agnostic)

log = logging.getLogger(__name__)
TIER_ORDER = ("ok", "thin", "wide", "stale", "disagree", "skip")


def _pair_class(c: Any) -> str:
    """Normalise a cluster's class across the stub (``pair_class``) and rfqbot.clusters (``classes`` set)."""
    pc = getattr(c, "pair_class", None)
    if pc:
        return pc
    classes = set(getattr(c, "classes", ()) or ())
    for k in ("same_market", "same_group", "same_game", "no_game_key"):
        if k in classes:
            return k
    return "single" if len(c.legs) == 1 else "same_game"


@dataclass
class LegEval:
    position_id: str
    slug: str | None
    outcome: str | None
    game_key: str | None
    p: float | None = None
    err: float | None = None
    tier: str = "skip"
    flags: tuple[str, ...] = ()
    kalshi_confidence: str = "none"
    key: str | None = None            # canonical LegSpec key when the sport is registered

    def as_dict(self) -> dict[str, Any]:
        return {"position_id": self.position_id, "slug": self.slug, "outcome": self.outcome, "game_key": self.game_key,
                "p": self.p, "err": self.err, "tier": self.tier, "flags": list(self.flags),
                "kalshi_confidence": self.kalshi_confidence, "key": self.key}

    def short(self) -> str:
        p = "p=None" if self.p is None else f"p={self.p:.3f}±{(self.err or 0):.3f}"
        return f"{self.key or self.slug}|{self.outcome} {p} {self.tier}"


@dataclass
class ClusterEval:
    game_key: str | None
    position_ids: list[str]
    pair_class: str
    p: float | None = None
    lo: float | None = None
    hi: float | None = None
    source: str = "marginal"           # marginal | joint | zero
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"game_key": self.game_key, "position_ids": self.position_ids, "pair_class": self.pair_class, "p": self.p,
                "lo": self.lo, "hi": self.hi, "source": self.source, "notes": self.notes}


@dataclass
class Decision:
    rfq_id: str
    at_ms: int
    direction: str
    side: str
    requested_unit: str
    requested_e6: int
    n_legs: int
    legs: list[LegEval] = field(default_factory=list)
    clusters: list[ClusterEval] = field(default_factory=list)
    certified: bool | None = None
    p_fair: float | None = None
    u_m: float | None = None
    u_d: float | None = None
    e_b: float | None = None
    e_i: float | None = None
    margin: float | None = None
    maker_action: str | None = None
    price_e6: int | None = None
    size_e6: int | None = None
    decision: str = "skipped:unknown"
    reason: str = ""
    latency_ms: float = 0.0
    model_built_at: float | None = None
    requester: str = ""
    condition_id: str = ""

    @property
    def quoted(self) -> bool:
        return self.decision == "quoted"

    def skip(self, why: str, detail: str = "") -> "Decision":
        self.decision = f"skipped:{why}"
        self.reason = f"skip {why}" + (f": {detail}" if detail else "") + (f"; legs: {' | '.join(l.short() for l in self.legs)}" if self.legs else "")
        return self


class Assembler:
    """`Listener(on_rfq=Assembler(...).handle)`."""

    def __init__(self, cache: GameCache, cfg: QuoteConfig | None = None, *, decisions: DecisionLog | None = None,
                 now_ms=None, round_trip_window_ms: int = 10_000):
        self.cache = cache
        self.cfg = cfg or QuoteConfig()
        self.decisions = decisions
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._config_json = self.cfg.to_json()
        self.latencies: deque[float] = deque(maxlen=20_000)
        self.counts: dict[str, int] = {}
        self._recent: deque[tuple[int, str, str, str, int]] = deque(maxlen=5_000)   # (at_ms, requester, condition, direction, size)
        self.round_trip_window_ms = round_trip_window_ms
        self.samples: deque[str] = deque(maxlen=50)

    # -- entry point ------------------------------------------------------------------
    def handle(self, rfq: Any, store: Any) -> PaperQuote | None:
        t0 = time.perf_counter()
        d = self.evaluate(rfq, store)
        d.latency_ms = (time.perf_counter() - t0) * 1000
        self.latencies.append(d.latency_ms)
        self.counts[d.decision] = self.counts.get(d.decision, 0) + 1
        if d.quoted:
            self.samples.append(d.reason)
        if self.decisions is not None:
            try:
                self.decisions.write(decision_row(d, self._config_json))
            except Exception as e:  # pragma: no cover
                log.warning("decision log write failed: %s", e)
        if not d.quoted or d.price_e6 is None or d.size_e6 is None:
            return None
        return PaperQuote(price_e6=d.price_e6, size_e6=d.size_e6, source="collateral", reason=d.reason)

    # -- evaluation (pure given the cache) --------------------------------------------
    def evaluate(self, rfq: Any, store: Any) -> Decision:
        now = self._now()
        rs = rfq.requested_size
        d = Decision(rfq_id=rfq.rfq_id, at_ms=now, direction=rfq.direction, side=rfq.side, requested_unit=rs.unit,
                     requested_e6=int(rs.value_e6 or 0), n_legs=len(rfq.leg_position_ids),
                     requester=getattr(rfq, "requestor_public_id", "") or "", condition_id=getattr(rfq, "condition_id", "") or "")
        try:
            mv = maker_view(rfq.direction, rfq.side, rs.unit)
        except UnsupportedRfq as e:
            return d.skip("unsupported_rfq", str(e))
        d.maker_action = mv.maker_action
        if d.n_legs == 0 or d.requested_e6 <= 0:
            return d.skip("empty_request")
        rt = self._round_trip(d)
        if rt:
            d.legs = []  # filled below; keep the flag in the reason
        # 1. legs
        rows = []
        for pid in rfq.leg_position_ids:
            m = store.get_market(pid)
            if m is None:
                d.legs.append(LegEval(pid, None, None, None))
                return d.skip("leg:unknown_market", pid)
            rows.append({"position_id": pid, "condition_id": m["condition_id"], "slug": m["slug"], "outcome": m["outcome"]})
        cl = cluster(rows)
        d.certified = bool(cl.certified_independent)
        entries: dict[str, GameEntry | None] = {}
        for r in rows:
            gk = game_prefix_of(r["slug"])
            le = LegEval(r["position_id"], r["slug"], r["outcome"], gk)
            d.legs.append(le)
            if gk is None:
                continue
            sport = gk.split("-")[0]
            if sport not in self.cfg.quote_sports:
                continue
            entry = entries.get(gk)
            if entry is None and gk not in entries:
                entry = entries[gk] = self.cache.get(gk)
            if entry is None or not entry.marginals:
                continue
            trip = entry.marginals.get(r["position_id"])
            if trip is None:
                continue
            _, _, marg = trip
            le.p, le.err, le.tier, le.flags, le.kalshi_confidence = marg.p, marg.err, marg.tier, marg.flags, marg.kalshi_confidence
            spec = entry.specs.get(r["position_id"])
            le.key = canonical_key(spec) if spec is not None else None
        for le in d.legs:
            if le.game_key is None:
                continue
            if le.game_key.split("-")[0] not in self.cfg.quote_sports:
                return d.skip("leg:sport_not_quoted", le.game_key.split("-")[0])
            if entries.get(le.game_key) is None:
                return d.skip("leg:cold_game", le.game_key)
            if le.p is None:
                flag = le.flags[0] if le.flags else "no_quote"
                return d.skip(f"leg:{flag.split(':')[0]}", le.short())
            if le.tier == "skip":
                return d.skip(f"leg:{le.flags[0].split(':')[0] if le.flags else 'skip'}", le.short())
        # 2. clusters
        if not d.certified and ("no_game_key" in getattr(cl, "flags", ()) or any(c.game_key is None for c in cl.clusters)):
            return d.skip("unpriceable_dependence", "a leg has no game key")
        by_pid = {l.position_id: l for l in d.legs}
        for c in cl.clusters:
            pids = [l.position_id for l in c.legs]
            prefix = by_pid[pids[0]].game_key          # ladder prefix of the cluster's game
            ce = ClusterEval(c.game_key, pids, _pair_class(c))
            d.clusters.append(ce)
            if len(pids) == 1:
                l = by_pid[pids[0]]
                ce.p, ce.lo, ce.hi, ce.source = l.p, l.p, l.p, "marginal"
                continue
            if ce.pair_class in ("same_market", "same_group"):
                ce.p, ce.lo, ce.hi, ce.source = 0.0, 0.0, 0.0, "zero"
                ce.notes.append(f"{ce.pair_class}: mutually exclusive legs")
                continue
            entry = entries.get(prefix) if prefix else None
            sport = (prefix or "").split("-")[0]
            if entry is None or sport != "nfl":
                return d.skip(f"unsupported:{sport or 'unknown'}_same_game", f"{len(pids)} legs in {prefix}")
            if entry.model is None:
                return d.skip("cold_model", f"{prefix}: {entry.model_error or 'not built yet'}")
            specs = [entry.specs.get(p) for p in pids]
            if any(s is None for s in specs):
                return d.skip("unsupported:unparsed_leg", prefix)
            try:
                jq = entry.model.joint(specs)
            except Exception as e:
                return d.skip("joint_error", f"{type(e).__name__}: {e}")
            if jq.unsupported:
                fams = sorted({k.split("|")[4] + "/" + k.split("|")[3] for k in jq.unsupported})
                return d.skip(f"unsupported:{','.join(fams)}", c.game_key)
            p_use = jq.p_cal if jq.p_cal is not None else jq.p_joint
            ce.p, ce.lo, ce.hi, ce.source = p_use, jq.lo, jq.hi, "joint"
            ce.notes.append(f"raw={jq.p_joint:.3f} cal={p_use:.3f} level={jq.calibration_level} pair={jq.family_pair} "
                            f"model_err={jq.model_error:.3f} indep={jq.p_independent:.3f} "
                            f"mme={jq.max_marginal_error if jq.max_marginal_error is not None else float('nan'):.3f}")
            ce.notes.append(f"shocks={ {k: round(v, 4) for k, v in jq.shocked.items()} }")
            d.model_built_at = entry.model_built_at
        # 3. fair and uncertainty
        p_fair = 1.0
        rel_m2 = 0.0
        rel_d2 = 0.0
        for ce in d.clusters:
            assert ce.p is not None
            p_fair *= ce.p
            if ce.p <= 0:
                continue
            legs = [by_pid[p] for p in ce.position_ids]
            rel_m2 += sum(((l.err or 0) / max(l.p or 1e-9, 1e-9)) ** 2 for l in legs)
            if ce.source == "joint" and ce.lo is not None and ce.hi is not None:
                rel_d2 += (((ce.hi - ce.lo) / 2) / ce.p) ** 2
        d.p_fair = p_fair
        d.u_m = p_fair * math.sqrt(rel_m2)
        d.u_d = p_fair * math.sqrt(rel_d2)
        d.e_b, d.e_i = self.cfg.base_edge, self.cfg.inventory_skew
        d.margin = self.cfg.k_m * d.u_m + self.cfg.k_d * d.u_d + d.e_b + d.e_i
        # 4. price for the maker's side
        tick = self.cfg.tick
        if mv.maker_sells:
            price = math.ceil((p_fair + d.margin) / tick) * tick
            price = max(price, self.cfg.price_min)
            if price > self.cfg.buy_safety_max:
                return d.skip("price_above_safety", f"{price:.3f} > {self.cfg.buy_safety_max}")
            assert price >= p_fair - 1e-12, "never sell below fair"
        else:
            price = math.floor((p_fair - d.margin) / tick) * tick
            if price < self.cfg.price_min:
                return d.skip("price_below_min", f"fair {p_fair:.3f} - margin {d.margin:.3f}")
            price = min(price, self.cfg.price_max)
            assert price <= p_fair + 1e-12, "never buy above fair"
        d.price_e6 = int(round(price * SHARE_E6))
        # 5. size
        requested = requested_shares_e6(mv.size_unit, d.requested_e6, d.price_e6)
        worst = max((l.tier for l in d.legs), key=TIER_ORDER.index)
        cap_usd = self.cfg.max_size_usd * self.cfg.tier_size_mult.get(worst, 0.0) * self.cfg.legs_mult(d.n_legs)
        cap_shares = int(cap_usd * SHARE_E6 / price) if price > 0 else 0
        size = min(requested, cap_shares)
        size -= size % 1000   # 0.001-share granularity
        if notional_e6(size, d.price_e6) < self.cfg.min_size_usd * SHARE_E6:
            return d.skip("size_below_min", f"size {size / SHARE_E6:.2f} sh at {price:.3f} < ${self.cfg.min_size_usd}")
        d.size_e6 = size
        d.decision = "quoted"
        joint_n = sum(1 for c in d.clusters if c.source == "joint")
        impossible = " [impossible]" if p_fair == 0 else ""
        d.reason = (f"fair={p_fair:.4f}{impossible} ({'indep' if d.certified else 'clusters'} {len(d.clusters)}{', ' + str(joint_n) + ' joint' if joint_n else ''}) "
                    f"u_m={d.u_m:.4f} u_d={d.u_d:.4f} base={d.e_b:.3f} inv={d.e_i:g} -> {'ask' if mv.maker_sells else 'bid'} {price:.3f} x {size / SHARE_E6:.0f}"
                    f"{' [round_trip]' if rt else ''}; legs: " + " | ".join(l.short() for l in d.legs))
        return d

    def _round_trip(self, d: Decision) -> bool:
        """Same requester, same combo, opposite direction, same requested size within the window."""
        hit = False
        for at, req, cond, direction, size in self._recent:
            if d.at_ms - at > self.round_trip_window_ms:
                continue
            if req == d.requester and cond == d.condition_id and direction != d.direction:
                hit = True
                break
        self._recent.append((d.at_ms, d.requester, d.condition_id, d.direction, d.requested_e6))
        return hit

    # -- reporting --------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies)

        def pct(q: float) -> float | None:
            return None if not lat else lat[min(len(lat) - 1, int(q * len(lat)))]
        return {"seen": sum(self.counts.values()), "counts": dict(sorted(self.counts.items(), key=lambda kv: -kv[1])),
                "latency_p50_ms": pct(0.5), "latency_p99_ms": pct(0.99), "latency_max_ms": lat[-1] if lat else None,
                "cache": dict(self.cache.stats), "games": len(self.cache.entries)}
