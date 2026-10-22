"""CLI: python -m rfqbot.history {pull|prices|resolve|pairs|reparse|stats}"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .store import HistoryStore


def _log(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_pull(a: argparse.Namespace) -> int:
    from .pull import pull
    st = HistoryStore(a.db)
    for sport in a.sport.split(","):
        c = asyncio.run(pull(st, sport.strip(), since=a.since, series_slug=a.series, include_side_events=not a.no_side_events,
                             max_events=a.max_events))
        print(f"pull {sport}: {c}")
    st.close()
    return 0


def cmd_prices(a: argparse.Namespace) -> int:
    from .prices import coverage_by_offset, snapshot_prices
    st = HistoryStore(a.db)
    offsets = [int(x) for x in a.minutes_before.split(",")]
    c = asyncio.run(snapshot_prices(st, sport=a.sport, offsets=offsets, include_props=a.include_props, limit=a.limit,
                                    fidelity_min=a.fidelity))
    print(f"prices: {c}")
    for off, have, checked in coverage_by_offset(st, sport=a.sport):
        print(f"  offset {off:>3} min: {have}/{checked} checked tokens have a snapshot")
    st.close()
    return 0


def cmd_resolve(a: argparse.Namespace) -> int:
    from .resolve import resolve
    st = HistoryStore(a.db)
    c = asyncio.run(resolve(st, a.rfq_db, limit=a.limit))
    print(f"resolve: {c}")
    st.close()
    return 0


def cmd_pairs(a: argparse.Namespace) -> int:
    from .pairs import build_pairs, pair_summary
    st = HistoryStore(a.db)
    c = build_pairs(st, sport=a.sport, max_pairs_per_game=a.max_pairs_per_game,
                    periods=tuple(a.periods.split(",")) if a.periods else None, rebuild=a.rebuild)
    print(f"pairs: {c}")
    for fa, pa, fb, pb, n, both in pair_summary(st, sport=a.sport)[:12]:
        print(f"  {fa:<14}{pa:<4}x {fb:<14}{pb:<4} pairs {n:>8}  both won {both:>7}")
    st.close()
    return 0


def cmd_reparse(a: argparse.Namespace) -> int:
    from .pull import reparse
    st = HistoryStore(a.db)
    print(f"reparse: {reparse(st, a.sport, only_failed=a.only_failed)}")
    st.close()
    return 0


def cmd_stats(a: argparse.Namespace) -> int:
    from .prices import coverage_by_offset
    st = HistoryStore(a.db)
    print("games / markets / resolved / parsed legs by sport:")
    for r in st.rows("SELECT g.sport, COUNT(DISTINCT g.game_prefix), SUM(g.n_markets), SUM(g.n_resolved),"
                     " (SELECT COUNT(*) FROM hist_legs l JOIN hist_markets m USING(condition_id) WHERE m.sport=g.sport AND l.parse_error IS NULL),"
                     " (SELECT COUNT(*) FROM hist_legs l JOIN hist_markets m USING(condition_id) WHERE m.sport=g.sport AND l.parse_error IS NOT NULL)"
                     " FROM hist_games g GROUP BY 1"):
        print(f"  {r[0]:<5} games {r[1]:>5}  markets {r[2]:>7}  resolved {r[3]:>7}  legs parsed {r[4]:>7}  unparsed {r[5]:>7}")
    for sport, in st.rows("SELECT DISTINCT sport FROM hist_games"):
        for off, have, checked in coverage_by_offset(st, sport=sport):
            print(f"  {sport} prices offset {off:>3} min: {have}/{checked} checked tokens")
    print(f"pairs: {st.scalar('SELECT COUNT(*) FROM hist_pairs')} over {st.scalar('SELECT COUNT(DISTINCT game_prefix) FROM hist_pairs')} games")
    print(f"resolution checks: {st.scalar('SELECT COUNT(*) FROM hist_resolve_checks')} (resolved {st.scalar('SELECT COUNT(*) FROM hist_resolve_checks WHERE resolved_outcome IS NOT NULL')})")
    st.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rfqbot.history")
    p.add_argument("--db", default="./data/history.db")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pull"); s.add_argument("--sport", default="nfl"); s.add_argument("--since", default=None)
    s.add_argument("--series", default=None, help="override the Gamma series slug"); s.add_argument("--no-side-events", action="store_true")
    s.add_argument("--max-events", type=int, default=None); s.set_defaults(fn=cmd_pull)
    s = sub.add_parser("prices"); s.add_argument("--sport", default=None); s.add_argument("--minutes-before", default="60,15,5")
    s.add_argument("--include-props", action="store_true"); s.add_argument("--limit", type=int, default=None)
    s.add_argument("--fidelity", type=int, default=1); s.set_defaults(fn=cmd_prices)
    s = sub.add_parser("resolve"); s.add_argument("--rfq-db", default="./data/rfq.db"); s.add_argument("--limit", type=int, default=None)
    s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("pairs"); s.add_argument("--sport", default=None); s.add_argument("--max-pairs-per-game", type=int, default=60_000)
    s.add_argument("--periods", default=None, help="e.g. FG,1H,2H"); s.add_argument("--rebuild", action="store_true"); s.set_defaults(fn=cmd_pairs)
    s = sub.add_parser("reparse", help="re-run the sport parser over stored markets (after a parser change)")
    s.add_argument("--sport", default=None); s.add_argument("--only-failed", action="store_true"); s.set_defaults(fn=cmd_reparse)
    s = sub.add_parser("stats"); s.set_defaults(fn=cmd_stats)
    a = p.parse_args(argv)
    _log(a.log_level)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
