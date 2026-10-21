"""Does the replay reproduce what the live bot did, and is the raw log complete?

``python -m rfqbot.backtest validate --run <id> [--decisions data/decisions.db]`` compares, for every rfq_id in both
the replayed run and the live decisions log, the decision kind, ``p_fair`` and the quoted price; and counts the
RFQ_REQUEST frames of the raw log against ``rfq.db.rfqs`` in the window (the listener writes both from the same
frames, so any difference is frames lost in one of them).

Expected differences, all by construction: the live fairs come from a cache refreshed on a worker thread every
20 s (cold games, model builds in flight), the replay refreshes on one-second ticks, so a fair can differ by the
book moves of up to one refresh period; the live handler saw the stores as of its wall clock, the replay as of
the frame time; and the live book had its own earlier quotes, the replay's has its own.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .asof import ro
from .feed import raw_files


def compare_to_live(bt: sqlite3.Connection, run_id: str, decisions_db: str | Path, *, since_ms: int | None = None,
                    until_ms: int | None = None) -> dict[str, Any]:
    live = ro(decisions_db)
    try:
        where, args = "WHERE 1=1", []
        if since_ms is not None:
            where += " AND at_ms >= ?"; args.append(since_ms)
        if until_ms is not None:
            where += " AND at_ms <= ?"; args.append(until_ms)
        lrows = {}
        for r in live.execute(f"SELECT rfq_id, at_ms, decision, p_fair, price_e6, size_e6, margin FROM decisions {where} ORDER BY id", args):
            lrows[r["rfq_id"]] = r                      # last decision per rfq wins
    finally:
        live.close()
    out: dict[str, Any] = {"live_rows": len(lrows)}
    rows = bt.execute("SELECT rfq_id, at_ms, decision, p_fair, price_e6, size_e6, margin FROM decisions WHERE run_id=?", (run_id,)).fetchall()
    both = [(r, lrows[r["rfq_id"]]) for r in rows if r["rfq_id"] in lrows]
    out.update(replay_rows=len(rows), in_both=len(both), only_replay=len(rows) - len(both))
    replay_ids = {r["rfq_id"] for r in rows}
    out["only_live"] = sum(1 for k, v in lrows.items() if k not in replay_ids and v["decision"] != "skipped:leg:sport_not_quoted"
                           and not (v["decision"] or "").startswith("skipped:leg:unknown"))
    kind = lambda d: "quoted" if d == "quoted" else ("skip" if d else "none")
    agree = [(a, b) for a, b in both if kind(a["decision"]) == kind(b["decision"])]
    out["decision_kind_agree"] = len(agree)
    out["decision_matrix"] = {}
    for a, b in both:
        k = f"live {b['decision']} | replay {a['decision']}"
        out["decision_matrix"][k] = out["decision_matrix"].get(k, 0) + 1
    out["decision_matrix"] = dict(sorted(out["decision_matrix"].items(), key=lambda kv: -kv[1])[:12])
    fair = [(a["p_fair"], b["p_fair"]) for a, b in both if a["p_fair"] is not None and b["p_fair"] is not None]
    diffs = sorted(abs(x - y) for x, y in fair)
    if diffs:
        out["p_fair"] = {"n": len(diffs), "median_abs_diff": diffs[len(diffs) // 2], "p90": diffs[int(0.9 * len(diffs))], "p99": diffs[min(len(diffs) - 1, int(0.99 * len(diffs)))],
                         "max": diffs[-1], "share_within_0.5pt": sum(d <= 0.005 for d in diffs) / len(diffs),
                         "share_within_1pt": sum(d <= 0.01 for d in diffs) / len(diffs), "mean_signed": sum(x - y for x, y in fair) / len(fair)}
    pr = sorted(abs(a["price_e6"] - b["price_e6"]) / 1e6 for a, b in both if a["price_e6"] and b["price_e6"])
    if pr:
        out["price"] = {"n": len(pr), "median_abs_diff": pr[len(pr) // 2], "p90": pr[int(0.9 * len(pr))], "max": pr[-1]}
    lag = sorted(abs(a["at_ms"] - b["at_ms"]) for a, b in both)
    if lag:
        out["time_offset_ms"] = {"median": lag[len(lag) // 2], "max": lag[-1]}
    return out


def raw_completeness(source: Path, rfq_db: str | Path, start_ms: int, end_ms: int) -> dict[str, Any]:
    from .feed import iter_requests
    c: dict[str, int] = {}
    n = sum(1 for _ in iter_requests(source, start_ms, end_ms, counts=c))
    conn = ro(rfq_db)
    try:
        db = conn.execute("SELECT COUNT(*) FROM rfqs WHERE first_seen_ms BETWEEN ? AND ? AND stub = 0", (start_ms, end_ms)).fetchone()[0]
    finally:
        conn.close()
    return {"raw_unique_requests": n, "raw_duplicates": c.get("duplicate", 0), "raw_unparseable": c.get("unparseable", 0),
            "rfq_db_rfqs": db, "difference": n - db, "files": [p.name for p in raw_files(Path(source), start_ms, end_ms)]}


def validate_cli(args: Any) -> None:
    from .store import BacktestStore
    st = BacktestStore(args.out)
    run = st.run(args.run)
    w = json.loads(run["window_json"])
    rep = {"live_vs_replay": compare_to_live(st.conn, args.run, args.decisions, since_ms=w["start_ms"], until_ms=w["end_ms"]),
           "raw_log": raw_completeness(Path(args.source), args.rfq_db, w["start_ms"], w["end_ms"])}
    print(json.dumps(rep, indent=1, default=str))
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(rep, indent=1, default=str))
    st.close()
