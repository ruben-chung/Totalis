"""One ``fit``: decisions + prints -> counterfactual fills -> fill curves -> markouts -> curse ->
wallet scores -> optimiser -> margin_table.json. Everything is also written to data/edge.db."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import EdgeConfig
from .curse import adverse_move, estimate as curse_estimate
from .data import DecisionRec, Print, load_decisions, load_prints
from .fairs import FairSource
from .fillcurve import CurveTable, fit_curves
from .fills import Fill, counterfactual
from .markout import info_premium_for, node_means, refresh_markouts, summarize
from .optimize import NodeResult, optimize_node
from .segments import GLOBAL, Segment
from .store import EdgeStore, now_ms
from .table import write_table
from .wallets import WalletObs, WalletScore, load_manual, score_wallets

log = logging.getLogger(__name__)
CURSE_HORIZONS = (15, 30)
CURSE_REFS = ("model", "poly", "kalshi")


@dataclass
class FitReport:
    now_ms: int
    since_ms: int | None
    decisions: list[DecisionRec]
    prints: list[Print]
    fills: list[Fill]
    curves: CurveTable
    markout_counts: dict[str, int]
    markout_rows: list[Any]
    curse: list[Any]
    nodes: dict[str, NodeResult]
    wallets: dict[str, WalletScore]
    wallet_obs: int
    table: dict[str, Any] = field(default_factory=dict)
    fair_calls: int = 0


def fit(cfg: EdgeConfig, store: EdgeStore, *, since_ms: int | None = None, until_ms: int | None = None,
        now: int | None = None, fairs: FairSource | None = None, write: bool = True,
        decisions: list[DecisionRec] | None = None, prints: list[Print] | None = None) -> FitReport:
    now = now if now is not None else now_ms()
    fairs = fairs or FairSource(cfg)
    t0 = time.perf_counter()
    decisions = decisions if decisions is not None else load_decisions(cfg, since_ms, until_ms)
    prints = prints if prints is not None else load_prints(cfg, since_ms, until_ms, sports=cfg.sports)
    # prints for the decisions' RFQs regardless of sport filter (a decision implies a quoted sport anyway)
    dec_ids = {d.rfq_id for d in decisions}
    by_rfq = {p.rfq_id: p for p in prints}
    missing = dec_ids - set(by_rfq)
    if missing and (decisions is not None):
        for p in load_prints(cfg, since_ms, until_ms, rfq_ids=missing):
            by_rfq.setdefault(p.rfq_id, p)
    fills = counterfactual(decisions, by_rfq.values())
    store.put_fills(_fill_row(f, cfg) for f in fills)
    curves = fit_curves(fills, cfg)
    cell_n = {k: curves.nodes[k].n for k in curves.cells if k in curves.nodes}
    store.put_curves(curves.nodes.values(), curves.cells, cell_n, now)
    log.info("fills %d (printed %d, wins %d) curves %d nodes in %.1fs", len(fills), sum(f.printed for f in fills),
             sum(f.win_actual for f in fills), len(curves.nodes), time.perf_counter() - t0)

    counts = refresh_markouts(cfg, store, fairs, since_ms=since_ms, until_ms=until_ms, now=now, prints=prints)
    rows = store.markout_rows(since_ms)
    log.info("markouts %s (compute_game calls %d) in %.1fs", counts, fairs.calls, time.perf_counter() - t0)

    # later fairs per rfq for the curse: (ref, horizon) -> fair_h
    later: dict[str, dict[tuple[str, int], float]] = {}
    for r in rows:
        if r["fair_h"] is not None:
            later.setdefault(r["rfq_id"], {})[(r["ref"], int(r["horizon_min"]))] = float(r["fair_h"])
    curse_est = []
    measured30: dict[str, list[float | None]] = {}
    for key, curve in curves.nodes.items():
        if curve.n_printed == 0:
            continue
        members = [f for f in fills if f.printed and curve.segment.contains(f.segment(cfg.sigma_cuts))]
        for h in CURSE_HORIZONS:
            for ref in CURSE_REFS:
                obs = []
                for f in members:
                    fh = later.get(f.rfq_id, {}).get((ref, h))
                    mv = adverse_move(f.direction, f.p_fair, fh) if fh is not None else None
                    obs.append((f.delta, mv, f.undercut if f.win_actual else None))
                est = curse_estimate(key, h, ref, curve.grid, curve.sigma_mean, curve.delta_mean, curve.n_printed, obs)
                curse_est.append(est)
                if h == 30 and ref == "model":
                    measured30[key] = est.measured
    store.put_curse(curse_est, now)

    means = node_means(rows, ref="model", horizon_min=30)
    nodes: dict[str, NodeResult] = {}
    for key, curve in curves.nodes.items():
        if curve.n < cfg.n_min and curve.segment != GLOBAL:
            continue
        prem, _, _ = info_premium_for(key, means, cfg.n_min)
        nodes[key] = optimize_node(curve, cfg, info_premium=prem, measured=measured30.get(key))
    # every observed full cell is priced on the node it resolves to, but with its own sigma
    for cell, (node_key, level) in curves.cells.items():
        if cell in nodes or not node_key:
            continue
        own = curves.nodes.get(cell)
        node_curve = curves.nodes[node_key]
        prem, _, _ = info_premium_for(cell, means, cfg.n_min)
        nodes[cell] = optimize_node(node_curve, cfg, info_premium=prem, measured=measured30.get(node_key),
                                    sigma=own.sigma_mean if own else None, segment=cell, level=level)

    obs = wallet_observations(fills, prints, rows, nodes, cfg)
    scores = score_wallets(obs, cfg, now, prior=store.wallet_scores(), manual=load_manual(cfg.wallet_list))
    store.put_wallets(scores.values(), now)
    store.set_meta("last_fit", {"at_ms": now, "since_ms": since_ms, "decisions": len(decisions), "prints": len(prints),
                                "fills_printed": sum(f.printed for f in fills), "nodes": len(nodes), "wallets": len(scores)})
    table: dict[str, Any] = {}
    if write:
        table = write_table(cfg.table_path, [n.as_dict() for n in nodes.values()], {k: v.as_dict() for k, v in scores.items()}, cfg,
                            fitted_at_ms=now, extra={"cells": {k: {"node": v[0], "level": v[1], "n": cell_n.get(k, 0)} for k, v in curves.cells.items()},
                                                     "counts": {"decisions": len(decisions), "prints": len(prints),
                                                                "printed": sum(f.printed for f in fills), "wins": sum(f.win_actual for f in fills)}})
    return FitReport(now, since_ms, decisions, prints, fills, curves, counts, rows, curse_est, nodes, scores, len(obs), table, fairs.calls)


def wallet_observations(fills: list[Fill], prints: list[Print], markout_rows: list[Any], nodes: dict[str, NodeResult],
                        cfg: EdgeConfig) -> list[WalletObs]:
    """One observation per print in the quoted sports with a 30-min model markout: profit = the markup
    we charged (our logged quote on that RFQ) or would charge (the node's m*) plus the maker markout.
    Paper mode has no real fills, so a wallet is scored on its prints as if we had filled them."""
    m30: dict[str, float] = {}
    seg_of: dict[str, str] = {}
    for r in markout_rows:
        if r["ref"] == "model" and int(r["horizon_min"]) == 30 and r["markout"] is not None:
            m30[r["rfq_id"]] = float(r["markout"])
            seg_of[r["rfq_id"]] = r["segment"] or ""
    by_rfq = {f.rfq_id: f for f in fills}
    out: list[WalletObs] = []
    for p in prints:
        mk = m30.get(p.rfq_id)
        if mk is None or not p.requester:
            continue
        f = by_rfq.get(p.rfq_id)
        if f is not None:
            markup, win, rt = f.margin, f.win_actual, f.round_trip
        else:
            markup, win, rt = _m_star(seg_of.get(p.rfq_id, ""), nodes, cfg), False, False
        out.append(WalletObs(p.requester, markup + mk, p.executed_at_ms, win=win, round_trip=rt))
    return out


def _m_star(seg_key: str, nodes: dict[str, NodeResult], cfg: EdgeConfig) -> float:
    if seg_key:
        for s in Segment.parse(seg_key).ladder():
            n = nodes.get(s.key)
            if n is not None and n.source in ("model", "measured"):
                return n.m_star
    return cfg.default_edge


def _fill_row(f: Fill, cfg: EdgeConfig) -> dict[str, Any]:
    return {"decision_id": f.decision_id, "rfq_id": f.rfq_id, "at_ms": f.at_ms, "direction": f.direction, "sport": f.sport,
            "n_legs": f.n_legs, "same_game": int(f.same_game), "p_fair": f.p_fair, "sigma": f.sigma, "margin": f.margin,
            "price": f.price, "size_e6": f.size_e6, "requester": f.requester, "round_trip": int(f.round_trip),
            "printed": int(f.printed), "print_price": f.print_price, "print_size_e6": f.print_size_e6,
            "executed_at_ms": f.executed_at_ms, "delta": f.delta, "win_actual": int(f.win_actual), "fill_frac": f.fill_frac,
            "undercut": f.undercut, "segment": f.segment(cfg.sigma_cuts).key}
