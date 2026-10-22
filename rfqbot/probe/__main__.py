"""CLI: python -m rfqbot.probe {dry-run|run|report}

  dry-run   plan the probes the last N hours of prints would have triggered, build their bodies through
            the DryRunGateway (nothing is sent), print the plan statistics, disguise hit rate, one body
            and the report shape; writes rows with mode 'dry' to --db (default: a scratch copy, not data/probe.db)
  run       long-running loop (PROBE_MODE=live only): poll prints and decisions, send due probes
  report    tables from data/probe.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from ..edge.config import EdgeConfig
from ..edge.fairs import FairSource
from .analysis import report_lines
from .config import ProbeConfig, load_probe_config
from .disguise import candidate_pool, leg_meta, pick_extra_leg
from .gateway import DryRunGateway, HttpGateway
from .scheduler import execute, plan, triggers
from .store import ProbeStore, now_ms

log = logging.getLogger("rfqbot.probe")


def _fairs(cfg: ProbeConfig) -> FairSource:
    return FairSource(EdgeConfig(decisions_db=cfg.decisions_db, rfq_db=cfg.rfq_db, books_db=cfg.books_db, kalshi_db=cfg.kalshi_db))


def cmd_dry_run(cfg: ProbeConfig, args: argparse.Namespace) -> int:
    now = now_ms()
    since = now - int(args.since_hours * 3_600_000)
    store = ProbeStore(args.db)
    trigs = triggers(cfg, since, now)
    # the plan is evaluated as if every horizon were due now, with an unlimited lateness window
    dry_cfg = ProbeConfig(**{**cfg.__dict__, "mode": "dry", "max_late_sec": 1e12})
    plans, st = plan(trigs, store, dry_cfg, now)
    print(f"dry-run over the last {args.since_hours:g} h: {len(trigs)} triggers (prints on combos we quoted), plan {st.as_dict()}")
    pool = candidate_pool(cfg, now) if cfg.disguise else []
    pids = {pid for t in trigs for pid in t.position_ids}
    metas = leg_meta(pids, books_db=cfg.books_db, rfq_db=cfg.rfq_db) if pids else {}
    hits = 0
    for i, t in enumerate(trigs):
        combo = [metas[p] for p in t.position_ids if p in metas]
        if len(combo) == len(t.position_ids) and pick_extra_leg(combo, now, pool, salt=i) is not None:
            hits += 1
    print(f"disguise: pool of {len(pool)} near-certain legs with a tight live book and a future start; "
          f"a disjoint one exists for {hits}/{len(trigs)} triggers")
    gw = DryRunGateway(dry_cfg)
    rows = execute(plans, gw, store, dry_cfg, now=now, fairs=_fairs(cfg) if args.fairs else None, pool=pool, metas=metas, mode="dry")
    print(f"built {len(rows)} request bodies (sent 0)")
    for b in gw.bodies[:args.show]:
        print(json.dumps(b, indent=1))
    for line in report_lines(store, cfg):
        print(line)
    store.close()
    return 0


def cmd_run(cfg: ProbeConfig, args: argparse.Namespace) -> int:
    if cfg.mode != "live":
        print("PROBE_MODE is not 'live'; nothing to run. Use dry-run to see what would be sent.", file=sys.stderr)
        return 2
    missing = cfg.missing_credentials()
    if missing:
        print("cannot go live, missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    store = ProbeStore(cfg.db)
    gw = HttpGateway(cfg)
    fairs = _fairs(cfg)
    last = now_ms() - int(cfg.max_late_sec * 1000)
    log.info("probe loop live: gateway=%s url=%s budget %d/h, %d/combo/day, disguise=%s", cfg.gateway, cfg.gateway_url, cfg.max_per_hour,
             cfg.max_per_combo, cfg.disguise)
    while True:
        t0 = time.time()
        try:
            now = now_ms()
            trigs = triggers(cfg, last - int(cfg.max_late_sec * 1000), now)
            plans, st = plan(trigs, store, cfg, now)
            if plans:
                rows = execute(plans, gw, store, cfg, now=now, fairs=fairs)
                log.info("sent %d probes (%s): %s", len(rows), st.as_dict(),
                         ", ".join(f"{r['direction']}@{r['horizon_sec']}s->{r['status']} {r['backed_out_price']}" for r in rows))
            last = now
        except Exception as e:  # keep the loop alive
            log.exception("probe loop error: %s", e)
        time.sleep(max(0.5, cfg.poll_sec - (time.time() - t0)))


def cmd_report(cfg: ProbeConfig, args: argparse.Namespace) -> int:
    store = ProbeStore(args.db or cfg.db)
    since = now_ms() - int(args.since_hours * 3_600_000) if args.since_hours else None
    for line in report_lines(store, cfg, since):
        print(line)
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfqbot.probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--log-level", default="INFO")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dry-run"); p.add_argument("--since-hours", type=float, default=1.0); p.add_argument("--show", type=int, default=1)
    p.add_argument("--db", default="./data/probe_dryrun.db"); p.add_argument("--fairs", action="store_true", help="also compute the leg-book fair at send time")
    p = sub.add_parser("run")
    p = sub.add_parser("report"); p.add_argument("--since-hours", type=float, default=0); p.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = load_probe_config(args.env)
    return {"dry-run": cmd_dry_run, "run": cmd_run, "report": cmd_report}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
