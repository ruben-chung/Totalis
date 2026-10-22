"""Marginal layer: fair probability + expected error for one Polymarket leg at one instant.

Inputs are up to two live quotes: the leg's own Polymarket CLOB top of book (`MarketQuote` from
rfqbot.ladders) and, when the matcher found one, the matched Kalshi market's top of book
(`MatchRow`, `KalshiQuote` from rfqbot.kalshi_books; duck-typed here, see sources.py).

Per-venue estimate
    p_v   = mid + (microprice - mid) * depth / (depth + MICRO_D0), microprice = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
            (the size-at-best shift toward the heavy side is shrunk when the book is thin; plain mid when either
            size is missing or both are zero)
    Kalshi side mapping: kalshi_side == "no" -> the leg is the NO contract, so bid = 1 - yes_ask,
            ask = 1 - yes_bid and the sizes / depths swap sides. p_no = 1 - p_yes.
Per-venue precision
    sigma_v     = half_spread_v * (1 + c_thin / (1 + depth_v / D0)) + c_stale * staleness_v
                  depth_v = min(bid_depth, ask_depth) (sizes at best when depth is missing)
                  staleness_v = seconds since the quote was last confirmed. Both stores are change-only
                  (a row is written when the top changes, not per poll), so a quiet book is not a stale
                  one: the quote counts as confirmed at the feed's last activity at or before the
                  evaluation instant while the instrument was subscribed (VenueQuote.confirmed_ms).
                  The raw age of the row is kept as a feature (VenueEstimate.age_s).
                  c_stale = C_STALE_PREGAME, or C_STALE_INPLAY once game_start_time <= now
    precision_v = 1 / sigma_v^2
    Kalshi precision is multiplied by KALSHI_PRIOR[sport]. A `rules_differ` match is a bias, not noise: its variance
    becomes sigma^2 + RULES_DIFFER_BIAS^2 (props with differing rules are not combined at all). `shifted_line` /
    `none` matches do not combine (Polymarket only). A locked or crossed book (ask <= bid) is unusable (flag crossed:<venue>);
    half spreads are floored at half a tick (HS_FLOOR) so a 1-tick book cannot reach zero sigma.
Combination
    p   = sum(precision_v * p_v) / sum(precision_v)      (w_v = normalized precision)
    err = 1 / sqrt(sum(precision_v)) assumes the venues' errors are independent. They are not (both follow the same game
          information), so it is floored at ERR_RHO * min(sigma_v) of the venues used, then at ERR_FLOOR
          (DEGENERATE_ERR_FLOOR when degenerate), capped at ERR_CAP
Flags are gates or notes, never extra error (features already flow through sigma), except the
disagreement rule: when |p_poly - p_kalshi| exceeds DISAGREE_K * sqrt(sigma_p^2 + sigma_k^2) the tier is `disagree`
(beyond GAP_SKIP `skip`); p stays the precision-weighted combination and err >= gap.
Fees: both venues' quotes are fee-exclusive and no fee adjustment is made here; quote assembly
owns fees.
Units: probabilities and err are fractions 0..1 (0.01 = one probability point).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import MarginalConfig

POLY = "poly"
KALSHI = "kalshi"
TIERS = ("ok", "wide", "thin", "stale", "disagree", "skip")
CONFIDENCES = ("none", "exact", "rules_differ", "shifted_line")
_SIGMA_EPS = 1e-4
PROP_SLUG = re.compile(r"-(?:points|rebounds|assists|threes|steals|blocks|pra|pts-reb-ast)-")
_GAP_EPS = 1e-9        # float noise guard: a gap equal to the combined half-spreads is not a disagreement


@dataclass(frozen=True)
class Marginal:
    position_id: str
    p: float | None            # fair probability; None when every source is unusable
    err: float | None          # expected absolute error (fraction of probability)
    p_poly: float | None
    p_kalshi: float | None
    w_poly: float              # normalized precision weights actually used
    w_kalshi: float
    kalshi_confidence: str     # none | exact | rules_differ | shifted_line
    flags: tuple[str, ...]
    tier: str                  # ok | wide | thin | stale | disagree | skip
    at_ms: int                 # newest quote time used
    explain: str
    venues: tuple["VenueEstimate", ...] = field(default=(), compare=False, repr=False)   # features for logging


@dataclass(frozen=True)
class VenueQuote:
    """A top of book in the leg's own direction (after the Kalshi side mapping)."""
    venue: str
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    bid_depth: float | None
    ask_depth: float | None
    at_ms: int                 # 0 = no quote row
    confirmed_ms: int | None = None   # feed last seen alive for this instrument (change-only stores); None = unknown


