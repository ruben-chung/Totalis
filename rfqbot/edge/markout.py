"""Markout monitor: for every print in the quoted sports, the move of the combo fair from the print
time to +1/5/15/30 min, in maker terms (negative = the maker who filled it lost), against three
references (model, Polymarket-only, Kalshi-only). Rows live in data/edge.db ``markouts`` and are
filled in as horizons mature, so the monitor can run on a timer.

markout_h = -(fair_h - fair_0) for a BUY print (maker sold YES), +(fair_h - fair_0) for a SELL.

Segment of a print: ``Segment.of(sport, n_legs, direction, same_game, rfq_sigma(sigma_m, 0), cuts)``, the
same function and cuts decisions use; a print has no quoting-time dependence term, so u_d = 0 is its
proxy (every disjoint combo decision has u_d = 0 as well, so those bucket identically).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .config import EdgeConfig
from .curse import adverse_move
from .data import Print, load_prints
from .fairs import FairSource
from .segments import Segment, rfq_sigma
from .store import EdgeStore, now_ms

REFS = ("model", "poly", "kalshi")
SETTLE_MS = 30_000      # a horizon is computed once it is this far in the past
RETRY_MS = 30 * 60_000  # a row with no markout is recomputed while its horizon matured less than this long ago


def refresh_markouts(cfg: EdgeConfig, store: EdgeStore, fairs: FairSource, *, since_ms: int | None = None,
                     until_ms: int | None = None, now: int | None = None, prints: Sequence[Print] | None = None,
                     refs: Sequence[str] = REFS) -> dict[str, int]:
    """Compute every (print, ref, horizon) not yet stored with a later fair. Returns counts."""
    now = now if now is not None else now_ms()
    prints = list(prints) if prints is not None else load_prints(cfg, since_ms, until_ms, sports=cfg.sports)
    done = store.markout_done(retry_ms=RETRY_MS, now=now)
    rows: list[dict[str, Any]] = []
    counts = {"prints": len(prints), "computed": 0, "missing_fair0": 0, "missing_fair_h": 0, "pending": 0}
    for p in prints:
        have = done.get(p.rfq_id, set())
        todo = [(ref, h) for h in cfg.horizons_min for ref in refs if (ref, h) not in have]
        if not todo:
            continue
        t0 = p.executed_at_ms
        f0 = fairs.combo(p.position_ids, p.prefixes, t0)
        sigma = None if f0.sigma_m is None else rfq_sigma(f0.sigma_m, 0.0)     # u_d proxy 0, see module docstring
        seg = Segment.of(p.sport, p.n_legs, p.direction, p.same_game, sigma, cfg.sigma_cuts)
        for ref, h in todo:
            th = t0 + h * 60_000
            if th + SETTLE_MS > now:
                counts["pending"] += 1
                continue
            fair0 = f0.ref(ref)
            if fair0 is None:
                counts["missing_fair0"] += 1
                rows.append(_row(p, ref, h, t0, None, None, None, seg, sigma, now))
                continue
            fh = fairs.combo(p.position_ids, p.prefixes, th).ref(ref)
            if fh is None:
                counts["missing_fair_h"] += 1
                rows.append(_row(p, ref, h, t0, fair0, None, None, seg, sigma, now))
                continue
            counts["computed"] += 1
            rows.append(_row(p, ref, h, t0, fair0, fh, adverse_move(p.direction, fair0, fh) * -1.0, seg, sigma, now))
    store.put_markouts(rows)
    return counts


def _row(p: Print, ref: str, h: int, t0: int, fair0: float | None, fair_h: float | None, markout: float | None,
         seg: Segment, sigma: float | None, now: int) -> dict[str, Any]:
    # adverse_move is the maker's loss; the markout convention is maker P&L, hence the sign flip above
    return {"rfq_id": p.rfq_id, "ref": ref, "horizon_min": h, "t0_ms": t0, "fair0": fair0, "fair_h": fair_h, "markout": markout,
            "direction": p.direction, "sport": p.sport, "n_legs": p.n_legs, "same_game": int(p.same_game), "sigma": sigma,
            "segment": seg.key, "requester": p.requester, "print_price": p.price, "computed_at_ms": now}


@dataclass
class MarkoutStat:
    key: tuple
    n: int
    mean: float
    se: float
    median: float

    def as_dict(self) -> dict:
        return {"key": list(self.key), "n": self.n, "mean": round(self.mean, 5), "se": round(self.se, 5), "median": round(self.median, 5)}


def _stat(key: tuple, xs: list[float]) -> MarkoutStat:
    n = len(xs)
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    s = sorted(xs)
    return MarkoutStat(key, n, mean, sd / math.sqrt(n) if n else 0.0, s[n // 2])


def summarize(rows: Iterable[Any], by: Sequence[str] = ("ref", "horizon_min", "direction")) -> list[MarkoutStat]:
    """Group stored markout rows (sqlite rows or dicts) with a later fair by the given columns."""
    groups: dict[tuple, list[float]] = {}
    for r in rows:
        if r["markout"] is None:
            continue
        groups.setdefault(tuple(r[c] for c in by), []).append(float(r["markout"]))
    return [_stat(k, v) for k, v in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0]))]


def node_means(rows: Iterable[Any], *, ref: str = "model", horizon_min: int = 30) -> dict[str, tuple[float, int]]:
    """Mean markout per pooling node (every full cell contributes to all its ancestors)."""
    acc: dict[str, list[float]] = {}
    for r in rows:
        if r["ref"] != ref or int(r["horizon_min"]) != horizon_min or r["markout"] is None or not r["segment"]:
            continue
        for node in Segment.parse(r["segment"]).ladder():
            acc.setdefault(node.key, []).append(float(r["markout"]))
    return {k: (sum(v) / len(v), len(v)) for k, v in acc.items()}


def node_mean_for(node_key: str, means: dict[str, tuple[float, int]], n_min: int) -> tuple[float, str, int]:
    """(mean 30-min markout, node, n) at the first node on the ladder with n_min observations; (0.0, empty, 0) if none."""
    for s in Segment.parse(node_key).ladder():
        hit = means.get(s.key)
        if hit is not None and hit[1] >= n_min:
            return hit[0], s.key, hit[1]
    return 0.0, "", 0


def info_premium_for(node_key: str, means: dict[str, tuple[float, int]], n_min: int) -> tuple[float, str, int]:
    """max(0, -mean 30-min markout) at the first node on the ladder with n_min observations."""
    mean, key, n = node_mean_for(node_key, means, n_min)
    return max(0.0, -mean), key, n
