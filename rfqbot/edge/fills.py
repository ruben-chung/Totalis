"""Counterfactual fills: join a quoted decision to the trade print on its rfq_id.

The print is the best competing quote (RFQ = first-price auction among makers), so at markup m
over our fair we would have won iff
    BUY  (requester buys YES, we sell):  p_fair + m <= print
    SELL (requester sells YES, we buy):  p_fair - m >= print
Both collapse to ``m <= delta`` with the competitor gap ``delta = print - p_fair`` (BUY) or
``p_fair - print`` (SELL), measured in our win direction. A decision without a print never fills
(the requester did not trade), so it counts in F(m)'s denominator at every m.
Size: a win fills ``min(1, our_size / printed_size)`` of the print.

Round trips: quote assembly tags a decision ``[round_trip]`` when the requester polled BUY and SELL of
one combo within 10 s. That fires on ~43% of quotes and describes polling, not trading; it is kept on
the Fill for the logs. ``adverse_round_trips`` is the actual event: we (would have) filled the
requester and the same wallet printed the opposite side of the same combo within ``ROUND_TRIP_MS``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .data import DecisionRec, Print
from .segments import Segment

ROUND_TRIP_MS = 10_000


def competitor_gap(direction: str, p_fair: float, print_price: float) -> float:
    return print_price - p_fair if direction == "BUY" else p_fair - print_price


def would_win(direction: str, p_fair: float, m: float, print_price: float) -> bool:
    if direction == "BUY":
        return p_fair + m <= print_price + 1e-12
    if direction == "SELL":
        return p_fair - m >= print_price - 1e-12
    raise ValueError(f"direction {direction!r}")


def fill_fraction(our_size_e6: int, print_size_e6: int) -> float:
    if print_size_e6 <= 0:
        return 1.0 if our_size_e6 > 0 else 0.0
    return max(0.0, min(1.0, our_size_e6 / print_size_e6))


@dataclass
class Fill:
    decision_id: int
    rfq_id: str
    at_ms: int
    direction: str
    sport: str
    n_legs: int
    same_game: bool
    p_fair: float
    sigma: float
    margin: float                 # our actual markup (|price - p_fair| before tick rounding)
    price: float                  # our actual quote
    size_e6: int
    requester: str
    round_trip: bool
    printed: bool
    print_price: float | None = None
    print_size_e6: int | None = None
    executed_at_ms: int | None = None
    delta: float | None = None    # competitor gap in the win direction; None without a print
    win_actual: bool = False      # at our actual quote
    fill_frac: float = 0.0        # share of the print we would have filled, at our actual quote
    undercut: float | None = None  # delta - actual markup: how far inside the print our quote sat (>= 0 when we win)

    def segment(self, cuts: Sequence[float]) -> Segment:
        return Segment.of(self.sport, self.n_legs, self.direction, self.same_game, self.sigma, cuts)

    def wins_at(self, m: float) -> bool:
        return self.delta is not None and m <= self.delta + 1e-12

    def wins_on_grid(self, grid: Iterable[float]) -> list[bool]:
        return [self.wins_at(m) for m in grid]


def adverse_round_trips(fills: Iterable[Fill], prints: Iterable[Print], window_ms: int = ROUND_TRIP_MS) -> set[str]:
    """rfq_ids of fills that won at our quote where the same requester printed the opposite direction of
    the same combo (condition_id) within ``window_ms`` of the print we filled."""
    prints = list(prints)
    by_rfq = {p.rfq_id: p for p in prints}
    by_combo: dict[str, list[Print]] = {}
    for p in prints:
        if p.condition_id:
            by_combo.setdefault(p.condition_id, []).append(p)
    out: set[str] = set()
    for f in fills:
        if not f.win_actual:
            continue
        p = by_rfq.get(f.rfq_id)
        if p is None or not p.condition_id:
            continue
        for q in by_combo.get(p.condition_id, ()):
            if (q.rfq_id != p.rfq_id and q.requester == p.requester and q.direction != p.direction
                    and abs(q.executed_at_ms - p.executed_at_ms) <= window_ms):
                out.add(f.rfq_id)
                break
    return out


def actual_markup(d: DecisionRec) -> float:
    """The markup our logged price carries over p_fair (includes tick rounding away from fair)."""
    return (d.price - d.p_fair) if d.direction == "BUY" else (d.p_fair - d.price)


def counterfactual(decisions: Iterable[DecisionRec], prints: Iterable[Print]) -> list[Fill]:
    """One Fill per decision; printed ones carry delta / win flags. A decision whose print has a
    different direction (cannot happen on the feed) is treated as unprinted."""
    by_rfq: dict[str, Print] = {p.rfq_id: p for p in prints}
    out: list[Fill] = []
    for d in decisions:
        p = by_rfq.get(d.rfq_id)
        m_act = actual_markup(d)
        f = Fill(decision_id=d.id, rfq_id=d.rfq_id, at_ms=d.at_ms, direction=d.direction, sport=d.sport, n_legs=d.n_legs,
                 same_game=d.same_game, p_fair=d.p_fair, sigma=d.sigma, margin=m_act, price=d.price, size_e6=d.size_e6,
                 requester=d.requester or (p.requester if p else ""), round_trip=d.round_trip, printed=False)
        if p is not None and p.direction == d.direction and p.price > 0:
            f.printed = True
            f.print_price, f.print_size_e6, f.executed_at_ms = p.price, p.size_e6, p.executed_at_ms
            f.delta = competitor_gap(d.direction, d.p_fair, p.price)
            f.win_actual = would_win(d.direction, d.p_fair, m_act, p.price)
            f.fill_frac = fill_fraction(d.size_e6, p.size_e6) if f.win_actual else 0.0
            f.undercut = max(0.0, f.delta - m_act) if f.win_actual else f.delta - m_act
        out.append(f)
    return out
