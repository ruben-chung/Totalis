"""CLI: python -m rfqbot {listen|catalog|replay|stats|books|bookstats|kalshi-sync|coverage|joint|joint-validate|joint-calibrate|risk}"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .config import ConfigError, Settings, load_settings
from .store import Store

log = logging.getLogger("rfqbot")
SUMMARY_PERIOD_S = 60


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _build_quoting(settings: Settings, store: Store, args: argparse.Namespace):
    """The paper quote handler and its game cache for ``listen --quote``: (assembler, cache)."""
    from .quoting import Assembler, load_quote_config
    from .quoting.cache import GameCache
    from .quoting.decisions import DecisionLog
    qcfg = load_quote_config(args.env)
    cache = GameCache(qcfg)
    inventory = None
    decisions_path = args.decisions_db or qcfg.decisions_db
    if qcfg.risk:
        from .quoting.inventory import Inventory
        from .risk.caps import Caps
        inventory = Inventory(cache, qcfg, Caps.from_env(), store_path=store.path, horizon_ms=settings.horizon_ms)
        cache.on_refresh.append(inventory.refresh)
        if Path(decisions_path).exists():
            n = inventory.load(decisions_path)
            log.info("inventory: %d pending quotes rebuilt from %s", n, decisions_path)
    decisions = DecisionLog(decisions_path)
    assembler = Assembler(cache, qcfg, decisions=decisions, inventory=inventory, horizon_ms=settings.horizon_ms)
    cache.on_refresh.append(decisions.flush)                  # decision rows are buffered off the RFQ path
    if qcfg.edge:
        cache.on_refresh.append(assembler.reload_edge_table)   # a refit by `python -m rfqbot.edge monitor` lands without a restart
    log.info("quoting ON (paper) decisions=%s base_edge=%s max_size_usd=%s risk=%s caps=%s edge_table=%s",
             decisions.path, qcfg.base_edge, qcfg.max_size_usd, qcfg.risk,
             inventory.caps.as_dict() if inventory else None, assembler.edge_table_fitted_at if qcfg.edge else "off")
    return assembler, cache


async def _summary_loop(listener, store: Store, rawlog, period_s: float = SUMMARY_PERIOD_S) -> None:
    """Log the per-minute frame counts until cancelled."""
    prev: dict[str, int] = {}
    while True:
        await asyncio.sleep(period_s)
        cur = dict(listener.counts)
        delta = {k: cur[k] - prev.get(k, 0) for k in sorted(cur) if cur[k] - prev.get(k, 0)}
        prev = cur
        extra = f" raw_log={rawlog.lines_written} lines" if rawlog else ""
        log.info("last %ds: %s | total rfqs=%s%s", period_s, delta, store.scalar("SELECT COUNT(*) FROM rfqs"), extra)


def cmd_listen(args: argparse.Namespace) -> int:
    from .catalog import CatalogSync
    from .listener import Listener
    from .rawlog import RawLog
    from .ws_client import AuthError, FatalGatewayError, RfqSocket

    try:
        settings = load_settings(args.env, require_auth=args.require_auth)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    _setup_logging(args.log_level or settings.log_level)
    store = Store(args.db or settings.db_path)
    catalog = None if args.no_catalog else CatalogSync(store, settings.rfq_api_url, refresh_sec=settings.catalog_refresh_sec)
    rawlog = RawLog(settings.raw_log_dir) if settings.raw_log_dir else None
    sock = RfqSocket(settings.ws_url, settings.auth_message, require_auth=args.require_auth)
    assembler, cache = _build_quoting(settings, store, args) if args.quote else (None, None)
    listener = Listener(settings, store, catalog=catalog, rawlog=rawlog, socket=sock,
                        **({"on_rfq": assembler.handle} if assembler else {}))
    log.info("db=%s raw_log=%s raw_db_types=%s paper=True", store.path, settings.raw_log_dir or "off", settings.raw_db_types)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, listener.stop)
        tasks = [asyncio.create_task(_summary_loop(listener, store, rawlog))]
        if cache:
            tasks.append(asyncio.create_task(cache.run()))
        if args.duration:
            loop.call_later(args.duration, listener.stop)
        try:
            await listener.run()
            return 0
        except AuthError as e:
            log.error("%s", e)
            return 3
        except FatalGatewayError as e:
            log.error("%s", e)
            return 4
        finally:
            if cache:
                cache.stop()
            for t in tasks:
                t.cancel()
            if assembler:
                _print_quote_stats(assembler)
                assembler.decisions.close()
            store.close()

    return asyncio.run(main())


def _print_quote_stats(assembler) -> None:
    st = assembler.stats()
    print(f"\nquote handler: RFQs seen {st['seen']}  latency p50={st['latency_p50_ms'] and round(st['latency_p50_ms'], 2)} ms "
          f"p99={st['latency_p99_ms'] and round(st['latency_p99_ms'], 2)} ms max={st['latency_max_ms'] and round(st['latency_max_ms'], 1)} ms  "
          f"games cached {st['games']}  cache {st['cache']}")
    for k, v in st["counts"].items():
        print(f"  {k:<44} {v:>8}")
    inv = st.get("inventory")
    if inv:
        print(f"inventory: {inv['positions']} active ({inv['pending']} pending) notional ${inv['notional']:,.0f} ES1 ${inv['es1']:,.0f} "
              f"= {inv['es1_frac'] if inv['es1_frac'] is not None else float('nan'):.1%} of capital; stats {inv['stats']}; "
              f"refresh {inv['refresh_ms']} ms; assess p50={inv['assess_p50_ms'] and round(inv['assess_p50_ms'], 2)} ms "
              f"p99={inv['assess_p99_ms'] and round(inv['assess_p99_ms'], 2)} ms; e_i {inv['e_i']}")
    if assembler.samples:
        print("sample reasons:")
        for r in list(assembler.samples)[-10:]:
            print("  " + r[:400])


def cmd_catalog(args: argparse.Namespace) -> int:
    from .catalog import CatalogSync

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    store = Store(args.db or settings.db_path)
    n = asyncio.run(CatalogSync(store, settings.rfq_api_url).sync())
    print(f"synced {n} position rows into {store.path}")
    store.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .replay import replay

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    target = Path(args.to)
    if target.exists():
        if not args.overwrite:
            print(f"{target} exists; pass --overwrite to replace it", file=sys.stderr)
            return 2
        target.unlink()
    store = Store(target)
    counts = replay(Path(args.source), store, expiry_grace_ms=settings.expiry_grace_ms, raw_db_types=args.raw_db_types)
    print(f"replayed into {target}: {dict(sorted(counts.items()))}")
    _print_stats(store)
    store.close()
    return 0


def cmd_books(args: argparse.Namespace) -> int:
    from .books import main as books_main

    argv = ["--env", args.env]
    if args.db:
        argv += ["--db", args.db]
    if args.rfq_db:
        argv += ["--rfq-db", args.rfq_db]
    if args.assets:
        argv += ["--assets", args.assets]
    if args.assets_per_conn:
        argv += ["--assets-per-conn", str(args.assets_per_conn)]
    if args.no_games:
        argv.append("--no-games")
    if args.duration:
        argv += ["--duration", str(args.duration)]
    if args.log_level:
        argv += ["--log-level", args.log_level]
    return books_main(argv)


def cmd_bookstats(args: argparse.Namespace) -> int:
    from .books import BookStore

    settings = load_settings(args.env)
    path = Path(args.db or settings.book_db_path)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    st = BookStore(path)
    q = st.scalar
    print(f"legs mapped to tokens: {q('SELECT COUNT(*) FROM leg_tokens WHERE token_id IS NOT NULL')}  unresolved: {q('SELECT COUNT(*) FROM leg_tokens WHERE token_id IS NULL')}")
    print(f"assets with book rows: {q('SELECT COUNT(DISTINCT asset_id) FROM book_top')}  top rows: {q('SELECT COUNT(*) FROM book_top')}  "
          f"leg trades: {q('SELECT COUNT(*) FROM leg_trades')}  subscribed now: {q('SELECT COUNT(*) FROM subscriptions WHERE unsubscribed_ms IS NULL')}")
    first, last = q("SELECT MIN(at_ms) FROM book_top"), q("SELECT MAX(at_ms) FROM book_top")
    print(f"span: {((last or 0) - (first or 0)) / 1000:.0f}s  connections: {q('SELECT COUNT(*) FROM book_connections')}  "
          f"open: {q('SELECT COUNT(*) FROM book_connections WHERE disconnected_at_ms IS NULL')}")
    for r in st.conn.execute("SELECT source, COUNT(*) FROM book_top GROUP BY 1"):
        print(f"  {r[0]:<8} {r[1]:>10}")
    from .books import coverage_report
    for line in coverage_report(st):
        print(line)
    st.close()
    return 0


def cmd_kalshi_sync(args: argparse.Namespace) -> int:
    from .kalshi import KalshiClient, KalshiSync

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    store = Store(args.db or settings.db_path)
    client = KalshiClient(args.kalshi_url or settings.kalshi_api_url, min_gap_sec=args.min_gap)
    res = asyncio.run(KalshiSync(store, client, status=None if args.all_statuses else "open").sync(args.sport))
    total = sum(n for n, _ in res.values()); errs = sum(e for _, e in res.values())
    for series, (n, e) in sorted(res.items()):
        if n:
            print(f"  {series:<20} {n:>5} markets  {e:>3} parse errors")
    print(f"synced {total} kalshi markets ({errs} parse errors) into {store.path}")
    store.close()
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from .sports.coverage import report

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    path = Path(args.db or settings.db_path)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    store = Store(path)
    res = report(store, args.sport, gamma_sample=args.sample if args.gamma else 0, seed=args.seed)
    store.close()
    return 1 if res["failed"] or res.get("gamma_disagreements") else 0


def cmd_joint(args: argparse.Namespace) -> int:
    from .pricing.cli import run_joint

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    return run_joint(args, settings)


def cmd_joint_validate(args: argparse.Namespace) -> int:
    from .pricing.validate import Validator, format_report, worst_family_pairs

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    v = Validator(args.history_db, args.out_db, max_pairs_per_game=args.max_pairs, seed=args.seed)
    offsets = [int(x) for x in args.offsets.split(",")]
    for off in offsets:
        if not args.report_only:
            print(f"== offset {off} min")
            v.run(off, limit=args.games, log=print)
        res = v.report(off)
        print("\n" + format_report(res))
        worst = worst_family_pairs(res)
        if worst:
            print("\nworst family pairs (engine Brier minus independence Brier):")
            for f, m in worst:
                print(f"  {f:<40} n={m['n']} brier_e={m['brier_engine']:.4f} brier_i={m['brier_indep']:.4f} bias_e={m['bias_engine']:+.3f} bias_i={m['bias_indep']:+.3f}")
    return 0


def cmd_joint_calibrate(args: argparse.Namespace) -> int:
    from .pricing.calibrate import DEFAULT_PATH, fit, format_metrics

    cal, metrics = fit(args.scores_db, args.offset, folds=args.folds, seed=args.seed)
    print(format_metrics(metrics))
    out = Path(args.out or DEFAULT_PATH)
    if not args.dry_run:
        cal.save(out)
        print(f"saved {out}")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    from .risk.cli import run_risk

    settings = load_settings(args.env)
    _setup_logging(args.log_level or settings.log_level)
    return run_risk(args, settings)


def cmd_stats(args: argparse.Namespace) -> int:
    settings = load_settings(args.env)
    path = Path(args.db or settings.db_path)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    store = Store(path)
    _print_stats(store)
    store.close()
    return 0


def _print_stats(store: Store) -> None:
    def section(title: str, rows):
        print(f"\n{title}")
        if not rows:
            print("  (none)")
        for k, v in rows:
            print(f"  {str(k):<28} {v:>8}")

    section("raw messages in db by type", store.count_by("raw_messages", "msg_type"))
    section("RFQs by status", store.count_by("rfqs", "status"))
    section("RFQs by direction", store.count_by("rfqs", "direction"))
    section("RFQs by leg count", store.count_by("rfqs_v", "n_legs", limit=12))
    section("quotes by state", store.count_by("quotes", "state"))
    section("connections by auth_ok", store.count_by("connections", "auth_ok"))
    first = store.scalar("SELECT MIN(first_seen_ms) FROM rfqs"); last = store.scalar("SELECT MAX(first_seen_ms) FROM rfqs")
    span = f"{(last - first) / 1000:.0f}s" if first and last else "-"
    print(f"\nrfqs: {store.scalar('SELECT COUNT(*) FROM rfqs')} over {span}  "
          f"combos: {store.scalar('SELECT COUNT(*) FROM combos')}  requesters: {store.scalar('SELECT COUNT(*) FROM requesters')}")
    print(f"trades: {store.scalar('SELECT COUNT(*) FROM trades')}  ours: {store.scalar('SELECT COUNT(*) FROM trades WHERE is_ours=1')}  "
          f"settlements: {store.scalar('SELECT COUNT(*) FROM settlements')}  unmet-demand summaries: {store.scalar('SELECT COUNT(*) FROM unmet_demand')}")
    print(f"markets: {store.scalar('SELECT COUNT(*) FROM markets')}  "
          f"legs with known market: {store.scalar('SELECT COUNT(*) FROM combo_legs l WHERE EXISTS (SELECT 1 FROM markets m WHERE m.position_id=l.position_id)')}"
          f"/{store.scalar('SELECT COUNT(*) FROM combo_legs')}")


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", default=".env", help="path to .env (default: ./.env)")
    common.add_argument("--db", default=None, help="override RFQ_DB_PATH")
    common.add_argument("--log-level", default=None)

    p = argparse.ArgumentParser(prog="rfqbot", description="Polymarket Combos RFQ paper-trading bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("listen", help="connect to the RFQ gateway and record everything", parents=[common])
    s.add_argument("--no-catalog", action="store_true", help="skip combo-markets catalog sync")
    s.add_argument("--require-auth", action="store_true", help="exit if the gateway rejects our credentials")
    s.add_argument("--duration", type=float, default=None, help="stop after N seconds (default: run until Ctrl-C)")
    s.add_argument("--quote", action="store_true", help="run the paper quote handler (rfqbot.quoting); off by default")
    s.add_argument("--decisions-db", default=None, help="override QUOTE_DECISIONS_DB")
    s.set_defaults(fn=cmd_listen)

    s = sub.add_parser("catalog", help="one-shot combo-markets catalog sync", parents=[common])
    s.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("replay", help="re-normalize recorded raw messages into a new db", parents=[common])
    s.add_argument("--from", dest="source", required=True, help="raw-log dir, .jsonl(.gz) file, or rfq.db")
    s.add_argument("--to", required=True, help="target db path")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--raw-db-types", default="non_request", choices=["non_request", "all", "none"])
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("stats", help="print counts from the db", parents=[common])
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("books", help="record CLOB order books for legs seen in recent RFQ flow", parents=[common])
    s.add_argument("--rfq-db", default=None, help="listener db to read the leg universe from (default: RFQ_DB_PATH)")
    s.add_argument("--assets", default=None, help="comma-separated asset ids instead of the RFQ-derived universe")
    s.add_argument("--assets-per-conn", type=int, default=None)
    s.add_argument("--no-games", action="store_true", help="leg-level only (ignore BOOK_GAME_SPORTS)")
    s.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    s.set_defaults(fn=cmd_books)

    s = sub.add_parser("bookstats", help="print counts from the books db", parents=[common])
    s.set_defaults(fn=cmd_bookstats)

    from .sports import registry as sports_registry

    s = sub.add_parser("kalshi-sync", help="fetch Kalshi game-level markets into kalshi_markets", parents=[common])
    s.add_argument("--sport", default="nfl", choices=sports_registry.sports())
    s.add_argument("--kalshi-url", default=None, help="override KALSHI_API_URL")
    s.add_argument("--min-gap", type=float, default=0.2, help="minimum seconds between requests")
    s.add_argument("--all-statuses", action="store_true", help="fetch every status, not only open")
    s.set_defaults(fn=cmd_kalshi_sync)

    s = sub.add_parser("joint", help="build the joint score model of one game and print diagnostics", parents=[common])
    s.add_argument("--game", required=True, help="Polymarket game slug prefix, e.g. nfl-ind-min-2026-10-25")
    s.add_argument("--at-ms", type=int, default=None, help="point-in-time quotes (default: latest)")
    s.add_argument("--pairs", action="store_true", help="print pairwise joint vs independent for the main families")
    s.add_argument("--synthetic", action="store_true", help="use a synthetic game with known truth instead of recorded books")
    s.add_argument("--books-db", default=None)
    s.add_argument("--n", type=int, default=50_000, help="scenarios")
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(fn=cmd_joint)

    s = sub.add_parser("joint-validate", help="score the joint engine on resolved games from history.db", parents=[common])
    s.add_argument("--history-db", default="./data/history.db")
    s.add_argument("--out-db", default="./data/engine_validation.db")
    s.add_argument("--offsets", default="15", help="comma-separated snapshot offsets in minutes, e.g. 60,15,5")
    s.add_argument("--max-pairs", type=int, default=3000, help="stratified pair sample per game")
    s.add_argument("--games", type=int, default=None, help="limit to the N most recent games")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--report-only", action="store_true", help="only print the report from out-db")
    s.set_defaults(fn=cmd_joint_validate)

    s = sub.add_parser("joint-calibrate", help="fit the joint-engine calibration layer on validation scores", parents=[common])
    s.add_argument("--scores-db", default="./data/engine_validation.db")
    s.add_argument("--offset", type=int, default=15)
    s.add_argument("--folds", type=int, default=5)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", default=None, help="calibration JSON (default: rfqbot/pricing/calibration.json)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_joint_calibrate)

    s = sub.add_parser("risk", help="exposure deltas, per-game worst cases, portfolio ES and caps for the paper book", parents=[common])
    s.add_argument("--from", dest="from_db", default="./data/decisions.db", help="decisions log (quoted rows become positions)")
    s.add_argument("--assume-filled", action="store_true", help="count every quote as executed (else pending for 60 s after its decision)")
    s.add_argument("--rfq-db", default="./data/rfq.db", help="catalog for leg specs (read-only)")
    s.add_argument("--books-db", default=None, help="books for building game models on demand")
    s.add_argument("--at-ms", type=int, default=None, help="build models from the books at this time (default: latest)")
    s.add_argument("--since-ms", type=int, default=None)
    s.add_argument("--until-ms", type=int, default=None)
    s.add_argument("--game", default=None, help="only positions touching this game prefix")
    s.add_argument("--outcome", default=None, help="also report the delta to this canonical LegSpec key")
    s.add_argument("--n-draws", type=int, default=20_000, help="portfolio Monte Carlo draws")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--json", default=None, help="write the full report here")
    s.set_defaults(fn=cmd_risk)

    s = sub.add_parser("coverage", help="parse / match coverage for --sport", parents=[common])
    s.add_argument("--sport", default="nfl", choices=sports_registry.sports())
    s.add_argument("--gamma", action="store_true", help="validate a sample of markets against the Gamma API")
    s.add_argument("--sample", type=int, default=60, help="sample size for --gamma")
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=cmd_coverage)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
