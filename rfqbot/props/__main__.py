"""python -m rfqbot.props {pull-box | pull-lines | fit | stats} (see package docstring)."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .store import DEFAULT_DB, PropsStore


def cmd_pull_box(args: argparse.Namespace) -> int:
    from .espn import SEASON_WINDOWS, EspnClient, pull_box_scores
    start, end = SEASON_WINDOWS[args.season]
    start, end = args.start or start, args.end or end
    types = tuple(int(x) for x in args.types.split(","))
    st = PropsStore(args.db)
    c = EspnClient(raw_dir=args.raw_dir, min_gap_s=args.gap)
    try:
        n = pull_box_scores(st, c, start=start, end=end, season_types=types, refetch_days=args.refetch_days)
    finally:
        c.close()
    print(f"season {args.season} types {types} {start}..{end}: {n}  requests={c.requests}")
    print(st.counts())
    st.close()
    return 0


def cmd_pull_lines(args: argparse.Namespace) -> int:
    st = PropsStore(args.db)
    if args.kalshi:
        from .kalshi_lines import KalshiPublic, pull_kalshi_lines
        c = KalshiPublic(min_gap_s=args.gap)
        try:
            n = pull_kalshi_lines(st, c, series=tuple(args.series.split(",")), candles=not args.no_candles,
                                  refetch=args.refetch, out=print)
        finally:
            c.close()
        print(f"kalshi: {n}  requests={c.requests}")
    if args.poly:
        from .poly_lines import pull_poly_lines
        print(f"polymarket: {pull_poly_lines(st, history_db=args.history_db, offset_min=args.offset_min, out=print)}")
    print(st.counts())
    st.close()
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    from .fit import run_fit
    st = PropsStore(args.db, readonly=True)
    try:
        run_fit(st, out_json=args.out, out_md=args.notes, season_types=tuple(int(x) for x in args.types.split(",")),
                min_minutes=args.min_minutes, out=print)
    finally:
        st.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    st = PropsStore(args.db, readonly=True)
    try:
        print(st.counts())
        for r in st.rows("SELECT season, season_type, status, COUNT(*), MIN(date_et), MAX(date_et) FROM games GROUP BY 1,2,3 ORDER BY 1,2,3"):
            print("games", tuple(r))
        for r in st.rows("SELECT source, stat, COUNT(*), SUM(p_over IS NOT NULL), SUM(result IN ('yes','no')), MIN(game_key), MAX(game_key) FROM prop_lines GROUP BY 1,2 ORDER BY 1,2"):
            print("lines", tuple(r))
        joined = st.rows("""SELECT l.source, COUNT(*), SUM(b.player_id IS NOT NULL) FROM prop_lines l
                            LEFT JOIN box_player b ON b.game_key=l.game_key AND b.player_key=l.player_key AND (l.team IS NULL OR b.team=l.team)
                            GROUP BY 1""")
        for r in joined:
            print("lines joined to box rows", tuple(r))
        for r in st.rows("SELECT kind, status, COUNT(*) FROM fetch_log GROUP BY 1,2"):
            print("fetch_log", tuple(r))
    finally:
        st.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rfqbot.props")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pull-box", help="ESPN box scores for one season into games / box_team / box_player")
    s.add_argument("--season", type=int, default=2026, help="ESPN season year (2026 = 2025-26)")
    s.add_argument("--types", default="2,3,5", help="season types: 1 preseason, 2 regular, 3 postseason, 5 play-in")
    s.add_argument("--start", default=None); s.add_argument("--end", default=None)
    s.add_argument("--raw-dir", default="./data/props/raw/espn")
    s.add_argument("--gap", type=float, default=0.25, help="minimum seconds between requests")
    s.add_argument("--refetch-days", action="store_true", help="refetch every scoreboard day (new games)")
    s.set_defaults(fn=cmd_pull_box)
    s = sub.add_parser("pull-lines", help="pre-game prop lines into prop_lines")
    s.add_argument("--kalshi", action="store_true"); s.add_argument("--poly", action="store_true")
    s.add_argument("--series", default="KXNBAPTS,KXNBAREB,KXNBAAST,KXNBA3PT")
    s.add_argument("--no-candles", action="store_true", help="skip the candlestick price fetch")
    s.add_argument("--refetch", action="store_true", help="refetch candlesticks already fetched")
    s.add_argument("--gap", type=float, default=1.1, help="minimum seconds between Kalshi requests (public rate limit)")
    s.add_argument("--history-db", default="./data/history.db")
    s.add_argument("--offset-min", type=int, default=15, help="hist_prices offset used as the closing line")
    s.set_defaults(fn=cmd_pull_lines)
    s = sub.add_parser("fit", help="share model, residuals, correlations -> docs/props-fit-notes.md + JSON")
    s.add_argument("--out", default="./data/props_fit.json")
    s.add_argument("--notes", default="./docs/props-fit-notes.md")
    s.add_argument("--types", default="2,3,5", help="season types to fit on")
    s.add_argument("--min-minutes", type=float, default=0.0)
    s.set_defaults(fn=cmd_fit)
    s = sub.add_parser("stats"); s.set_defaults(fn=cmd_stats)
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
