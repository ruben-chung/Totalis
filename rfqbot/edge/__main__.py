"""CLI: python -m rfqbot.edge {fit|report|wallets|monitor}

  fit      join decisions to prints, fit curves / curse / markouts / wallets, write margin_table.json
  report   print the tables from data/edge.db and the margin table (no recomputation)
  wallets  list wallet scores; --allow / --deny / --clear maintain the manual list
  monitor  run fit every EDGE_REFRESH_SEC seconds (the markout monitor's timer)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import EdgeConfig, load_edge_config
from .fairs import FairSource
from .pipeline import fit
from .report import _pt, _table, _ts, report_lines
from .store import EdgeStore, now_ms
from .wallets import load_manual, save_manual

log = logging.getLogger("rfqbot.edge")


# ----------------------------------------------------------------------------- commands
def cmd_fit(cfg: EdgeConfig, args: argparse.Namespace) -> int:
    store = EdgeStore(cfg.edge_db)
    since = now_ms() - int(args.since_hours * 3_600_000) if args.since_hours else None
    rep = fit(cfg, store, since_ms=since, write=not args.no_write, fairs=FairSource(cfg))
    print(f"fit done: decisions {len(rep.decisions):,} prints {len(rep.prints):,} printed decisions {sum(f.printed for f in rep.fills):,} "
          f"wins {sum(f.win_actual for f in rep.fills):,} nodes {len(rep.nodes)} markouts {rep.markout_counts} wallet obs {rep.wallet_obs} "
          f"compute_game calls {rep.fair_calls}")
    if not args.no_write:
        print(f"wrote {cfg.table_path}")
    for line in report_lines(store, cfg, since):
        print(line)
    store.close()
    return 0


def cmd_report(cfg: EdgeConfig, args: argparse.Namespace) -> int:
    store = EdgeStore(cfg.edge_db)
    since = now_ms() - int(args.since_hours * 3_600_000) if args.since_hours else None
    for line in report_lines(store, cfg, since):
        print(line)
    store.close()
    return 0


def cmd_wallets(cfg: EdgeConfig, args: argparse.Namespace) -> int:
    manual = load_manual(cfg.wallet_list)
    changed = False
    for pid in args.allow or []:
        manual[pid] = "allow"; changed = True
    for pid in args.deny or []:
        manual[pid] = "deny"; changed = True
    for pid in args.clear or []:
        manual.pop(pid, None); changed = True
    if changed:
        save_manual(cfg.wallet_list, manual)
        print(f"manual list -> {cfg.wallet_list}: allow {sum(v == 'allow' for v in manual.values())} deny {sum(v == 'deny' for v in manual.values())} (applied at the next fit)")
    store = EdgeStore(cfg.edge_db)
    ws = sorted(store.wallet_scores().values(), key=lambda w: (not w.blocked, w.shrunk))
    if not args.all:
        ws = [w for w in ws if w.blocked or w.premium > 0 or w.n >= cfg.block_min_fills][:40]
    rows = [[w.public_id, w.n, w.n_wins, _pt(w.mean), _pt(w.sd), _pt(w.shrunk), _pt(w.upper), int(w.blocked), w.reason, _pt(w.premium), w.manual, _ts(w.blocked_until_ms)]
            for w in ws]
    print(_table(rows, ["wallet", "n", "wins", "mean", "sd", "shrunk", "upper", "blk", "reason", "premium", "manual", "until"]))
    store.close()
    return 0


def cmd_monitor(cfg: EdgeConfig, args: argparse.Namespace) -> int:
    store = EdgeStore(cfg.edge_db)
    every = args.every or cfg.refresh_sec
    fairs = FairSource(cfg)      # one source across cycles: (game, minute) fairs are point-in-time, so the cache stays valid
    while True:
        t0 = time.time()
        try:
            since = now_ms() - int(args.since_hours * 3_600_000) if args.since_hours else None
            rep = fit(cfg, store, since_ms=since, fairs=fairs)
            log.info("fit: decisions %d prints %d printed %d wins %d nodes %d markouts %s blocked %d (%.0fs)", len(rep.decisions), len(rep.prints),
                     sum(f.printed for f in rep.fills), sum(f.win_actual for f in rep.fills), len(rep.nodes), rep.markout_counts,
                     sum(w.blocked for w in rep.wallets.values()), time.time() - t0)
        except Exception as e:  # keep the timer alive
            log.exception("fit failed: %s", e)
        time.sleep(max(5.0, every - (time.time() - t0)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfqbot.edge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fit"); p.add_argument("--since-hours", type=float, default=0); p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("report"); p.add_argument("--since-hours", type=float, default=0)
    p = sub.add_parser("wallets"); p.add_argument("--allow", action="append"); p.add_argument("--deny", action="append")
    p.add_argument("--clear", action="append"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("monitor"); p.add_argument("--every", type=float, default=0); p.add_argument("--since-hours", type=float, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = load_edge_config(args.env)
    return {"fit": cmd_fit, "report": cmd_report, "wallets": cmd_wallets, "monitor": cmd_monitor}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
