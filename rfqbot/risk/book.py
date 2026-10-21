"""Positions and the book (stage 3, docs/risk-exposure-brief.md).

A position is one combo from the maker's point of view: its legs (``LegSpec`` where the sport is
registered), a sign (+1 long YES = we bought the combo, -1 short YES = we sold it), a size in
shares and the YES price per share. ``price`` is what we paid (long) or received (short), so the
P&L per share is ``sign * (I - price)`` with ``I`` the combo's payoff (1 iff every leg wins).

Pending quotes count at their quoted size until they expire; executed positions until every leg
is resolved (``Book.settle``). ``Book.from_decisions`` builds the paper book from the quote
handler's decisions log (``rfqbot.quoting.decisions``); ``Book.from_store`` from our quotes and
executions in ``rfq.db``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from ..sports import registry
from ..sports.legspec import LegSpec, LegSpecError, canonical_key, parse_canonical_key

PENDING, FILLED, SETTLED, EXPIRED = "pending", "filled", "settled", "expired"
SHARE_E6 = 1_000_000
DEFAULT_PENDING_WINDOW_MS = 60_000     # decisions log carries no deadline; treat a quote as live this long


@dataclass(frozen=True)
class PositionLeg:
    position_id: str
    key: str | None                 # canonical LegSpec key when the sport is registered
    spec: LegSpec | None
    p: float | None                 # marginal probability of the leg at quote time
    game: str | None                # slug prefix (ladder / books prefix, e.g. nfl-dal-phi-2026-10-27)
    slug: str | None = None
    outcome: str | None = None
    condition_id: str | None = None

    @property
    def sport(self) -> str | None:
        return self.game.split("-", 1)[0] if self.game else None


@dataclass
class Position:
    rfq_id: str
    legs: list[PositionLeg]
    sign: int                       # +1 long YES, -1 short YES
    size: float                     # shares
    price: float                    # YES price per share (pUSD)
    status: str = PENDING
    at_ms: int = 0
    expires_ms: int | None = None
    clusters: list[tuple[str | None, tuple[str, ...], float | None]] = field(default_factory=list)   # (game_key, pids, p) at quote time
    realized: float | None = None   # P&L once settled
    source: str = ""
    requester: str = ""             # who asked (for the repeated-request log)
    condition_id: str = ""          # the combo's condition id

    def __post_init__(self) -> None:
        if self.sign not in (1, -1):
            raise ValueError("sign must be +1 or -1")
        if self.size < 0 or not (0 <= self.price <= 1):
            raise ValueError("size must be >= 0 and price in [0, 1]")

    @property
    def games(self) -> list[str]:
        out: list[str] = []
        for l in self.legs:
            if l.game and l.game not in out:
                out.append(l.game)
        return out

    @property
    def sport(self) -> str | None:
        return next((l.sport for l in self.legs if l.sport), None)

    @property
    def game_dates(self) -> list[str]:
        out: list[str] = []
        for l in self.legs:
            d = l.spec.game_date if l.spec is not None else (l.game[-10:] if l.game else None)
            if d and d not in out:
                out.append(d)
        return out

    @property
    def max_loss_per_share(self) -> float:
        """Collateral at risk per share: the price for a long YES, 1 - price for a short YES."""
        return self.price if self.sign > 0 else 1.0 - self.price

    @property
    def notional(self) -> float:
        return self.size * self.max_loss_per_share

    @property
    def active(self) -> bool:
        return self.status in (PENDING, FILLED)

    def pnl(self, won: bool) -> float:
        return self.sign * self.size * ((1.0 if won else 0.0) - self.price)

    def as_dict(self) -> dict[str, Any]:
        return {"rfq_id": self.rfq_id, "sign": self.sign, "size": self.size, "price": self.price, "status": self.status,
                "at_ms": self.at_ms, "expires_ms": self.expires_ms, "realized": self.realized, "source": self.source,
                "requester": self.requester, "condition_id": self.condition_id,
                "legs": [{"position_id": l.position_id, "key": l.key, "p": l.p, "game": l.game, "slug": l.slug,
                          "outcome": l.outcome, "condition_id": l.condition_id} for l in self.legs]}


class Book:
    def __init__(self, positions: Iterable[Position] = ()):
        self.positions: dict[str, Position] = {}
        self.flags: list[str] = []
        for p in positions:
            self.add(p)

    # -- mutation ----------------------------------------------------------------------
    def add(self, pos: Position) -> None:
        self.positions[pos.rfq_id] = pos

    def expire(self, now_ms: int) -> list[str]:
        """Pending positions past their expiry are dropped from the active book."""
        out = []
        for p in self.positions.values():
            if p.status == PENDING and p.expires_ms is not None and p.expires_ms <= now_ms:
                p.status = EXPIRED
                out.append(p.rfq_id)
        return out

    def fill(self, rfq_id: str, size: float | None = None, price: float | None = None) -> Position:
        p = self.positions[rfq_id]
        p.status = FILLED
        if size is not None:
            p.size = size
        if price is not None:
            p.price = price
        return p

    def settle(self, resolutions: Mapping[str, str], now_ms: int | None = None) -> list[str]:
        """Settle filled positions whose every leg's market is in ``resolutions``
        (condition_id -> winning outcome name). A leg won iff its outcome is the resolved one."""
        out = []
        for p in self.positions.values():
            if p.status != FILLED:
                continue
            if any(l.condition_id is None or l.condition_id not in resolutions for l in p.legs):
                continue
            won = all(resolutions[l.condition_id] == l.outcome for l in p.legs)   # type: ignore[index]
            p.realized = p.pnl(won)
            p.status = SETTLED
            out.append(p.rfq_id)
        return out

    # -- views -------------------------------------------------------------------------
    def active(self) -> list[Position]:
        return [p for p in self.positions.values() if p.active]

    def __len__(self) -> int:
        return len(self.positions)

    def __iter__(self) -> Iterator[Position]:
        return iter(self.positions.values())

    def games(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.active():
            for g in p.games:
                seen.setdefault(g, None)
        return list(seen)

    def notional_by(self, what: str = "sport") -> dict[str, float]:
        """Collateral at risk by sport or by game date (a cross-date combo counts on every date it touches)."""
        out: dict[str, float] = {}
        for p in self.active():
            keys = [p.sport or "?"] if what == "sport" else (p.game_dates or ["?"])
            for k in keys:
                out[k] = out.get(k, 0.0) + p.notional
        return out

    def summary(self) -> dict[str, Any]:
        act = self.active()
        return {"positions": len(self.positions), "active": len(act),
                "by_status": dict(Counter(p.status for p in self.positions.values())),
                "by_sport": dict(Counter(p.sport or "?" for p in act)),
                "by_sign": {"long": sum(1 for p in act if p.sign > 0), "short": sum(1 for p in act if p.sign < 0)},
                "by_n_legs": dict(Counter(len(p.legs) for p in act)),
                "shares": sum(p.size for p in act), "notional": sum(p.notional for p in act),
                "notional_by_sport": self.notional_by("sport"), "flags": list(self.flags)}

    # -- loaders -----------------------------------------------------------------------
    @classmethod
    def from_decisions(cls, decisions_db: str | Path, assume_filled: bool = True, *, rfq_db: str | Path | None = None,
                       now_ms: int | None = None, since_ms: int | None = None, until_ms: int | None = None,
                       pending_window_ms: int = DEFAULT_PENDING_WINDOW_MS) -> "Book":
        """The paper book: every quoted decision (latest row per rfq_id) becomes a position.

        ``assume_filled``: count every quote as executed. Otherwise a quote is pending until the
        logged ``expires_ms`` (rows written by the caps-aware quoter), else for ``pending_window_ms``
        after its decision, and dropped once expired at ``now_ms``. ``rfq_db`` (read-only) supplies the catalog rows so legs get full LegSpecs
        (rules, condition ids, outcomes); without it specs come from the canonical keys alone.
        """
        now = int(time.time() * 1000) if now_ms is None else now_ms
        cat = _Catalog(rfq_db)
        book = cls()
        c = sqlite3.connect(f"file:{decisions_db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            where = ["decision = 'quoted'"]
            args: list[Any] = []
            if since_ms is not None:
                where.append("at_ms >= ?"); args.append(since_ms)
            if until_ms is not None:
                where.append("at_ms <= ?"); args.append(until_ms)
            rows = c.execute(f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY id", args).fetchall()
        finally:
            c.close()
        latest: dict[str, sqlite3.Row] = {r["rfq_id"]: r for r in rows}     # last decision per rfq wins
        for r in latest.values():
            pos = position_from_decision(r, cat)
            if pos is None:
                continue
            if assume_filled:
                pos.status = FILLED
            else:
                if pos.expires_ms is None:
                    pos.expires_ms = pos.at_ms + pending_window_ms
                if pos.expires_ms <= now:
                    continue
            book.add(pos)
        book.flags.extend(cat.flags)
        return book

    @classmethod
    def from_store(cls, store: Any, *, now_ms: int | None = None, p_of: Any = None, include_paper: bool = True) -> "Book":
        """Our live quotes (states SUBMITTED/ACKED/SELECTED/CONFIRMED, pending until the RFQ deadline)
        and executions / filled quotes (filled) from ``rfq.db``. Leg probabilities come from
        ``p_of(position_id)`` when given, else the catalog's ``outcome_price`` (flagged)."""
        from ..normalizer import Q_FILLED, Q_LIVE
        now = int(time.time() * 1000) if now_ms is None else now_ms
        conn = store.conn
        book = cls()
        q = conn.execute("SELECT q.*, r.direction, r.side, r.submission_deadline_ms, r.combo_id, r.status AS rfq_status"
                         " FROM quotes q JOIN rfqs r ON r.rfq_id = q.rfq_id WHERE q.state IN (%s)"
                         % ",".join("?" * (len(Q_LIVE) + 1)), (*sorted(Q_LIVE), Q_FILLED)).fetchall()
        for row in q:
            if not include_paper and row["is_paper"]:
                continue
            filled = row["state"] == Q_FILLED or store.has_execution(row["rfq_id"])
            deadline = row["submission_deadline_ms"]
            if not filled and deadline is not None and deadline <= now:
                continue
            pids = store.combo_legs(row["combo_id"]) if row["combo_id"] is not None else []
            legs = []
            for pid in pids:
                m = store.get_market(pid)
                spec, game, key = _spec_from_catalog(m)
                p = None
                if p_of is not None:
                    p = p_of(pid)
                if p is None and m is not None and m["outcome_price"] is not None:
                    p = float(m["outcome_price"])
                    book.flags.append(f"{row['rfq_id']}: p of {pid[:8]} from catalog price")
                legs.append(PositionLeg(pid, key, spec, p, game, m["slug"] if m else None, m["outcome"] if m else None,
                                        m["condition_id"] if m else None))
            sign = -1 if row["direction"] == "BUY" else 1      # requester buys -> maker sold YES
            book.add(Position(row["rfq_id"], legs, sign, row["size_e6"] / SHARE_E6, row["price_e6"] / SHARE_E6,
                              FILLED if filled else PENDING, int(row["created_at_ms"]), None if filled else deadline,
                              source="store"))
        return book