@dataclass(frozen=True)
class VenueEstimate:
    venue: str
    p: float | None
    half_spread: float | None  # None when one-sided
    depth: float
    staleness_s: float
    sigma: float | None        # None when there is nothing to estimate from
    precision: float           # after venue prior / confidence scaling; 0 when not usable
    weight: float              # normalized weight actually used in the combination
    usable: bool               # two-sided, fresh enough
    one_sided: bool
    flags: tuple[str, ...]
    at_ms: int
    reason: str = ""           # why it was not used, when it was not
    age_s: float = 0.0         # seconds since the row itself (quietness); staleness_s is since last confirmation


# ----------------------------------------------------------------- quote shaping
def poly_quote(mq: Any, confirmed_ms: int | None = None) -> VenueQuote:
    return VenueQuote(POLY, mq.best_bid, mq.best_ask, mq.bid_size, mq.ask_size, mq.bid_depth, mq.ask_depth,
                      int(mq.at_ms or 0), confirmed_ms)


def kalshi_quote(kq: Any, side: str, confirmed_ms: int | None = None) -> VenueQuote:
    """Map a Kalshi YES-side book onto the leg: side "no" flips prices and swaps bid/ask sizes."""
    side = (side or "yes").lower()
    if side not in ("yes", "no"):
        raise ValueError(f"kalshi_side must be yes|no, got {side!r}")
    yb, ya = kq.yes_bid, kq.yes_ask
    if side == "yes":
        return VenueQuote(KALSHI, yb, ya, kq.bid_size, kq.ask_size, kq.bid_depth, kq.ask_depth, int(kq.at_ms or 0), confirmed_ms)
    return VenueQuote(KALSHI,
                      None if ya is None else 1.0 - ya, None if yb is None else 1.0 - yb,
                      kq.ask_size, kq.bid_size, kq.ask_depth, kq.bid_depth, int(kq.at_ms or 0), confirmed_ms)


def microprice(bid: float, ask: float, bid_size: float | None, ask_size: float | None) -> float:
    if bid_size is None or ask_size is None or (bid_size + ask_size) <= 0:
        return (bid + ask) / 2
    return (bid * ask_size + ask * bid_size) / (bid_size + ask_size)


def parse_game_start_ms(game_start_time: str | None) -> int | None:
    """Gamma's '2026-10-23 00:15:00+00' (or ISO) -> epoch ms; None when absent / unparseable."""
    if not game_start_time:
        return None
    s = str(game_start_time).strip().replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ----------------------------------------------------------------- per-venue estimate
def estimate_venue(q: VenueQuote, *, now_ms: int, inplay: bool, cfg: MarginalConfig, scale: float = 1.0, bias: float = 0.0) -> VenueEstimate:
    """Features and precision for one venue. `scale` multiplies precision (the venue prior); `bias` is a payoff bias whose
    square is added to the variance (sigma stays the noise term, so disagreement tests use the unbiased sigma)."""
    v = q.venue
    flags: list[str] = []
    if q.at_ms <= 0 or (q.bid is None and q.ask is None):
        return VenueEstimate(v, None, None, 0.0, 0.0, None, 0.0, 0.0, False, False, (f"no_quote:{v}",), q.at_ms, "no quote")
    age = max(0.0, (now_ms - q.at_ms) / 1000.0)
    last_seen = max(q.at_ms, min(q.confirmed_ms, now_ms) if q.confirmed_ms else 0)
    staleness = max(0.0, (now_ms - last_seen) / 1000.0)
    warn = cfg.stale_warn_inplay_s if inplay else cfg.stale_warn_pregame_s
    drop = cfg.stale_drop_inplay_s if inplay else cfg.stale_drop_pregame_s
    c_stale = cfg.c_stale_inplay if inplay else cfg.c_stale_pregame
    stale = staleness > warn
    dropped = staleness > drop
    if stale:
        flags.append(f"stale:{v}")

    one_sided = q.bid is None or q.ask is None
    if one_sided:
        flags.append(f"one_sided:{v}")
        p = q.bid if q.ask is None else q.ask
        depth = (q.bid_depth if q.ask is None else q.ask_depth)
        if depth is None:
            depth = (q.bid_size if q.ask is None else q.ask_size) or 0.0
        sigma = cfg.one_sided_sigma + c_stale * staleness
        if depth < cfg.d_min:
            flags.append(f"thin:{v}")
        return VenueEstimate(v, p, None, float(depth), staleness, sigma, 0.0, 0.0, not dropped, True,
                             tuple(flags), q.at_ms, "stale_drop" if dropped else "one_sided", age)

    if q.ask <= q.bid:                       # locked or crossed: no spread to measure, the book is mid-update
        flags.append(f"crossed:{v}")
        return VenueEstimate(v, None, None, 0.0, staleness, None, 0.0, 0.0, False, False, tuple(flags), q.at_ms, "crossed", age)
    bd = q.bid_depth if q.bid_depth is not None else q.bid_size
    ad = q.ask_depth if q.ask_depth is not None else q.ask_size
    depth = min(bd if bd is not None else 0.0, ad if ad is not None else 0.0)
    mid = (q.bid + q.ask) / 2
    p = mid + (microprice(q.bid, q.ask, q.bid_size, q.ask_size) - mid) * depth / (depth + cfg.micro_d0)
    hs = max((q.ask - q.bid) / 2, cfg.hs_floor.get(v, cfg.hs_floor[POLY]))
    sigma = hs * (1 + cfg.c_thin / (1 + depth / cfg.d0)) + c_stale * staleness
    sigma = max(sigma, _SIGMA_EPS)
    precision = 0.0 if dropped else scale / (sigma * sigma + bias * bias)
    if 2 * hs > cfg.k_wide * p * (1 - p) + cfg.wide_floor:
        flags.append(f"wide:{v}")
    if depth < cfg.d_min:
        flags.append(f"thin:{v}")
    return VenueEstimate(v, p, hs, float(depth), staleness, sigma, precision, 0.0, not dropped, False,
                         tuple(flags), q.at_ms, "stale_drop" if dropped else "", age)


