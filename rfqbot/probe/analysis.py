"""Probe analysis: combo-level markout vs the leg-book markout, and the per-RFQ competitor gap vs the
segment mean the margin optimiser uses.

Signs are the maker's: on a BUY trigger we sold YES, so a higher probe price is a loss
(markout = -(price - our price)); on a SELL trigger we bought YES, markout = +(price - our price).
The leg-book counterpart uses the recombined leg fair at the same instant in place of the probe price.
The competitor gap is ``rfqbot.edge.fills.competitor_gap`` with the backed-out probe price as the
competing quote, in our win direction.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..edge.fills import competitor_gap
from ..edge.segments import Segment
from ..edge.table import load_table
from .config import ProbeConfig
from .store import ProbeStore


def combo_markout(trigger_direction: str, our_price: float, probe_price: float) -> float:
    sign = -1.0 if trigger_direction == "BUY" else 1.0
    return sign * (probe_price - our_price)


def leg_markout(trigger_direction: str, our_price: float, leg_fair: float) -> float:
    return combo_markout(trigger_direction, our_price, leg_fair)


def gap(trigger_direction: str, our_fair: float, probe_price: float) -> float:
    return competitor_gap(trigger_direction, our_fair, probe_price)


def probe_gap(rfq_id: str, *, db: str | Path | ProbeStore | None = None, horizon_sec: int | None = None) -> float | None:
    """Per-RFQ competitor gap from the earliest priced probe on that trigger (or the given horizon),
    for the edge pipeline to use in place of the segment delta later."""
    store = db if isinstance(db, ProbeStore) else ProbeStore(db or ProbeConfig().db)
    try:
        for r in store.for_trigger(rfq_id):
            if horizon_sec is not None and int(r["horizon_sec"]) != horizon_sec:
                continue
            if r["backed_out_price"] is None or r["our_p_fair"] is None or r["direction"] != r["trigger_direction"]:
                continue
            return gap(r["trigger_direction"], float(r["our_p_fair"]), float(r["backed_out_price"]))
    finally:
        if not isinstance(db, ProbeStore):
            store.close()
    return None


@dataclass
class Stat:
    n: int
    mean: float | None
    se: float | None

    @staticmethod
    def of(xs: Sequence[float]) -> "Stat":
        n = len(xs)
        if n == 0:
            return Stat(0, None, None)
        m = sum(xs) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
        return Stat(n, m, sd / math.sqrt(n))


def _pt(x: float | None, d: int = 2) -> str:
    return "-" if x is None else f"{100 * x:+.{d}f}"


def _table(rows: list[list[Any]], header: list[str]) -> str:
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(header)]
    line = lambda r: "  ".join(str(c).ljust(w[i]) for i, c in enumerate(r))
    return "\n".join([line(header), line(["-" * x for x in w])] + [line(r) for r in rows])


def segment_table(rows: Iterable[Any], cfg: ProbeConfig) -> list[str]:
    """Per segment and horizon: probes, priced, combo vs leg-book markout, their disagreement, the
    per-RFQ gap next to the optimiser's segment delta."""
    table = load_table(max_age_s=0)
    groups: dict[tuple[str, int, str], list[Any]] = {}
    for r in rows:
        groups.setdefault((r["segment"] or "", int(r["horizon_sec"]), r["direction"] == r["trigger_direction"] and "same" or "opp"), []).append(r)
    out = []
    for (seg, h, side), rs in sorted(groups.items()):
        priced = [r for r in rs if r["backed_out_price"] is not None and r["our_price"] is not None]
        cm = Stat.of([combo_markout(r["trigger_direction"], r["our_price"], r["backed_out_price"]) for r in priced])
        lb = [r for r in priced if r["leg_fair_send"] is not None]
        lm = Stat.of([leg_markout(r["trigger_direction"], r["our_price"], r["leg_fair_send"]) for r in lb])
        diff = Stat.of([combo_markout(r["trigger_direction"], r["our_price"], r["backed_out_price"]) - leg_markout(r["trigger_direction"], r["our_price"], r["leg_fair_send"]) for r in lb])
        same = [r for r in priced if side == "same" and r["our_p_fair"] is not None]
        g = Stat.of([gap(r["trigger_direction"], r["our_p_fair"], r["backed_out_price"]) for r in same])
        node = table.resolve(Segment.parse(seg)) if (table and seg) else None
        seg_delta = node.get("delta") if node else None
        err = Stat.of([r["backed_out_err"] for r in priced if r["backed_out_err"] is not None])
        out.append([seg, h, side, len(rs), len(priced), _pt(cm.mean), _pt(cm.se), lm.n, _pt(lm.mean), _pt(diff.mean), _pt(diff.se),
                    g.n, _pt(g.mean), _pt(seg_delta), _pt(err.mean, 3)])
    lines = ["probes by segment / horizon (pt = probability points x100; same = probe in the trigger's direction)",
             _table(out, ["segment", "h_s", "dir", "n", "priced", "combo_mo", "se", "n_leg", "leg_mo", "diff", "se", "n_gap", "gap", "seg_delta", "bo_err"])]
    return lines


def toxicity_table(rows: Iterable[Any]) -> list[str]:
    groups: dict[str, list[Any]] = {}
    for r in rows:
        if r["backed_out_price"] is not None and r["our_price"] is not None and r["direction"] == r["trigger_direction"]:
            groups.setdefault(r["trigger_requester"] or "", []).append(r)
    out = []
    for req, rs in groups.items():
        by_h: dict[int, list[float]] = {}
        for r in rs:
            by_h.setdefault(int(r["horizon_sec"]), []).append(combo_markout(r["trigger_direction"], r["our_price"], r["backed_out_price"]))
        hs = sorted(by_h)
        first, last = Stat.of(by_h[hs[0]]), Stat.of(by_h[hs[-1]])
        out.append([req[:14], len(rs), len({r["trigger_rfq_id"] for r in rs}), hs[0], _pt(first.mean), hs[-1], _pt(last.mean)])
    out.sort(key=lambda r: (r[6] != "-", r[6]))
    return ["toxicity by requester of the triggering print (combo markout in pt, first and last horizon)",
            _table(out, ["requester", "probes", "rfqs", "h0", "markout", "h1", "markout"])]


def report_lines(store: ProbeStore, cfg: ProbeConfig, since_ms: int | None = None) -> list[str]:
    rows = store.rows(since_ms)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    disguised = sum(1 for r in rows if r["extra_leg_json"])
    lines = [f"probes: {len(rows)} ({', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))}); disguised {disguised}; "
             f"triggers {len({r['trigger_rfq_id'] for r in rows})}; requester id {store.meta('requester_id') or 'unknown'}"]
    lines += segment_table(rows, cfg)
    lines += toxicity_table(rows)
    return lines