# ------------------------------------------------------------------------ helpers
class _Catalog:
    """Read-only lookups in rfq.db's markets table (None path -> nothing found)."""

    def __init__(self, rfq_db: str | Path | None):
        self.conn = None
        self.flags: list[str] = []
        if rfq_db is not None and Path(rfq_db).exists():
            self.conn = sqlite3.connect(f"file:{rfq_db}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
        elif rfq_db is not None:
            self.flags.append(f"catalog {rfq_db} not found; specs from canonical keys")
        self._cache: dict[str, sqlite3.Row | None] = {}

    def get(self, position_id: str) -> sqlite3.Row | None:
        if self.conn is None:
            return None
        if position_id not in self._cache:
            self._cache[position_id] = self.conn.execute("SELECT * FROM markets WHERE position_id=?", (position_id,)).fetchone()
        return self._cache[position_id]


def _spec_from_catalog(m: Any) -> tuple[LegSpec | None, str | None, str | None]:
    """(LegSpec, game prefix, canonical key) for a catalog row via the sports registry."""
    if m is None or not m["slug"]:
        return None, None, None
    from ..quoting.assemble import game_prefix_of
    game = game_prefix_of(m["slug"])
    sp = registry.sport_for_slug(m["slug"])
    if sp is None:
        return None, game, None
    try:
        spec = sp.parse_poly(m["slug"], m["title"], m["outcome"], m["outcome_index"], position_id=m["position_id"])
    except (ValueError, LegSpecError):
        return None, game, None
    return spec, game, canonical_key(spec)


def _spec_from_key(key: str | None) -> LegSpec | None:
    if not key:
        return None
    rules: dict[str, str] = {}
    sport = key.split("|", 1)[0]
    if sport == "nfl":
        from ..sports.nfl_poly import poly_rules
        parts = key.split("|")
        rules = poly_rules(parts[4], parts[3]) if len(parts) == 8 else {}
    try:
        return parse_canonical_key(key, rules=rules)
    except LegSpecError:
        return None


def position_from_decision(r: Mapping[str, Any], cat: _Catalog | None = None) -> Position | None:
    """A quoted decisions row -> Position (None when it was not a quote)."""
    if r["decision"] != "quoted" or r["price_e6"] is None or r["size_e6"] is None:
        return None
    legs = []
    for l in json.loads(r["legs_json"]):
        m = cat.get(l["position_id"]) if cat is not None else None
        spec, game, key = _spec_from_catalog(m)
        if spec is None:
            spec, key = _spec_from_key(l.get("key")), l.get("key")
        game = game or l.get("game_key")
        legs.append(PositionLeg(l["position_id"], key, spec, l.get("p"), game, l.get("slug"),
                                m["outcome"] if m is not None else l.get("outcome"), m["condition_id"] if m is not None else None))
    clusters = [(c.get("game_key"), tuple(c["position_ids"]), c.get("p")) for c in json.loads(r["clusters_json"])]
    sign = -1 if r["maker_action"] == "sell_yes" else 1
    keys = r.keys() if hasattr(r, "keys") else ()
    expires = r["expires_ms"] if "expires_ms" in keys else None
    return Position(r["rfq_id"], legs, sign, r["size_e6"] / SHARE_E6, r["price_e6"] / SHARE_E6, PENDING,
                    int(r["at_ms"]), int(expires) if expires is not None else None, clusters, source="decisions",
                    requester=(r["requester"] if "requester" in keys else None) or "",
                    condition_id=(r["condition_id"] if "condition_id" in keys else None) or "")
