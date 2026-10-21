"""Disguise leg: add one near-certain position from an independent game so the probe combo has a new
condition id, then back the combo price out: p_XYZ = quoted / p_L, relative error = relative error of p_L.

Candidates: catalog positions (rfq.db ``markets``, ``outcome_price`` >= min_p - 0.02 as a pre-filter)
with a live top of book in data/books.db at ``at_ms`` whose mid is >= min_p and whose spread is at
most one tick, a known game start in the future (not in-play) that is no earlier than the combo's own
latest start (so the extra leg cannot resolve first), and ``disjoint`` from every combo leg under
``rfqbot.clusters`` (different game, no shared team) with the whole set certified independent.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..clusters import classify_pair, cluster, leg_ref
from .config import ProbeConfig


@dataclass(frozen=True)
class LegMeta:
    position_id: str
    condition_id: str | None
    slug: str | None
    outcome: str | None
    outcome_index: int | None
    game_start_ms: int | None
    end_ms: int | None


@dataclass(frozen=True)
class ExtraLeg:
    position_id: str
    condition_id: str | None
    slug: str | None
    outcome: str | None
    p_L: float                 # mid
    bid: float
    ask: float
    game_start_ms: int | None
    book_at_ms: int

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def game_key(self) -> str | None:
        r = leg_ref(self.position_id, self.condition_id, self.slug)
        return r.game_key

    def as_dict(self) -> dict:
        return {"position_id": self.position_id, "condition_id": self.condition_id, "slug": self.slug, "outcome": self.outcome,
                "p_L": self.p_L, "bid": self.bid, "ask": self.ask, "spread": round(self.spread, 6), "game_key": self.game_key,
                "game_start_ms": self.game_start_ms, "book_at_ms": self.book_at_ms}


def parse_ts(s: str | None) -> int | None:
    """'2026-10-24 14:00:00+00' or '2026-10-24T14:00:00Z' -> ms."""
    if not s:
        return None
    t = s.strip().replace("Z", "+00:00").replace(" ", "T")
    if t.endswith("+00"):
        t += ":00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _ro(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def leg_meta(position_ids: Iterable[str], *, books_db: str | Path, rfq_db: str | Path) -> dict[str, LegMeta]:
    ids = list(dict.fromkeys(position_ids))
    out: dict[str, LegMeta] = {}
    if not ids:
        return out
    cat, books = _ro(rfq_db), _ro(books_db)
    try:
        rows = {r["position_id"]: r for r in cat.execute(
            f"SELECT position_id, condition_id, slug, outcome, outcome_index FROM markets WHERE position_id IN ({','.join('?' * len(ids))})", ids)}
        toks = {r["position_id"]: r for r in books.execute(
            f"SELECT position_id, condition_id, slug, outcome, outcome_index, game_start_time, end_date FROM leg_tokens WHERE position_id IN ({','.join('?' * len(ids))})", ids)}
        for pid in ids:
            m, t = rows.get(pid), toks.get(pid)
            if m is None and t is None:
                continue
            out[pid] = LegMeta(pid, (m or t)["condition_id"], (m or t)["slug"], (m or t)["outcome"], (m or t)["outcome_index"],
                               parse_ts(t["game_start_time"]) if t else None, parse_ts(t["end_date"]) if t else None)
    finally:
        cat.close(); books.close()
    return out


def candidate_pool(cfg: ProbeConfig, at_ms: int, *, limit: int = 5000) -> list[ExtraLeg]:
    """Near-certain positions with a tight live book at ``at_ms`` and a future game start."""
    cat, books = _ro(cfg.rfq_db), _ro(cfg.books_db)
    try:
        pre = cat.execute("SELECT position_id, condition_id, slug, outcome FROM markets WHERE outcome_price >= ? LIMIT ?",
                          (cfg.disguise_min_p - 0.02, limit)).fetchall()
        if not pre:
            return []
        pids = [r["position_id"] for r in pre]
        toks: dict[str, sqlite3.Row] = {}
        for i in range(0, len(pids), 500):
            chunk = pids[i:i + 500]
            for t in books.execute(f"SELECT position_id, token_id, game_start_time FROM leg_tokens WHERE token_id IS NOT NULL"
                                   f" AND position_id IN ({','.join('?' * len(chunk))})", chunk):
                toks[t["position_id"]] = t
        out: list[ExtraLeg] = []
        min_book_ms = at_ms - int(cfg.disguise_max_age_sec * 1000)
        for r in pre:
            t = toks.get(r["position_id"])
            if t is None:
                continue
            start = parse_ts(t["game_start_time"])
            if start is None or start <= at_ms:
                continue   # unknown start or in-play
            top = books.execute("SELECT best_bid, best_ask, at_ms FROM book_top WHERE asset_id = ? AND at_ms <= ? ORDER BY at_ms DESC LIMIT 1",
                                (t["token_id"], at_ms)).fetchone()
            if top is None or top["best_bid"] is None or top["best_ask"] is None or top["at_ms"] < min_book_ms:
                continue
            bid, ask = top["best_bid"] / 10_000, top["best_ask"] / 10_000
            mid = (bid + ask) / 2
            if mid < cfg.disguise_min_p or ask - bid > cfg.disguise_max_spread + 1e-9:
                continue
            out.append(ExtraLeg(r["position_id"], r["condition_id"], r["slug"], r["outcome"], mid, bid, ask, start, int(top["at_ms"])))
        return out
    finally:
        cat.close(); books.close()


def pick_extra_leg(combo: Sequence[LegMeta], at_ms: int, pool: Sequence[ExtraLeg], *, salt: int = 0) -> ExtraLeg | None:
    """The best disjoint candidate: highest p_L, then tightest spread; ``salt`` rotates among ties so
    consecutive probes do not always carry the same extra leg."""
    refs = [leg_ref(l.position_id, l.condition_id, l.slug, l.outcome_index) for l in combo]
    latest_start = max((l.game_start_ms for l in combo if l.game_start_ms is not None), default=None)
    ok: list[ExtraLeg] = []
    for c in pool:
        if c.game_start_ms is None or c.game_start_ms <= at_ms:
            continue
        if latest_start is not None and c.game_start_ms < latest_start:
            continue   # would resolve before the combo
        cr = leg_ref(c.position_id, c.condition_id, c.slug)
        if cr.game_key is None or any(classify_pair(cr, r) != "disjoint" for r in refs):
            continue
        if not cluster(refs + [cr]).certified_independent:
            continue
        ok.append(c)
    if not ok:
        return None
    ok.sort(key=lambda c: (-round(c.p_L, 4), round(c.spread, 4), c.position_id))
    best_p, best_s = round(ok[0].p_L, 4), round(ok[0].spread, 4)
    ties = [c for c in ok if round(c.p_L, 4) == best_p and round(c.spread, 4) == best_s]
    return ties[salt % len(ties)]


def back_out(quoted_price: float, p_L: float, spread: float) -> tuple[float, float]:
    """(p_XYZ, absolute error) from the disguised quote; the error is p_XYZ x (half spread / p_L)."""
    if p_L <= 0:
        raise ValueError("p_L must be positive")
    p = quoted_price / p_L
    return p, p * (spread / 2) / p_L


def disguised_price(p_combo: float, p_L: float) -> float:
    return p_combo * p_L
