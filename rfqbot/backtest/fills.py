"""Counterfactual fills for the replayed quotes, through ``rfqbot.edge.fills`` (the one fill rule).

The live bot never submits in paper mode, so the only fill evidence is the venue's trade print on the RFQ:
it is the best competing quote of a first-price auction. Our quote at ``price`` wins iff it is at or inside
the print (``would_win``), and fills ``min(1, our_size / print_size)`` of it (``fill_fraction``). Two flags:

  win     our price at or inside the print                (ties count; the edge package's rule)
  strict  our price inside the print by at least one tick (the range the report shows)

Limits, inherited from the rule and recorded in docs/future-work.md: no size priority, no later quotes by
other makers reacting to ours, prints of RFQs we would have quoted differently are the same prints.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..edge.config import EdgeConfig
from ..edge.data import DecisionRec, LegInfo, Print, load_prints
from ..edge.fills import Fill, counterfactual

E6 = 1_000_000


@dataclass(frozen=True)
class FillResult:
    printed: bool = False
    win: bool = False
    strict: bool = False
    frac: float = 0.0
    print_price: float | None = None
    print_size_e6: int | None = None
    executed_at_ms: int | None = None
    delta: float | None = None          # competitor gap in our win direction (print - fair for a requester BUY)
    fill_size_e6: int = 0               # frac x our size under the rule that drives the book

    def as_dict(self) -> dict[str, Any]:
        return {"printed": int(self.printed), "fill_win": int(self.win), "fill_strict": int(self.strict), "fill_frac": self.frac,
                "print_price": self.print_price, "print_size_e6": self.print_size_e6, "executed_at_ms": self.executed_at_ms,
                "delta": self.delta, "fill_size_e6": self.fill_size_e6}


def load_print_index(rfq_db: str | Path, start_ms: int, end_ms: int, *, sports: tuple[str, ...] = ("nfl", "nba"),
                     slack_ms: int = 120_000) -> dict[str, Print]:
    """Prints of RFQs first seen in the window (executed up to ``slack_ms`` after it), keyed by rfq_id."""
    cfg = EdgeConfig(rfq_db=Path(rfq_db))
    return {p.rfq_id: p for p in load_prints(cfg, start_ms, end_ms + slack_ms, sports=sports)}


def decision_rec(d: Any, idx: int = 0) -> DecisionRec:
    """An assembler Decision as the edge package's DecisionRec (quoted decisions only)."""
    from ..edge.data import game_prefix_of
    legs = [LegInfo(l.position_id, game_prefix_of(l.slug), l.p, l.err, l.kalshi_confidence or "none") for l in d.legs]
    same_game = any(len(c.position_ids) > 1 for c in d.clusters)
    return DecisionRec(id=idx, rfq_id=d.rfq_id, at_ms=d.at_ms, direction=d.direction, requested_unit=d.requested_unit,
                       requested_e6=d.requested_e6, n_legs=d.n_legs, p_fair=float(d.p_fair), u_m=float(d.u_m or 0.0),
                       u_d=float(d.u_d or 0.0), margin=float(d.margin or 0.0), price=int(d.price_e6) / E6,
                       size_e6=int(d.size_e6), legs=legs, same_game=same_game, round_trip="[round_trip]" in (d.reason or ""),
                       requester=d.requester or "")


def evaluate_fill(d: Any, prints: dict[str, Print], *, tick: float = 0.001, rule: str = "win") -> FillResult:
    """Win / strict / fraction of a quoted decision against the print on its rfq_id; no print -> never fills."""
    if not d.quoted or d.price_e6 is None or d.size_e6 is None:
        return FillResult()
    pr = prints.get(d.rfq_id)
    if pr is None:
        return FillResult()
    f: Fill = counterfactual([decision_rec(d)], [pr])[0]
    if not f.printed:
        return FillResult()
    price = int(d.price_e6) / E6
    # strict: inside the print by a full tick (ticks compared with a half-tick guard against float noise)
    gap_price = (pr.price - price) if d.direction == "BUY" else (price - pr.price)
    strict = f.win_actual and gap_price >= tick - 1e-9
    driving = strict if rule == "strict" else f.win_actual
    return FillResult(True, bool(f.win_actual), bool(strict), f.fill_frac if f.win_actual else 0.0, pr.price, pr.size_e6,
                      pr.executed_at_ms, f.delta, int(f.fill_frac * d.size_e6) if driving else 0)
