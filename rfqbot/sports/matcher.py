"""Match a Polymarket LegSpec to Kalshi markets.

Confidence:
  * ``exact``        same canonical key (after resolving the game date) and no settlement
                     difference that changes the payoff of a completed game.
  * ``rules_differ`` same canonical key but a payoff-relevant rule differs (OT inclusion,
                     tie handling). The ticker is still returned because it is the natural
                     hedge, but it is not the same contract. Never reported as exact.
  * ``shifted_line`` same game / family / period / subject (team or bucket), Kalshi lists other lines;
                     ``below`` / ``above`` are the nearest Kalshi lines on each side and
                     ``kalshi_ticker`` is the nearest of the two.
  * ``none``         nothing on Kalshi for this game + family + period (+ team).

``notes`` lists every settlement difference found, prefixed by severity:
  ``payoff:``   changes the payoff of a normally completed game (blocks ``exact``)
  ``abnormal:`` only matters for postponement / cancellation (Poly 50-50 vs Kalshi
                "fair price" after 48 h; Kalshi spreads/totals state nothing at all)
  ``info:``     one venue is silent where the other is explicit (e.g. Kalshi full-game
                spreads do not mention OT; Poly says "overtime is included")

Game identity: Kalshi dates are ET. A Poly spec parsed from its slug carries the UTC date,
so the index is searched for the team pair on the slug date and the day before; exactly
one Kalshi game may exist in that window (``AmbiguousGame`` otherwise). The returned
``game_date`` is the resolved ET date and the Poly spec is re-keyed to it before comparing.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .legspec import LegSpec, complement_side, contract_key, et_date_candidates

CONFIDENCES = ("exact", "rules_differ", "shifted_line", "none")
_COMPARED_KEYS = ("ot", "tie", "cancel", "postpone", "group")


class AmbiguousGame(ValueError):
    pass


@dataclass(frozen=True)
class LineCandidate:
    kalshi_ticker: str
    kalshi_side: str
    line: float


@dataclass
class MatchResult:
    confidence: str
    kalshi_ticker: str | None = None
    kalshi_side: str | None = None            # "yes" | "no"
    notes: list[str] = field(default_factory=list)
    game_date: str | None = None              # resolved ET date
    kalshi_spec: LegSpec | None = None
    below: LineCandidate | None = None        # shifted_line only
    above: LineCandidate | None = None

    def __post_init__(self) -> None:
        assert self.confidence in CONFIDENCES, self.confidence


class KalshiIndex:
    """Lookup structure over Kalshi YES-side LegSpecs."""

    def __init__(self, specs: Iterable[LegSpec] = ()):
        self.by_contract: dict[str, list[LegSpec]] = defaultdict(list)
        self.games: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.ladders: dict[tuple[str, str, str, str | None, str | None], list[LegSpec]] = defaultdict(list)
        self.families: dict[tuple[str, str], int] = defaultdict(int)   # (family, period) -> market count
        self.n = 0
        for s in specs:
            self.add(s)

    def add(self, s: LegSpec) -> None:
        if s.venue != "kalshi" or s.game_date_basis != "et":
            raise ValueError("KalshiIndex takes Kalshi specs with ET dates")
        self.n += 1
        self.by_contract[contract_key(s)].append(s)
        self.games[(s.away, s.home)].add(s.game_date)
        self.families[(s.family, s.period)] += 1
        if s.line is not None:
            self.ladders[(s.game_key, s.period, s.family, s.team, s.bucket)].append(s)

    def resolve_game_date(self, spec: LegSpec) -> str | None:
        """ET date of the Kalshi game matching ``spec``'s team pair, or None if Kalshi has none."""
        dates = self.games.get((spec.away, spec.home))
        if not dates:
            return None
        hits = [d for d in et_date_candidates(spec) if d in dates]
        if len(hits) > 1:
            raise AmbiguousGame(f"{spec.away}@{spec.home} has Kalshi games on {sorted(hits)}")
        return hits[0] if hits else None


