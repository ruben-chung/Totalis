"""Text report of the last fit: cells, fill curves and optimiser per node, winner's curse, markouts,
wallets (``python -m rfqbot.edge report``; also printed after ``fit``)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import EdgeConfig
from .markout import summarize
from .segments import GLOBAL
from .store import EdgeStore
from .table import load_table


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