# ----------------------------------------------------------------- combination
def _with(est: VenueEstimate, **kw) -> VenueEstimate:
    from dataclasses import replace
    return replace(est, **kw)


def _fmt_depth(d: float) -> str:
    return f"{d / 1000:.1f}k" if d >= 1000 else f"{d:.0f}"


def _venue_text(e: VenueEstimate, scale: float) -> str:
    if e.p is None:
        return f"{e.venue} none"
    hs = "1-sided" if e.half_spread is None else f"hs {e.half_spread:.3f}"
    sc = "" if scale == 1.0 else f" x{scale:g}"
    return f"{e.venue} {e.p:.3f}{sc} ({hs}, depth {_fmt_depth(e.depth)}, {e.staleness_s:.0f}s)"


def combine(position_id: str, poly: VenueQuote | None, kalshi: VenueQuote | None, *, confidence: str,
            sport: str | None, now_ms: int, game_start_ms: int | None, cfg: MarginalConfig,
            kalshi_block: str | None = None) -> Marginal:
    """Apply the rules in the module docstring to one leg's two (optional) venue quotes."""
    if confidence not in CONFIDENCES:
        raise ValueError(f"unknown kalshi confidence {confidence!r}")
    inplay = game_start_ms is not None and game_start_ms <= now_ms
    flags: list[str] = ["inplay"] if inplay else []

    poly_q = poly if poly is not None else VenueQuote(POLY, None, None, None, None, None, None, 0)
    ep = estimate_venue(poly_q, now_ms=now_ms, inplay=inplay, cfg=cfg)
    flags.extend(ep.flags)

    k_scale = cfg.prior_for(sport)
    k_bias = 0.0
    if confidence == "rules_differ":
        k_bias = cfg.rules_differ_bias
        flags.append("rules_differ")
    ek: VenueEstimate | None = None
    if kalshi_block:
        flags.append(f"kalshi_unused:{kalshi_block}")
    elif confidence in ("exact", "rules_differ"):
        if kalshi is None:
            flags.append("kalshi_unused:no_quote")
        else:
            ek = estimate_venue(kalshi, now_ms=now_ms, inplay=inplay, cfg=cfg, scale=k_scale, bias=k_bias)
            flags.extend(ek.flags)
            if ek.p is None:
                ek = None
    else:
        flags.append(f"kalshi_unused:{confidence}")

    two_sided = [e for e in (ep, ek) if e is not None and e.usable and not e.one_sided]
    lone_one_sided = None
    if not two_sided:
        for e in (ep, ek):
            if e is not None and e.usable and e.one_sided:
                lone_one_sided = e
                break

    tier = "ok"
    p: float | None = None
    err: float | None = None
    used: list[VenueEstimate] = []
    disagree_gap: float | None = None

    if two_sided:
        used = two_sided
        if len(used) == 2:
            gap = abs(ep.p - ek.p)
            thr = cfg.disagree_k * math.hypot(ep.sigma, ek.sigma)
            if gap > thr + _GAP_EPS:
                disagree_gap = gap
                flags.append("disagree")
                if gap > cfg.gap_skip:
                    flags.append("disagree_hard")
                    tier = "skip"
                else:
                    tier = "disagree"
        total = sum(e.precision for e in used)
        p = sum(e.precision * e.p for e in used) / total
        err = max(1 / math.sqrt(total), cfg.err_rho * min(e.sigma for e in used))     # correlated venue errors: no better than rho * best sigma
        if disagree_gap is not None:
            err = max(err, disagree_gap)
    elif lone_one_sided is not None:
        used = [lone_one_sided]
        p = lone_one_sided.p
        err = lone_one_sided.sigma
        tier = "wide"
    else:
        flags.append("no_quote")
        tier = "skip"

    if p is not None:
        floor = cfg.err_floor
        if p > cfg.degenerate_hi or p < cfg.degenerate_lo:
            flags.append("degenerate")
            floor = cfg.degenerate_err_floor
        err = min(max(err, floor), cfg.err_cap)
        p = min(1.0, max(0.0, p))

    # weights actually used
    w: dict[str, float] = {POLY: 0.0, KALSHI: 0.0}
    if used:
        if lone_one_sided is not None:
            w[used[0].venue] = 1.0
        else:
            total = sum(e.precision for e in used)
            for e in used:
                w[e.venue] = e.precision / total
    ep = _with(ep, weight=w[POLY])
    if ek is not None:
        ek = _with(ek, weight=w[KALSHI])

    # tier (skip > disagree > stale > wide > thin > ok), set by the venues that carry real weight
    newest = max((e.at_ms for e in used), default=max((e.at_ms for e in (ep, ek) if e is not None), default=0))
    weighty = [e for e in used if w[e.venue] >= cfg.tier_w_min]
    used_names = {e.venue for e in weighty}
    newest_seen = max((max(e.at_ms, (poly_q if e.venue == POLY else kalshi).confirmed_ms or 0) for e in weighty), default=newest)
    if tier not in ("skip", "disagree") and weighty:
        if any(f"stale:{n}" in flags for n in used_names) or (inplay and (now_ms - min(newest_seen, now_ms)) / 1000.0 >= cfg.inplay_fresh_s):
            tier = "stale"
        elif lone_one_sided is not None or any(f"wide:{n}" in flags for n in used_names):
            tier = "wide"
        elif any(f"thin:{n}" in flags for n in used_names):
            tier = "thin"

    parts = [_venue_text(ep, 1.0)]
    if ek is not None:
        parts.append(_venue_text(ek, k_scale))
    joiner = " + " if ek is not None and ek.weight > 0 else " | "
    if p is None:
        tail = "-> no quote"
    else:
        tail = f"-> {p:.3f} ± {err:.3f} w={w[POLY]:.2f}/{w[KALSHI]:.2f}"
        if disagree_gap is not None:
            tail += f" gap {disagree_gap:.3f}>{cfg.disagree_k:g}x sigma: venues disagree"
    explain = f"{joiner.join(parts)} {tail} [{confidence}{', inplay' if inplay else ''}] {tier}"

    return Marginal(
        position_id=position_id, p=p, err=err,
        p_poly=ep.p, p_kalshi=None if ek is None else ek.p,
        w_poly=w[POLY], w_kalshi=w[KALSHI], kalshi_confidence=confidence,
        flags=tuple(dict.fromkeys(flags)), tier=tier, at_ms=newest, explain=explain,
        venues=tuple(e for e in (ep, ek) if e is not None),
    )


