"""Settlement of filled positions from resolutions: re-runnable, realized and unrealized kept apart.

A leg's winning outcome comes from ``rfq.db.market_resolutions`` (the Data Engineer's resolve pass), then from
``history.db.hist_markets`` (``outcome_prices`` of resolved markets), both keyed by ``condition_id``. A combo is
YES iff every leg wins, so it is decided as soon as one leg resolves against it (payoff 0) or all resolve for
it (payoff 1); legs resolved 50-50 or void flag the combo ``void`` and exclude it from realized P&L (counted).
Undecided combos are marked to the last model fair the same run logged for the same combo (``p_fair`` of its
latest priced decision), and that mark's age is reported; unrealized P&L is never added to realized.

    python -m rfqbot.backtest settle --run <id>        # after `python -m rfqbot.history resolve --rfq-db data/rfq.db`
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .asof import ro
from .store import BacktestStore

E6 = 1_000_000
VOID_WORDS = {"50-50", "void", "n/a", "unknown", "cancelled", "canceled"}


@dataclass(frozen=True)
class Resolution:
    outcome: str | None       # winning outcome name; None when void
    void: bool
    source: str
    resolved_ms: int | None = None


def load_resolutions(rfq_db: str | Path, history_db: str | Path | None, cond_ids: Iterable[str]) -> dict[str, Resolution]:
    ids = sorted({c for c in cond_ids if c})
    out: dict[str, Resolution] = {}
    if not ids:
        return out
    if Path(rfq_db).exists():
        c = ro(rfq_db)
        try:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in c.execute("SELECT condition_id, resolved_outcome, resolved_at_ms FROM market_resolutions WHERE resolved_outcome IS NOT NULL"
                                   f" AND condition_id IN ({','.join('?' * len(chunk))})", chunk):
                    o = r["resolved_outcome"]
                    out[r["condition_id"]] = Resolution(None if o.strip().lower() in VOID_WORDS else o, o.strip().lower() in VOID_WORDS,
                                                        "market_resolutions", r["resolved_at_ms"])
        finally:
            c.close()
    rest = [i for i in ids if i not in out]
    if rest and history_db and Path(history_db).exists():
        c = ro(history_db)
        try:
            for i in range(0, len(rest), 500):
                chunk = rest[i:i + 500]
                for r in c.execute("SELECT condition_id, outcomes, outcome_prices FROM hist_markets WHERE resolved = 1"
                                   f" AND condition_id IN ({','.join('?' * len(chunk))})", chunk):
                    try:
                        outs, prices = json.loads(r["outcomes"]), [float(x) for x in json.loads(r["outcome_prices"])]
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    wins = [o for o, p in zip(outs, prices) if p >= 0.999]
                    out[r["condition_id"]] = Resolution(wins[0], False, "hist_markets") if len(wins) == 1 else Resolution(None, True, "hist_markets")
        finally:
            c.close()
    return out


def leg_status(outcome: str | None, res: Resolution | None) -> str | None:
    """won | lost | void | None (unresolved)."""
    if res is None:
        return None
    if res.void:
        return "void"
    return "won" if res.outcome == outcome else "lost"


def combo_status(statuses: list[str | None]) -> tuple[str, float | None]:
    """(status, payoff): void if any leg is void; lost as soon as one leg lost; won if all won; else unresolved."""
    if any(s == "void" for s in statuses):
        return "void", None
    if any(s == "lost" for s in statuses):
        return "lost", 0.0
    if statuses and all(s == "won" for s in statuses):
        return "won", 1.0
    return "unresolved", None


def sign_of(maker_action: str | None) -> int:
    return -1 if maker_action == "sell_yes" else 1


def fills_of(store: BacktestStore, run_id: str) -> list[sqlite3.Row]:
    return store.conn.execute("SELECT * FROM decisions WHERE run_id=? AND decision='quoted' AND fill_size_e6 > 0 ORDER BY at_ms", (run_id,)).fetchall()


def last_fairs(store: BacktestStore, run_id: str, cond_ids: Iterable[str]) -> dict[str, tuple[float, int]]:
    """condition_id -> (p_fair, at_ms) of the run's latest decision with a fair on that combo."""
    ids = sorted({c for c in cond_ids if c})
    out: dict[str, tuple[float, int]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in store.conn.execute(f"SELECT condition_id, p_fair, MAX(at_ms) AS at_ms FROM decisions WHERE run_id=? AND p_fair IS NOT NULL"
                                    f" AND condition_id IN ({','.join('?' * len(chunk))}) GROUP BY condition_id", (run_id, *chunk)):
            out[r["condition_id"]] = (float(r["p_fair"]), int(r["at_ms"]))
    return out


def settle_run(store: BacktestStore, run_id: str, rfq_db: str | Path, history_db: str | Path | None) -> dict[str, Any]:
    fills = fills_of(store, run_id)
    legs_by_fill = {r["rfq_id"]: json.loads(r["legs_json"]) for r in fills}
    res = load_resolutions(rfq_db, history_db, (l.get("condition_id") for ls in legs_by_fill.values() for l in ls))
    marks = last_fairs(store, run_id, (r["condition_id"] for r in fills))
    rows, summ = [], {"fills": len(fills), "won": 0, "lost": 0, "void": 0, "unresolved": 0, "realized": 0.0, "unrealized": 0.0,
                      "no_mark": 0, "mark_age_s_median": None}
    ages = []
    for r in fills:
        ls = legs_by_fill[r["rfq_id"]]
        sts = [leg_status(l.get("outcome"), res.get(l.get("condition_id"))) for l in ls]
        status, payoff = combo_status(sts)
        sign = sign_of(r["maker_action"])
        size, price = r["fill_size_e6"] / E6, r["price_e6"] / E6
        pnl = mark = unreal = None
        if payoff is not None:
            pnl = sign * size * (payoff - price)
            summ["realized"] += pnl
        elif status == "unresolved":
            m = marks.get(r["condition_id"])
            if m is None and r["p_fair"] is not None:
                m = (float(r["p_fair"]), int(r["at_ms"]))
            if m is None:
                summ["no_mark"] += 1
            else:
                mark = m[0]
                unreal = sign * size * (mark - price)
                summ["unrealized"] += unreal
                ages.append((store.conn.execute("SELECT MAX(at_ms) FROM decisions WHERE run_id=?", (run_id,)).fetchone()[0] - m[1]) / 1000)
        summ[status] += 1
        srcs = sorted({x.source for l in ls if (x := res.get(l.get("condition_id"))) is not None})
        resolved_ms = max([x.resolved_ms or 0 for l in ls if (x := res.get(l.get("condition_id"))) is not None], default=None)
        rows.append((run_id, r["rfq_id"], status, payoff, pnl, mark, unreal, sum(s is not None for s in sts), len(sts),
                     ",".join(srcs) or None, resolved_ms or None))
    store.conn.execute("DELETE FROM settlements WHERE run_id=?", (run_id,))
    store.conn.executemany("INSERT INTO settlements(run_id, rfq_id, status, payoff, pnl, mark, unrealized, legs_resolved, legs_total, source, settled_ms)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    if ages:
        ages.sort()
        summ["mark_age_s_median"] = ages[len(ages) // 2]
    summ["unresolved_share"] = (summ["unresolved"] / summ["fills"]) if summ["fills"] else None
    return summ


def settle_runs(args: Any) -> list[tuple[str, dict[str, Any]]]:
    store = BacktestStore(args.out)
    if args.group:
        run_ids = [r["run_id"] for r in store.runs(args.group)]
    elif args.run:
        run_ids = [args.run]
    else:
        run_ids = [r["run_id"] for r in store.runs()]
    out = []
    store.begin()
    for rid in run_ids:
        s = settle_run(store, rid, args.rfq_db, getattr(args, "history_db", None))
        out.append((rid, s))
        sh = s["unresolved_share"]
        print(f"{rid:<28} fills {s['fills']:>5}  won {s['won']:>4} lost {s['lost']:>4} void {s['void']:>3} unresolved {s['unresolved']:>5}"
              f" ({'-' if sh is None else f'{sh:.0%}'})  realized {s['realized']:+10.2f}  unrealized {s['unrealized']:+10.2f}")
    store.commit()
    store.close()
    return out
