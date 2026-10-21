"""Trigger and budget: which probes are due, in what order, and sending them.

Trigger (paper): a print on a combo we quoted (decision joined to the trade on rfq_id, as
``rfqbot.edge.fills`` does). Live: our own fill (``trades.is_ours``). Each trigger is probed after
``delay_sec`` and again at each of ``horizons_sec``; the trigger's own direction first, the opposite
one too when the budget allows (a two-sided mark). Budget: ``max_per_hour`` overall, ``max_per_combo``
per combo per day, ``min_spacing_sec`` between probes on one combo. Priority: segments with the fewest
probes so far, then the largest quoted size.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..edge.config import EdgeConfig
from ..edge.data import DecisionRec, Print, load_decisions, load_prints
from ..edge.fairs import FairSource
from ..edge.segments import Segment
from .config import ProbeConfig
from .disguise import ExtraLeg, LegMeta, back_out, candidate_pool, leg_meta, pick_extra_leg
from .gateway import E6, Gateway, ProbeRequest
from .store import ProbeStore, now_ms

log = logging.getLogger(__name__)
SIGMA_CUTS = (0.005, 0.009)


@dataclass
class Trigger:
    rfq_id: str
    kind: str                       # print | fill
    condition_id: str
    position_ids: list[str]
    direction: str                  # the trigger's requester direction
    price: float
    size_e6: int
    at_ms: int
    requester: str
    decision: DecisionRec | None
    segment: str

    @property
    def our_price(self) -> float | None:
        return self.decision.price if self.decision else None


def triggers(cfg: ProbeConfig, since_ms: int, until_ms: int | None = None, *, decisions: Sequence[DecisionRec] | None = None,
             prints: Sequence[Print] | None = None) -> list[Trigger]:
    ecfg = EdgeConfig(decisions_db=cfg.decisions_db, rfq_db=cfg.rfq_db, books_db=cfg.books_db, kalshi_db=cfg.kalshi_db, sports=cfg.sports)
    decs = list(decisions) if decisions is not None else load_decisions(ecfg, since_ms - 120_000, until_ms)
    by_rfq = {d.rfq_id: d for d in decs}
    prs = list(prints) if prints is not None else load_prints(ecfg, since_ms, until_ms, sports=cfg.sports)
    out: list[Trigger] = []
    for p in prs:
        d = by_rfq.get(p.rfq_id)
        if d is None or not p.position_ids:
            continue
        seg = Segment.of(d.sport, d.n_legs, d.direction, d.same_game, d.sigma, SIGMA_CUTS).key
        out.append(Trigger(p.rfq_id, "print", p.condition_id or "", list(p.position_ids), p.direction, p.price, p.size_e6,
                           p.executed_at_ms, p.requester, d, seg))
    return out


@dataclass
class Plan:
    trigger: Trigger
    horizon_sec: int
    direction: str
    due_ms: int
    priority: tuple = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.trigger.rfq_id, self.horizon_sec, self.direction)


@dataclass
class PlanStats:
    candidates: int = 0
    due: int = 0
    selected: int = 0
    already_done: int = 0
    too_late: int = 0
    dropped_hourly: int = 0
    dropped_combo_cap: int = 0
    dropped_spacing: int = 0
    budget_remaining: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def plan(trigs: Sequence[Trigger], store: ProbeStore, cfg: ProbeConfig, now: int) -> tuple[list[Plan], PlanStats]:
    st = PlanStats()
    seg_counts = store.segment_counts(now - 86_400_000)
    horizons = [int(cfg.delay_sec)] + [h for h in cfg.horizons_sec if h > cfg.delay_sec]
    cands: list[Plan] = []
    for t in trigs:
        dirs = [t.direction] + ([("SELL" if t.direction == "BUY" else "BUY")] if cfg.both_directions else [])
        for h in horizons:
            for i, d in enumerate(dirs):
                st.candidates += 1
                due = t.at_ms + h * 1000
                if due > now:
                    continue
                st.due += 1
                if store.done(t.rfq_id, h, d):
                    st.already_done += 1
                    continue
                if now - due > cfg.max_late_sec * 1000:
                    st.too_late += 1
                    continue
                cands.append(Plan(t, h, d, due, (seg_counts.get(t.segment, 0), i, -t.size_e6, h, due)))
    cands.sort(key=lambda p: p.priority)
    budget = max(0, cfg.max_per_hour - store.count_since(now - 3_600_000))
    st.budget_remaining = budget
    chosen: list[Plan] = []
    combo_used: dict[str, int] = {}
    combo_last: dict[str, tuple[int, tuple[str, int]]] = {}   # condition -> (when, (trigger rfq, horizon)) chosen in this plan
    for p in cands:
        if len(chosen) >= budget:
            st.dropped_hourly += 1
            continue
        c = p.trigger.condition_id
        used = store.combo_count_since(c, now - 86_400_000) + combo_used.get(c, 0)
        if used >= cfg.max_per_combo:
            st.dropped_combo_cap += 1
            continue
        # spacing applies between probes on one combo, except the two directions of the same (trigger, horizon) pair
        prev = combo_last.get(c)
        last = prev[0] if prev else store.last_combo_ms(c)
        same_pair = prev is not None and prev[1] == (p.trigger.rfq_id, p.horizon_sec)
        if last is not None and now - last < cfg.min_spacing_sec * 1000 and not same_pair:
            st.dropped_spacing += 1
            continue
        chosen.append(p)
        combo_used[c] = combo_used.get(c, 0) + 1
        combo_last[c] = (now, (p.trigger.rfq_id, p.horizon_sec))
        seg_counts[p.trigger.segment] = seg_counts.get(p.trigger.segment, 0) + 1
    st.selected = len(chosen)
    return chosen, st


def build_request(p: Plan, cfg: ProbeConfig, extra: ExtraLeg | None) -> ProbeRequest:
    legs = tuple(p.trigger.position_ids) + ((extra.position_id,) if extra else ())
    if p.direction == "BUY":
        return ProbeRequest(legs, "BUY", "notional", int(round(cfg.size_usd * E6)))
    est = p.trigger.our_price or p.trigger.price or 0.5
    if extra:
        est *= extra.p_L
    shares = cfg.size_usd / max(est, 0.001)
    return ProbeRequest(legs, "SELL", "shares", int(round(shares * E6)))


def execute(plans: Sequence[Plan], gateway: Gateway, store: ProbeStore, cfg: ProbeConfig, *, now: int | None = None,
            fairs: FairSource | None = None, pool: Sequence[ExtraLeg] | None = None, metas: dict[str, LegMeta] | None = None,
            mode: str | None = None) -> list[dict[str, Any]]:
    """Send each plan (disguised when configured and a leg exists), record the row. Returns the rows."""
    now = now if now is not None else now_ms()
    mode = mode or gateway.name
    if pool is None:
        pool = candidate_pool(cfg, now) if cfg.disguise else []
    if metas is None:
        pids = {pid for p in plans for pid in p.trigger.position_ids}
        metas = leg_meta(pids, books_db=cfg.books_db, rfq_db=cfg.rfq_db) if pids else {}
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(plans):
        t = p.trigger
        combo = [metas.get(pid) or LegMeta(pid, None, None, None, None, None, None) for pid in t.position_ids]
        extra = pick_extra_leg(combo, now, pool, salt=i) if cfg.disguise and pool else None
        req = build_request(p, cfg, extra)
        body = req.body(cfg)
        resp = gateway.create(req)
        fair = fairs.combo(t.position_ids, [l.slug and _prefix(l.slug) for l in combo], now) if fairs is not None else None
        backed, err = (None, None)
        if resp.price is not None:
            backed, err = back_out(resp.price, extra.p_L, extra.spread) if extra else (resp.price, 0.0)
        d = t.decision
        row = {"created_ms": now, "mode": mode, "trigger_rfq_id": t.rfq_id, "trigger_kind": t.kind, "trigger_condition": t.condition_id,
               "trigger_direction": t.direction, "trigger_price": t.price, "trigger_size_e6": t.size_e6, "trigger_at_ms": t.at_ms,
               "trigger_requester": t.requester, "decision_id": d.id if d else None, "our_price": d.price if d else None,
               "our_p_fair": d.p_fair if d else None, "our_margin": (abs(d.price - d.p_fair) if d else None),
               "our_sigma": d.sigma if d else None, "segment": t.segment, "horizon_sec": p.horizon_sec, "direction": p.direction,
               "legs_json": json.dumps(t.position_ids), "extra_leg_json": json.dumps(extra.as_dict()) if extra else None,
               "p_L": extra.p_L if extra else None, "p_L_spread": extra.spread if extra else None, "body_json": json.dumps(body),
               "response_json": json.dumps(resp.raw) if resp.raw else None, "status": resp.status, "probe_rfq_id": resp.rfq_id,
               "requestor_public_id": resp.requestor_public_id, "blended_price": resp.price, "backed_out_price": backed,
               "backed_out_err": err, "leg_fair_send": fair.model if fair else None, "leg_fair_poly": fair.poly if fair else None,
               "leg_fair_kalshi": fair.kalshi if fair else None, "latency_ms": resp.latency_ms, "error": resp.error}
        row["id"] = store.put(row)
        rows.append(row)
        if resp.requestor_public_id and not store.meta("requester_id"):
            store.set_meta("requester_id", resp.requestor_public_id)
            log.warning("learned our requestor_public_id %s: set PROBE_REQUESTER_ID=%s so the quoter skips our probes",
                        resp.requestor_public_id, resp.requestor_public_id)
    return rows


def _prefix(slug: str) -> str | None:
    from ..edge.data import game_prefix_of
    return game_prefix_of(slug)