def marginal_for_leg(mq: Any, match: Any | None, kq: Any | None, *, sport: str | None, now_ms: int,
                     cfg: MarginalConfig, poly_confirmed_ms: int | None = None,
                     kalshi_confirmed_ms: int | None = None) -> Marginal:
    """Convenience over `combine` taking the producers' objects (MarketQuote, MatchRow, KalshiQuote).

    `*_confirmed_ms`: when the feed for that venue was last seen alive with the instrument subscribed
    (see sources.poly_confirmations / kalshi_confirmation); None means staleness is the row's age."""
    confidence = "none" if match is None else (getattr(match, "confidence", None) or "none")
    side = "yes" if match is None else (getattr(match, "kalshi_side", None) or "yes")
    kalshi = None if kq is None else kalshi_quote(kq, side, kalshi_confirmed_ms)
    # a prop whose rules differ (DNP / void conventions) is a payoff mismatch we cannot size: Polymarket only
    block = "props_rules" if confidence == "rules_differ" and PROP_SLUG.search(getattr(mq, "slug", None) or "") else None
    return combine(mq.position_id, poly_quote(mq, poly_confirmed_ms), kalshi, confidence=confidence, sport=sport, now_ms=now_ms,
                   game_start_ms=parse_game_start_ms(getattr(mq, "game_start_time", None)), cfg=cfg, kalshi_block=block)