def compare_rules(poly: LegSpec, kalshi: LegSpec) -> list[str]:
    """Every settlement difference between two specs of the same contract, severity-prefixed."""
    p, k = poly.rules, kalshi.rules
    notes: list[str] = []

    po, ko = p.get("ot", "unstated"), k.get("ot", "unstated")
    if po != ko:
        if "unstated" in (po, ko):
            notes.append(f"info:ot: poly {po}, kalshi {ko}")
        else:
            notes.append(f"payoff:ot: poly {po}, kalshi {ko}")

    pt, kt = p.get("tie", "n/a"), k.get("tie", "n/a")
    if pt != kt:
        notes.append(f"payoff:tie: poly {pt}, kalshi {kt}")

    pc, kc = p.get("cancel", "unstated"), k.get("cancel", "unstated")
    if pc != kc:
        notes.append(f"abnormal:cancel: poly {pc}, kalshi {kc}")
    pp, kp = p.get("postpone", "unstated"), k.get("postpone", "unstated")
    if pp != kp:
        notes.append(f"abnormal:postpone: poly {pp}, kalshi {kp}")

    pg, kg = p.get("group"), k.get("group")
    if (pg or kg) and pg != kg:
        notes.append(f"info:group: poly {pg or 'binary'}, kalshi {kg or 'binary'}")
    # any other rule key a sport records (e.g. "dnp" for player props) is payoff-relevant when it differs
    for key in sorted((set(p) | set(k)) - set(_COMPARED_KEYS)):
        pv, kv = p.get(key, "unstated"), k.get(key, "unstated")
        if pv != kv:
            sev = "info" if "unstated" in (pv, kv) else "payoff"
            notes.append(f"{sev}:{key}: poly {pv}, kalshi {kv}")
    return notes


def _side_on(kalshi: LegSpec, poly_side: str) -> str | None:
    if poly_side == kalshi.side:
        return "yes"
    try:
        if poly_side == complement_side(kalshi):
            return "no"
    except ValueError:
        pass
    return None


def match(poly: LegSpec, index: KalshiIndex) -> MatchResult:
    if poly.venue != "polymarket":
        raise ValueError("match() takes a Polymarket spec")
    if (poly.family, poly.period) not in index.families:
        return MatchResult("none", notes=[f"none:no kalshi markets for {poly.family}/{poly.period}"])
    date = index.resolve_game_date(poly)
    if date is None:
        return MatchResult("none", notes=[f"none:no kalshi game for {poly.away}@{poly.home} near {poly.game_date}"])
    spec = poly.with_game_date(date)

    # 1. same contract
    best: tuple[str, LegSpec] | None = None
    for k in index.by_contract.get(contract_key(spec), ()):
        side = _side_on(k, spec.side)
        if side == "yes":
            best = (side, k)
            break
        if side == "no" and best is None:
            best = (side, k)
    if best is not None:
        side, k = best
        notes = compare_rules(spec, k)
        conf = "rules_differ" if any(n.startswith("payoff:") for n in notes) else "exact"
        return MatchResult(conf, k.venue_ids.get("ticker"), side, notes, date, k)

    # 2. same subject, other lines
    if spec.line is not None:
        ladder = index.ladders.get((spec.game_key, spec.period, spec.family, spec.team, spec.bucket), [])
        below = above = None
        for k in ladder:
            side = _side_on(k, spec.side)
            if side is None or k.line is None:
                continue
            c = LineCandidate(k.venue_ids["ticker"], side, k.line)
            if k.line < spec.line and (below is None or k.line > below.line):
                below = c
            elif k.line > spec.line and (above is None or k.line < above.line):
                above = c
        if below or above:
            cands = [c for c in (below, above) if c]
            nearest = min(cands, key=lambda c: abs(c.line - spec.line))
            kspec = next(k for k in ladder if k.venue_ids["ticker"] == nearest.kalshi_ticker)
            notes = [f"line:poly {spec.line:g}, kalshi {nearest.line:g}" + (f" (below {below.line:g}" if below else " (no line below") +
                     (f", above {above.line:g})" if above else ", no line above)")]
            notes += compare_rules(spec, kspec)
            return MatchResult("shifted_line", nearest.kalshi_ticker, nearest.kalshi_side, notes, date, kspec, below, above)

    others = [k for (g, per, fam, _t, _b), ks in index.ladders.items() if g == spec.game_key and per == spec.period and fam == spec.family for k in ks]
    if others:
        reason = f"none:kalshi lists {poly.family}/{poly.period} for this game only on {sorted({k.team or k.bucket or '-' for k in others})}"
    elif any(k.game_key == spec.game_key and k.family == spec.family and k.period == spec.period for ks in index.by_contract.values() for k in ks):
        reason = f"none:kalshi has {poly.family}/{poly.period} for this game but not this subject"
    else:
        reason = f"none:kalshi game exists but has no {poly.family}/{poly.period} markets for it yet"
    return MatchResult("none", notes=[reason], game_date=date)

