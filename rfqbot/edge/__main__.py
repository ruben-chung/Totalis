"""CLI: python -m rfqbot.edge {fit|report|wallets|monitor}

  fit      join decisions to prints, fit curves / curse / markouts / wallets, write margin_table.json
  report   print the tables from data/edge.db and the margin table (no recomputation)
  wallets  list wallet scores; --allow / --deny / --clear maintain the manual list
  monitor  run fit every EDGE_REFRESH_SEC seconds (the markout monitor's timer)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .config import EdgeConfig, load_edge_config
from .fairs import FairSource
from .markout import summarize
from .pipeline import fit
from .segments import GLOBAL
from .store import EdgeStore, now_ms
from .table import load_table
from .wallets import load_manual, save_manual

log = logging.getLogger("rfqbot.edge")


def _ts(ms: int | None) -> str:
    return "-" if not ms else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _pt(x: float | None, d: int = 2) -> str:
    """Probability points (x100)."""
    return "-" if x is None else f"{100 * x:+.{d}f}"


def _table(rows: list[list[Any]], header: list[str]) -> str:
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(header)]
    line = lambda r: "  ".join(str(c).ljust(w[i]) for i, c in enumerate(r))
    return "\n".join([line(header), line(["-" * x for x in w])] + [line(r) for r in rows])


# ----------------------------------------------------------------------------- report
def report_lines(store: EdgeStore, cfg: EdgeConfig, since_ms: int | None = None) -> list[str]:
    out: list[str] = []
    meta = store.meta("last_fit") or {}
    out.append(f"last fit {_ts(meta.get('at_ms'))} UTC: decisions {meta.get('decisions', 0):,} prints {meta.get('prints', 0):,} "
               f"printed decisions {meta.get('fills_printed', 0):,} nodes {meta.get('nodes', 0)} wallets {meta.get('wallets', 0)}")
    f = store.conn.execute("SELECT COUNT(*), SUM(printed), SUM(win_actual), MIN(at_ms), MAX(at_ms) FROM fills").fetchone()
    out.append(f"fills: {f[0]:,} quoted decisions {_ts(f[3])}..{_ts(f[4])} UTC, {f[1] or 0:,} with a print, {f[2] or 0:,} would have won at our quote")
    # cells
    rows = []
    for r in store.conn.execute("SELECT c.cell, c.n, c.node, c.level, v.n_printed, v.json FROM cells c LEFT JOIN curves v ON v.node = c.cell ORDER BY c.cell"):
        d = json.loads(r["json"]) if r["json"] else {}
        rows.append([r["cell"], r["n"], d.get("n_printed", 0), d.get("n_win_actual", 0), r["node"], r["level"]])
    out.append("\nfull cells (sport|n_legs|dir|structure|sigma): n, printed, wins at our quote, node used, pooling level")
    out.append(_table(rows, ["cell", "n", "printed", "wins", "node", "lvl"]))
    # nodes / fill curves
    table = load_table(cfg.table_path, max_age_s=0)
    nodes = (table.nodes if table else {})
    rows = []
    for r in store.conn.execute("SELECT node, level, n, n_printed, json FROM curves ORDER BY level, node"):
        d = json.loads(r["json"])
        if r["n"] < cfg.n_min and r["node"] != GLOBAL.key:
            continue
        g, fv = d["grid"], d["f"]
        at = lambda m: fv[min(range(len(g)), key=lambda i: abs(g[i] - m))]
        nd = nodes.get(r["node"], {})
        rows.append([r["node"], r["level"], r["n"], r["n_printed"], _pct(at(0.0)), _pct(at(0.005)), _pct(at(0.01)), _pct(at(0.02)), _pct(at(0.05)),
                     _pt(d.get("delta_mean")), _pt(d.get("sigma_mean")),
                     _pt(nd.get("m_star")) if nd else "-", _pct(nd.get("f_star")) if nd else "-", _pt(nd.get("curse_star")) if nd else "-",
                     _pt(nd.get("info_premium")) if nd else "-", f"{nd.get('curse_mult', 0):.2f}" if nd else "-", _pt(nd.get("base")) if nd else "-",
                     nd.get("source", "-")])
    out.append(f"\nfill curves F(m) and optimiser per node (n >= {cfg.n_min}, plus global); pt = probability points x100")
    out.append(_table(rows, ["node", "lvl", "n", "printed", "F(0)", "F(.5pt)", "F(1pt)", "F(2pt)", "F(5pt)", "delta", "sigma", "m*", "F(m*)", "curse*",
                             "info", "c_mult", "base", "src"]))
    # curse: model vs measured
    rows = []
    for r in store.conn.execute("SELECT node, horizon_min, ref, json FROM curse ORDER BY node, horizon_min, ref"):
        d = json.loads(r["json"])
        if d["n_with_later"] < 2 or (r["node"] not in nodes and r["node"] != GLOBAL.key):
            continue
        nd = nodes.get(r["node"], {})
        m_star = nd.get("m_star")
        g = d["grid"]
        i = min(range(len(g)), key=lambda k: abs(g[k] - (m_star if m_star is not None else 0.0)))
        bu = [f"{_pt(b['mean_move'])}({b['n']})" for b in d["by_undercut"] if b["n"]]
        rows.append([r["node"], r["horizon_min"], r["ref"], d["n_printed"], d["n_with_later"], _pt(d["sigma_mean"]), _pt(d["delta_mean"]),
                     _pt(m_star), _pt(d["model"][i]), f"{_pt(d['measured'][i])}({d['measured_n'][i]})", " ".join(bu) or "-"])
    out.append("\nwinner's curse at m* (maker loss in pt; measured = later fair - p_fair among would-have-won; by undercut bins at our actual quote)")
    out.append(_table(rows, ["node", "h", "ref", "printed", "later", "sigma", "delta", "m*", "model", "measured(n)", "by undercut .5/1/2/5pt+"]))
    # markouts
    mrows = store.markout_rows(since_ms)
    out.append(f"\nmarkouts (maker P&L in pt after the print; {len(mrows):,} rows)")
    rows = [[*s.key, s.n, _pt(s.mean), _pt(s.se), _pt(s.median)] for s in summarize(mrows, ("ref", "horizon_min", "direction"))]
    out.append(_table(rows, ["ref", "h", "dir", "n", "mean", "se", "median"]))
    rows = [[*s.key, s.n, _pt(s.mean), _pt(s.se)] for s in summarize(mrows, ("ref", "sport", "direction")) if True]
    out.append(_table(rows, ["ref", "sport", "dir", "n", "mean(all h)", "se"]))
    # wallets
    ws = store.wallet_scores()
    blocked = [w for w in ws.values() if w.blocked]
    worst = sorted((w for w in ws.values() if not w.blocked and w.n >= 3), key=lambda w: w.shrunk)[:8]
    out.append(f"\nwallets: {len(ws)} scored, {len(blocked)} blocked, {sum(1 for w in ws.values() if w.premium > 0)} with a premium")
    rows = [[w.public_id[:12], w.n, w.n_wins, _pt(w.mean), _pt(w.shrunk), _pt(w.upper), w.reason, _pt(w.premium), _ts(w.blocked_until_ms)]
            for w in sorted(blocked, key=lambda w: w.shrunk)] + [[w.public_id[:12], w.n, w.n_wins, _pt(w.mean), _pt(w.shrunk), _pt(w.upper), "", _pt(w.premium), "-"] for w in worst]
    out.append(_table(rows, ["wallet", "n", "wins", "mean", "shrunk", "upper90", "block", "premium", "until"]))
    return out


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
    while True:
        t0 = time.time()
        try:
            since = now_ms() - int(args.since_hours * 3_600_000) if args.since_hours else None
            rep = fit(cfg, store, since_ms=since, fairs=FairSource(cfg))
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
