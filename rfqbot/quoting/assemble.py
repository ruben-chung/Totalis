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
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..edge import MarginTerms, Segment, load_edge_config, load_table, margin_for
from ..marginals import Marginal
from ..normalizer import PaperQuote
from ..sports.legspec import canonical_key
from .cache import GameCache, GameEntry
from .config import QuoteConfig
from .decisions import DecisionLog, decision_row
from .inventory import Assessment, Inventory, kelly_size, position_from_decision_legs
from .semantics import SHARE_E6, UnsupportedRfq, maker_view, notional_e6, requested_shares_e6

from ..clusters import cluster

log = logging.getLogger(__name__)
TIER_ORDER = ("ok", "thin", "wide", "stale", "disagree", "skip")

# Slug -> ladder/game prefix ("nfl-dal-phi-2026-10-27"), sport agnostic. This is the books/ladders
# prefix, not rfqbot.clusters' game_key (which is the LegSpec key for registered sports).
GAME_RE = re.compile(r"^([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")
DOUBLES_RE = re.compile(r"^([a-z]+-doubles-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")


def game_prefix_of(slug: str | None) -> str | None:
    if not slug:
        return None
    m = GAME_RE.match(slug) or DOUBLES_RE.match(slug)
    return m.group(1) if m else None


def _pair_class(c: Any) -> str:
    """The strongest pair class that joined a cluster's legs (rfqbot.clusters ``classes`` set)."""
    for k in ("same_market", "same_group", "same_game", "no_game_key"):
        if k in c.classes:
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
    condition_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"position_id": self.position_id, "slug": self.slug, "outcome": self.outcome, "game_key": self.game_key,
                "p": self.p, "err": self.err, "tier": self.tier, "flags": list(self.flags),
                "kalshi_confidence": self.kalshi_confidence, "key": self.key, "condition_id": self.condition_id}

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
    # stage 3: sizing against the live book
    deadline_ms: int | None = None
    expires_ms: int | None = None
    requested_shares: float | None = None     # shares the requester asked for at our price
    kelly_size: float | None = None
    allowed_size: float | None = None         # shares every cap allows (None = caps off / unconstrained)
    binding_cap: str | None = None            # the cap that cut or killed the size, when one did
    headroom: dict[str, float | None] | None = None
    d_es1_per_share: float | None = None
    pending_same_combo: int | None = None
    pending_requesters: int | None = None
    book: dict[str, Any] | None = None
    assessment: Assessment | None = field(default=None, repr=False, compare=False)
    position: Any = field(default=None, repr=False, compare=False)
    # margin optimiser (rfqbot.edge): which node priced the margin
    edge_segment: str | None = None
    edge_level: int | None = None
    edge_source: str | None = None
    edge_n: int | None = None
    curse_mult: float | None = None
    info_premium: float | None = None
    wallet_premium: float | None = None

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
                 now_ms=None, round_trip_window_ms: int = 10_000, inventory: Inventory | None = None,
                 horizon_ms: int = 31_000):
        self.cache = cache
        self.cfg = cfg or QuoteConfig()
        self.decisions = decisions
        self.inventory = inventory          # live book + caps + skew (None or cfg.risk False: stage 2 behaviour)
        self.horizon_ms = horizon_ms        # acceptance + last look + grace after the submission deadline
        self._sigma_cuts = load_edge_config().sigma_cuts
        self._edge_table = None             # rfqbot.edge margin table, reloaded off the hot path (reload_edge_table)
        self.edge_table_fitted_at: int | None = None
        if self.cfg.edge:
            self.reload_edge_table()
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._config_json = self.cfg.to_json()
        self.latencies: deque[float] = deque(maxlen=20_000)
        self.counts: dict[str, int] = {}
        self._recent: deque[tuple[int, str, str, str, int]] = deque(maxlen=5_000)   # (at_ms, requester, condition, direction, size)
        self.round_trip_window_ms = round_trip_window_ms
        self.samples: deque[str] = deque(maxlen=50)

    # -- margin table (refresh thread) ------------------------------------------------
    def reload_edge_table(self) -> bool:
        """Re-read rfqbot/edge's margin table when its mtime changed (a refit by the edge monitor);
        hook this on GameCache.on_refresh so the hot path never stats the file. Returns True on a change."""
        t = load_table(self.cfg.edge_table, max_age_s=0.0)
        changed = (t.fitted_at_ms if t is not None else None) != self.edge_table_fitted_at or (t is None) != (self._edge_table is None)
        self._edge_table = t
        self.edge_table_fitted_at = t.fitted_at_ms if t is not None else None
        if changed:
            log.info("margin table %s", f"loaded (fitted_at={self.edge_table_fitted_at}, {len(t.nodes)} nodes)" if t else "absent: base_edge fallback")
        return changed

    def _margin_terms(self, d: "Decision", now: int):
        sports = {l.game_key.split("-")[0] for l in d.legs if l.game_key}
        sport = "mixed" if len(sports) > 1 else (next(iter(sports)) if sports else "")
        same_game = any(len(c.position_ids) > 1 for c in d.clusters)
        sigma = (d.u_m or 0.0) + (d.u_d or 0.0)
        seg = Segment.of(sport, d.n_legs, d.direction, same_game, sigma, self._sigma_cuts)
        if self._edge_table is None:        # no table file: margin_for would fall back to the default path, so answer here
            return seg, MarginTerms(self.cfg.base_edge, 0.0, 0.0, 0.0, False)
        return seg, margin_for(seg, requester=d.requester or None, table=self._edge_table, default_edge=self.cfg.base_edge, now_ms=now)

    # -- entry point ------------------------------------------------------------------
    def handle(self, rfq: Any, store: Any) -> PaperQuote | None:
        t0 = time.perf_counter()
        d = self.evaluate(rfq, store)
        d.latency_ms = (time.perf_counter() - t0) * 1000
        self.latencies.append(d.latency_ms)
        self.counts[d.decision] = self.counts.get(d.decision, 0) + 1
        if d.quoted:
            self.samples.append(d.reason)
            if self.inventory is not None and d.position is not None and d.assessment is not None:
                try:
                    self.inventory.on_quoted(d.position, d.assessment, requester=d.requester, condition_id=d.condition_id)
                except Exception as e:  # pragma: no cover
                    log.warning("inventory update failed: %s", e)
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
        deadline = int(getattr(rfq, "submission_deadline_ms", 0) or 0)
        d.deadline_ms = deadline or None
        d.expires_ms = (deadline if deadline > 0 else now + 400) + self.horizon_ms
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
            le = LegEval(r["position_id"], r["slug"], r["outcome"], gk, condition_id=r["condition_id"])
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
        margin0 = self.cfg.k_m * d.u_m + self.cfg.k_d * d.u_d + d.e_b          # stage 2 formula = fallback
        if self.cfg.edge:
            seg, terms = self._margin_terms(d, now)
            d.edge_segment, d.edge_level, d.edge_source, d.edge_n = terms.segment or seg.key, terms.level, terms.source, terms.n
            d.curse_mult, d.info_premium, d.wallet_premium = terms.curse_mult, terms.info_premium, terms.wallet_premium
            if terms.blocked:
                return d.skip("wallet_blocked", f"{d.requester[:12]} {terms.block_reason or 'blocked'}")
            if terms.source in ("model", "measured"):
                # fitted node: its base and winner's-curse multiplier replace base_edge and k_m = k_d = 1
                d.e_b = terms.base
                margin0 = terms.margin(d.u_m + d.u_d)
            else:
                # default / no_prints / no_data: no fitted curse term, keep the stage 2 margin; carry the premiums
                margin0 += terms.info_premium + terms.wallet_premium
        # 4. inventory: caps and skew against the live book (hot path, closed forms; off -> stage 2)
        inv = self.inventory if (self.inventory is not None and self.cfg.risk) else None
        if inv is not None:
            price0 = self._price(p_fair, margin0, mv.maker_sells)
            if price0 is None:
                return d.skip("price_below_min", f"fair {p_fair:.3f} - margin {margin0:.3f}")
            if mv.maker_sells and price0 > self.cfg.buy_safety_max:
                return d.skip("price_above_safety", f"{price0:.3f} > {self.cfg.buy_safety_max}")
            sign = -1 if mv.maker_sells else 1
            req0 = requested_shares_e6(mv.size_unit, d.requested_e6, int(round(price0 * SHARE_E6))) / SHARE_E6
            cand = position_from_decision_legs(d.rfq_id, d.legs, sign, req0, price0, now, d.expires_ms, entries,
                                               requester=d.requester, condition_id=d.condition_id)
            try:
                a = inv.assess(cand, requester=d.requester, condition_id=d.condition_id)
            except Exception as e:
                return d.skip("risk_error", f"{type(e).__name__}: {e}")
            d.assessment, d.position = a, cand
            d.e_i = a.e_i
            d.allowed_size, d.headroom, d.d_es1_per_share = a.allowed_size, a.as_dict()["headroom"], a.d_es1_per_share
            d.pending_same_combo, d.pending_requesters, d.book = a.pending_same_combo, a.pending_requesters, a.book
        d.margin = max(0.0, margin0 + d.e_i)          # the skew never takes the price through fair
        # 5. price for the maker's side
        price = self._price(p_fair, d.margin, mv.maker_sells)
        if price is None:
            return d.skip("price_below_min", f"fair {p_fair:.3f} - margin {d.margin:.3f}")
        if mv.maker_sells and price > self.cfg.buy_safety_max:
            return d.skip("price_above_safety", f"{price:.3f} > {self.cfg.buy_safety_max}")
        d.price_e6 = int(round(price * SHARE_E6))
        # 6. size: requested, quality tiers, Kelly, caps
        requested = requested_shares_e6(mv.size_unit, d.requested_e6, d.price_e6)
        d.requested_shares = requested / SHARE_E6
        worst = max((l.tier for l in d.legs), key=TIER_ORDER.index)
        cap_usd = self.cfg.max_size_usd * self.cfg.tier_size_mult.get(worst, 0.0) * self.cfg.legs_mult(d.n_legs)
        cap_shares = int(cap_usd * SHARE_E6 / price) if price > 0 else 0
        size = min(requested, cap_shares)
        cut_by = None
        if inv is not None:
            assert d.assessment is not None
            # Kelly edge = margin net of the inventory charge (risk-adjusted): a hedge quoted at fair
            # still sizes on the tail risk it removes; an adder sizes on its margin before the widening
            d.kelly_size = kelly_size(d.margin - d.e_i, p_fair, inv.caps.capital_usd, self.cfg.kelly_fraction)
            k_e6 = int(d.kelly_size * SHARE_E6)
            if k_e6 < size:
                size, cut_by = k_e6, "kelly"
            a_e6 = int(min(d.assessment.allowed_size, 1e12) * SHARE_E6)
            if a_e6 < size:
                size, cut_by = a_e6, f"cap:{d.assessment.binding_cap}"
                d.binding_cap = d.assessment.binding_cap
            if a_e6 <= 0 or (notional_e6(a_e6 - a_e6 % 1000, d.price_e6) < self.cfg.min_size_usd * SHARE_E6):
                d.binding_cap = d.assessment.binding_cap
                return d.skip(f"cap:{d.assessment.binding_cap}",
                              f"allowed {d.assessment.allowed_size:.1f} sh ({d.assessment.detail.get(d.assessment.binding_cap or '', '')}); "
                              f"headroom { {k: (round(v, 1) if v is not None else None) for k, v in (d.headroom or {}).items()} }")
        size -= size % 1000   # 0.001-share granularity
        if notional_e6(size, d.price_e6) < self.cfg.min_size_usd * SHARE_E6:
            return d.skip("size_below_min", f"size {size / SHARE_E6:.2f} sh at {price:.3f} < ${self.cfg.min_size_usd}")
        d.size_e6 = size
        if d.position is not None:
            d.position.size, d.position.price = size / SHARE_E6, price
        d.decision = "quoted"
        joint_n = sum(1 for c in d.clusters if c.source == "joint")
        impossible = " [impossible]" if p_fair == 0 else ""
        d.reason = (f"fair={p_fair:.4f}{impossible} ({'indep' if d.certified else 'clusters'} {len(d.clusters)}{', ' + str(joint_n) + ' joint' if joint_n else ''}) "
                    f"u_m={d.u_m:.4f} u_d={d.u_d:.4f} base={d.e_b:.3f} inv={d.e_i:+.4f} -> {'ask' if mv.maker_sells else 'bid'} {price:.3f} x {size / SHARE_E6:.0f}"
                    f"{' cut:' + cut_by if cut_by else ''}{' [round_trip]' if rt else ''}"
                    f"{f' pending={d.pending_same_combo}/{d.pending_requesters}req' if d.pending_same_combo else ''}; legs: "
                    + " | ".join(l.short() for l in d.legs))
        return d

    def _price(self, p_fair: float, margin: float, maker_sells: bool) -> float | None:
        """Tick-rounded price away from fair for the maker's side; None when a bid falls below price_min."""
        tick = self.cfg.tick
        if maker_sells:
            price = math.ceil((p_fair + margin) / tick) * tick
            price = min(max(price, self.cfg.price_min), 1.0)
            assert price >= p_fair - 1e-12, "never sell below fair"
            return price
        price = math.floor((p_fair - margin) / tick) * tick
        if price < self.cfg.price_min:
            return None
        price = min(price, self.cfg.price_max)
        assert price <= p_fair + 1e-12, "never buy above fair"
        return price

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
                "cache": dict(self.cache.stats), "games": len(self.cache.entries),
                "inventory": self.inventory.summary() if self.inventory is not None else None}
