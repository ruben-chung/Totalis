"""Readers: quoted decisions (data/decisions.db) and trade prints (data/rfq.db), with the fields the
edge layer segments on. Read-only; both files belong to other writers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ..db import chunks, ro_connect
from ..slugs import E6, game_prefix_of
from .config import EdgeConfig
from .segments import Segment, rfq_sigma


def sport_of_prefixes(prefixes: Iterable[str | None]) -> str:
    sports = {(p or "").split("-")[0] or "unknown" for p in prefixes}
    if not sports:
        return "unknown"
    return sports.pop() if len(sports) == 1 else "mixed"


def same_game_of(prefixes: Sequence[str | None]) -> bool:
    known = [p for p in prefixes if p]
    return len(known) != len(set(known))


@dataclass
class LegInfo:
    position_id: str
    prefix: str | None
    p: float | None
    err: float | None
    kalshi_confidence: str = "none"


@dataclass
class DecisionRec:
    id: int
    rfq_id: str
    at_ms: int
    direction: str
    requested_unit: str
    requested_e6: int
    n_legs: int
    p_fair: float
    u_m: float
    u_d: float
    margin: float
    price: float
    size_e6: int
    legs: list[LegInfo] = field(default_factory=list)
    same_game: bool = False
    round_trip: bool = False
    requester: str = ""

    @property
    def sigma(self) -> float:
        return rfq_sigma(self.u_m, self.u_d)

    @property
    def prefixes(self) -> list[str]:
        return [l.prefix for l in self.legs if l.prefix]

    @property
    def sport(self) -> str:
        return sport_of_prefixes(l.prefix for l in self.legs)

    def segment(self, cuts: Sequence[float]) -> Segment:
        return Segment.of(self.sport, self.n_legs, self.direction, self.same_game, self.sigma, cuts)


@dataclass
class Print:
    rfq_id: str
    direction: str
    price: float
    size_e6: int
    executed_at_ms: int
    requester: str
    n_legs: int
    position_ids: list[str]
    prefixes: list[str | None]
    requested_unit: str = ""
    requested_e6: int = 0
    condition_id: str = ""

    @property
    def sport(self) -> str:
        return sport_of_prefixes(self.prefixes)

    @property
    def same_game(self) -> bool:
        return same_game_of(self.prefixes)


def load_decisions(cfg: EdgeConfig, since_ms: int | None = None, until_ms: int | None = None, *,
                   quoted_only: bool = True) -> list[DecisionRec]:
    """Quoted decisions in [since_ms, until_ms], with the requester joined from rfq.db by rfq_id."""
    if not Path(cfg.decisions_db).exists():
        return []
    where, args = [], []
    if quoted_only:
        where.append("decision = 'quoted'")
    if since_ms is not None:
        where.append("at_ms >= ?"); args.append(since_ms)
    if until_ms is not None:
        where.append("at_ms <= ?"); args.append(until_ms)
    sql = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    out: list[DecisionRec] = []
    conn = ro_connect(cfg.decisions_db, shared=False)
    try:
        for r in conn.execute(sql, args):
            if r["p_fair"] is None or r["price_e6"] is None:
                continue
            legs = [LegInfo(l["position_id"], game_prefix_of(l.get("slug")), l.get("p"), l.get("err"),
                            l.get("kalshi_confidence") or "none") for l in json.loads(r["legs_json"] or "[]")]
            clusters = json.loads(r["clusters_json"] or "[]")
            same_game = any(len(c.get("position_ids") or []) > 1 for c in clusters) or same_game_of([l.prefix for l in legs])
            out.append(DecisionRec(
                id=int(r["id"]), rfq_id=r["rfq_id"], at_ms=int(r["at_ms"]), direction=r["direction"] or "",
                requested_unit=r["requested_unit"] or "", requested_e6=int(r["requested_e6"] or 0), n_legs=int(r["n_legs"]),
                p_fair=float(r["p_fair"]), u_m=float(r["u_m"] or 0.0), u_d=float(r["u_d"] or 0.0),
                margin=float(r["margin"] or 0.0), price=int(r["price_e6"]) / E6, size_e6=int(r["size_e6"] or 0),
                legs=legs, same_game=same_game, round_trip="[round_trip]" in (r["reason"] or ""),
            ))
    finally:
        conn.close()
    _attach_requesters(cfg, out)
    return out


def _attach_requesters(cfg: EdgeConfig, decs: list[DecisionRec]) -> None:
    if not decs or not Path(cfg.rfq_db).exists():
        return
    conn = ro_connect(cfg.rfq_db, shared=False)
    try:
        ids = [d.rfq_id for d in decs]
        found: dict[str, str] = {}
        for chunk in chunks(ids):
            for r in conn.execute(f"SELECT r.rfq_id, q.public_id FROM rfqs r JOIN requesters q ON q.id = r.requester_id"
                                  f" WHERE r.rfq_id IN ({','.join('?' * len(chunk))})", chunk):
                found[r["rfq_id"]] = r["public_id"] or ""
        for d in decs:
            d.requester = found.get(d.rfq_id, "")
    finally:
        conn.close()


def load_prints(cfg: EdgeConfig, since_ms: int | None = None, until_ms: int | None = None, *,
                rfq_ids: Iterable[str] | None = None, sports: Sequence[str] | None = None) -> list[Print]:
    """Trade prints with their combo legs' game prefixes. ``sports`` keeps prints whose legs are all in
    those sports (None = everything)."""
    if not Path(cfg.rfq_db).exists():
        return []
    where, args = [], []
    if since_ms is not None:
        where.append("t.executed_at_ms >= ?"); args.append(since_ms)
    if until_ms is not None:
        where.append("t.executed_at_ms <= ?"); args.append(until_ms)
    conn = ro_connect(cfg.rfq_db, shared=False)
    try:
        sql = ("SELECT t.rfq_id, t.direction, t.price_e6, t.size_e6, t.executed_at_ms, t.combo_id, q.public_id,"
               " r.size_unit, r.size_value_e6, c.condition_id, c.n_legs"
               " FROM trades t LEFT JOIN requesters q ON q.id = t.requester_id LEFT JOIN rfqs r ON r.rfq_id = t.rfq_id"
               " LEFT JOIN combos c ON c.id = t.combo_id")
        rows = conn.execute(sql + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY t.executed_at_ms", args).fetchall()
        wanted = set(rfq_ids) if rfq_ids is not None else None
        rows = [r for r in rows if wanted is None or r["rfq_id"] in wanted]
        combo_ids = sorted({r["combo_id"] for r in rows if r["combo_id"] is not None})
        legs: dict[int, list[tuple[int, str, str | None]]] = {}
        for chunk in chunks(combo_ids):
            for l in conn.execute(f"SELECT cl.combo_id, cl.leg_index, cl.position_id, m.slug FROM combo_legs cl"
                                  f" LEFT JOIN markets m ON m.position_id = cl.position_id"
                                  f" WHERE cl.combo_id IN ({','.join('?' * len(chunk))})", chunk):
                legs.setdefault(l["combo_id"], []).append((l["leg_index"], l["position_id"], l["slug"]))
        out: list[Print] = []
        for r in rows:
            ls = sorted(legs.get(r["combo_id"], []))
            pids = [p for _, p, _ in ls]
            prefixes = [game_prefix_of(s) for _, _, s in ls]
            p = Print(rfq_id=r["rfq_id"], direction=r["direction"] or "", price=int(r["price_e6"] or 0) / E6,
                      size_e6=int(r["size_e6"] or 0), executed_at_ms=int(r["executed_at_ms"] or 0), requester=r["public_id"] or "",
                      n_legs=int(r["n_legs"] or len(pids) or 0), position_ids=pids, prefixes=prefixes,
                      requested_unit=r["size_unit"] or "", requested_e6=int(r["size_value_e6"] or 0),
                      condition_id=r["condition_id"] or "")
            if sports is not None and (not pids or any((pr or "").split("-")[0] not in sports for pr in prefixes)):
                continue
            out.append(p)
        return out
    finally:
        conn.close()
