"""`python -m rfqbot risk`: the book's exposure, per-game worst cases, portfolio distribution, caps."""
from __future__ import annotations

import json
import time
from typing import Any

from ..sports.legspec import parse_canonical_key
from .book import Book
from .caps import Caps, usage
from .exposure import Exposure, ModelPool


def run_risk(args: Any, settings: Any = None, out=print) -> int:
    t0 = time.perf_counter()
    book = Book.from_decisions(args.from_db, assume_filled=args.assume_filled, rfq_db=args.rfq_db,
                               since_ms=args.since_ms, until_ms=args.until_ms)
    t_book = time.perf_counter() - t0
    if args.game:
        for rid, p in list(book.positions.items()):
            if args.game not in p.games:
                del book.positions[rid]
    s = book.summary()
    out(f"book {args.from_db}: {s['active']} active positions ({'assume filled' if args.assume_filled else 'pending only'}), "
        f"{s['shares']:.0f} shares, collateral at risk ${s['notional']:,.0f}; by sport {s['by_sport']}; "
        f"long/short {s['by_sign']['long']}/{s['by_sign']['short']}; legs {s['by_n_legs']}  [{t_book:.2f}s]")
    for f in s["flags"][:5]:
        out(f"  flag: {f}")
    if not book.active():
        out("nothing to measure")
        return 0

    pool = ModelPool(books_db=args.books_db, rfq_db=args.rfq_db, at_ms=args.at_ms, seed=args.seed)
    t1 = time.perf_counter()
    ex = Exposure(book, pool, n_draws=args.n_draws, seed=args.seed)
    outcomes = []
    if args.outcome:
        outcomes.append((args.outcome, parse_canonical_key(args.outcome)))
    rep = ex.report(top=args.top, outcomes=outcomes)
    t_exp = time.perf_counter() - t1
    caps = Caps.from_env()
    use = usage(ex, book, caps)
    t_all = time.perf_counter() - t0

    out(f"\nmodels: {len(pool.models)} games built ({sum(pool.build_s.values()):.1f}s), {len(pool.errors)} without a model; "
        f"{len(rep.factors)} unmodeled factors; {rep.n_terms} distinct combos; flags {rep.flags}")
    for g, e in sorted(pool.errors.items())[:args.top]:
        out(f"  no model {g}: {e}")

    out(f"\ntop {args.top} deltas by |$| (E[P&L | O] - E[P&L | not O]):")
    for label, d, kind in rep.top_deltas(args.top):
        out(f"  {d:+10,.0f}  {label}  [{kind}]")
    if rep.extra:
        out("\nrequested outcome:")
        for d in rep.extra:
            out(f"  {d.delta:+10,.0f}  {d.game} {d.label} (p={d.p:.3f})")

    out(f"\nper-game P&L (conditional on the game's scenario), worst {args.top} by worst scenario:")
    for g in sorted(rep.games, key=lambda g: g.worst)[:args.top]:
        out(f"  {g.game:32s} n={g.n_positions:4d} mean {g.mean:+9,.0f} sd {g.sd:8,.0f} worst {g.worst:+10,.0f} q01 {g.q01:+10,.0f} q05 {g.q05:+10,.0f}")

    p = rep.portfolio
    out(f"\nportfolio ({p.n_draws} draws, {p.seconds:.2f}s): mean {p.mean:+,.0f} sd {p.sd:,.0f} worst {p.worst:+,.0f} "
        f"q01 {p.q01:+,.0f} q05 {p.q05:+,.0f}  ES1 {p.es1:,.0f} ES5 {p.es5:,.0f}  (capital ${caps.capital_usd:,.0f}: ES1 = {p.es1 / caps.capital_usd:.1%})")
    out("  top contributors to the 1% tail (mean contribution in the tail):")
    for k, v in p.top_contributors:
        out(f"    {v:+10,.0f}  {k}")

    out("\ncaps (usage ratio of the current book; >= 1 breached):")
    for cap, u in use.items():
        flag = "BREACHED" if u["ratio"] >= 1 else ("near" if u["ratio"] >= 0.8 else "")
        out(f"  {cap:8s} {u['ratio']:6.2f}  value {u['value']:+,.0f} / limit {u['limit']:,.0f}  {u['driver']} {flag}")
    out(f"\ntimings: book {t_book:.2f}s, models {sum(pool.build_s.values()):.2f}s, exposure {rep.timings.get('decompose', 0) + rep.timings.get('aggregate', 0):.2f}s "
        f"(decompose {rep.timings.get('decompose', 0):.2f}, aggregate {rep.timings.get('aggregate', 0):.2f}), report+mc {rep.timings.get('report', 0):.2f}s, total {t_all:.2f}s")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"book": s, "report": rep.as_dict(), "caps": caps.as_dict(), "usage": use}, fh, indent=1, default=str)
        out(f"wrote {args.json}")
    return 0
