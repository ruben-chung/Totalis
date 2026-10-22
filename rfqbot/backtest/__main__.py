"""python -m rfqbot.backtest {run | compare | settle | report | runs}   (see claude-briefs/backtest-notes.md)"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import BacktestConfig, WindowConfig
from .store import DEFAULT_DB, BacktestStore


def parse_time(s: str) -> int:
    """epoch ms, or ISO 'YYYY-MM-DDTHH:MM[:SS]' (UTC)."""
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def latest_raw_ms(source: Path) -> int:
    """Newest frame time in the newest raw file (the recorder is still writing it)."""
    import gzip
    files = sorted(p for p in Path(source).rglob("*.jsonl*") if p.is_file())
    last = 0
    for p in reversed(files[-2:]):
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        last = max(last, int(json.loads(line).get("t", 0)))
                    except Exception:
                        continue
        except EOFError:
            pass
        if last:
            break
    return last


def window_from(args: argparse.Namespace) -> WindowConfig:
    end = parse_time(args.end) if args.end else latest_raw_ms(Path(args.source)) - 5 * 60_000
    start = parse_time(args.start) if args.start else end - int(args.hours * 3_600_000)
    return WindowConfig(start_ms=start, end_ms=end, refresh_idle_ms=int(args.idle_s * 1000))


def add_window_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--source", default="./data/raw", help="raw feed log directory (or one .jsonl.gz)")
    ap.add_argument("--start", default=None, help="epoch ms or ISO UTC; default end - hours")
    ap.add_argument("--end", default=None, help="epoch ms or ISO UTC; default newest raw frame - 5 min")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--idle-s", type=float, default=300.0, help="refresh a game on cadence only while it saw an RFQ this recently")
    ap.add_argument("--rfq-db", default="./data/rfq.db"); ap.add_argument("--books-db", default="./data/books.db")
    ap.add_argument("--kalshi-db", default="./data/kalshi_books.db")
    ap.add_argument("--out", default=str(DEFAULT_DB), help="backtest.db (the only file written)")
    ap.add_argument("--max-requests", type=int, default=None, help="stop after this many evaluated RFQs (smoke tests)")


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Replayer
    quote = json.loads(args.quote) if args.quote else {}
    if args.base_edge is not None:
        quote["base_edge"] = args.base_edge
    if args.no_risk:
        quote["risk"] = False
    cal = args.calibration if args.calibration in ("fitted", "ones") else float(args.calibration)
    bc = BacktestConfig(name=args.name, quote=quote, pricing=args.pricing, calibration=cal, shock_scale=args.shock_scale,
                        table=args.table, table_policy=args.table_policy)
    w = window_from(args)
    print(f"window {datetime.fromtimestamp(w.start_ms / 1000, timezone.utc):%m-%d %H:%M} .. "
          f"{datetime.fromtimestamp(w.end_ms / 1000, timezone.utc):%m-%d %H:%M} UTC  run {args.name}")
    rp = Replayer(Path(args.source), w, [bc], Path(args.rfq_db), Path(args.books_db), Path(args.kalshi_db), Path(args.out),
                  group_id=args.group, max_requests=args.max_requests)
    for s in rp.run():
        print(json.dumps(s.as_dict(), default=str))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from .compare import run_compare
    run_compare(args, window_from(args))
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    from .settle import settle_runs
    settle_runs(args)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .report import report_cli
    report_cli(args)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .validate import validate_cli
    validate_cli(args)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    st = BacktestStore(args.out)
    for r in st.runs(args.group):
        s = json.loads(r["summary_json"] or "{}")
        print(f"{r['run_id']:<32} group={r['group_id'] or '-':<14} quoted={s.get('quoted')} filled={s.get('filled')} "
              f"elapsed={s.get('elapsed_s') and round(s['elapsed_s'])}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    import faulthandler
    import signal
    try:
        sys.stdout.reconfigure(line_buffering=True)                # progress must be visible in the log of a detached run
        faulthandler.register(signal.SIGUSR1, all_threads=True)    # kill -USR1 <pid> dumps where it is (stderr)
    except (AttributeError, ValueError, OSError):
        pass
    ap = argparse.ArgumentParser(prog="python -m rfqbot.backtest")
    ap.add_argument("--log-level", default="WARNING")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="replay one setting over a window")
    add_window_args(s)
    s.add_argument("--name", default="base"); s.add_argument("--group", default=None)
    s.add_argument("--pricing", choices=("joint", "independence"), default="joint")
    s.add_argument("--calibration", default="fitted", help="fitted | ones | <factor on the fitted lambdas>")
    s.add_argument("--shock-scale", type=float, default=1.0)
    s.add_argument("--table", default="none", help="none (stage 2 formula) | repo | <path to a margin table>")
    s.add_argument("--table-policy", choices=("after_fit", "always"), default="after_fit")
    s.add_argument("--base-edge", type=float, default=None); s.add_argument("--no-risk", action="store_true")
    s.add_argument("--quote", default=None, help="JSON of QuoteConfig overrides")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("compare", help="the design switches over one pass of the window")
    add_window_args(s)
    s.add_argument("--group", default=None, help="group id (default cmp-<end>)")
    s.add_argument("--only", default=None, help="comma list of setting names")
    s.add_argument("--report-only", action="store_true", help="skip the replay, analyse the group already in --out")
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("settle", help="settle filled positions from resolutions (re-runnable)")
    s.add_argument("--run", default=None); s.add_argument("--group", default=None)
    s.add_argument("--rfq-db", default="./data/rfq.db"); s.add_argument("--history-db", default="./data/history.db")
    s.add_argument("--out", default=str(DEFAULT_DB))
    s.set_defaults(fn=cmd_settle)
    s = sub.add_parser("report", help="print / write the report of a run")
    s.add_argument("--run", default=None); s.add_argument("--group", default=None)
    s.add_argument("--rfq-db", default="./data/rfq.db"); s.add_argument("--books-db", default="./data/books.db")
    s.add_argument("--kalshi-db", default="./data/kalshi_books.db"); s.add_argument("--out", default=str(DEFAULT_DB))
    s.add_argument("--json", default=None, help="write the report JSON here (default data/backtest_reports/<run>.json)")
    s.add_argument("--no-markouts", action="store_true")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("validate", help="replayed fairs vs the live decisions log; raw log vs rfq.db")
    s.add_argument("--run", required=True); s.add_argument("--decisions", default="./data/decisions.db")
    s.add_argument("--source", default="./data/raw"); s.add_argument("--rfq-db", default="./data/rfq.db"); s.add_argument("--out", default=str(DEFAULT_DB))
    s.add_argument("--json", default="data/backtest_reports/validate.json")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("runs", help="list runs"); s.add_argument("--group", default=None); s.add_argument("--out", default=str(DEFAULT_DB))
    s.set_defaults(fn=cmd_runs)
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
