"""``compare``: the design switches over one pass of the same window, one run per setting, paired against the base.

Settings (every run uses the stage 2 margin formula unless it is the table run, so the table does not confound the others):

  base           joint engine, fitted calibration, caps on, base edge 0.5 pt, k_m = k_d = 1
  indep          same-game clusters priced as the product of marginals
  cal-ones       joint engine with the identity calibration (raw engine)
  cal-0.75       fitted calibration with every lambda x 0.75
  caps-off       no caps, no Kelly sizing, no inventory skew (stage 2 behaviour); the book still tracked for ES1
  edge-0 / edge-1pt / edge-2pt   base edge 0 / 1 / 2 points (base is 0.5)
  table          the margin table (data/margin_table.json) for the whole window: IN-SAMPLE, it was fitted on this flow
  (shock-0.5 / shock-2 removed: inert with the bootstrap-only shock set, see SUITE)

For each comparison the paired difference in per-RFQ model P&L (realized where the combo is decided, else marked to
the run's last model fair; 0 for RFQs not filled) against the base is reported with its game-clustered SE and a
verdict: ``exceeds 2 SE``, ``within 2 SE`` or ``underpowered`` (fewer than 10 independent game clusters or no SE).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import BacktestConfig, WindowConfig
from .report import fill_table, pnl_section
from .settle import base_run_id, settle_run
from .stats import cluster_ids, cluster_mean_se, game_total_se, n_games, verdict
from .store import BacktestStore

SUITE: list[tuple[str, BacktestConfig, str]] = [
    ("base", BacktestConfig(name="base", table="none"), "base"),
    ("indep", BacktestConfig(name="indep", table="none", pricing="independence"), "pricing"),
    ("cal-ones", BacktestConfig(name="cal-ones", table="none", calibration="ones"), "calibration"),
    ("cal-0.75", BacktestConfig(name="cal-0.75", table="none", calibration=0.75), "calibration"),
    ("caps-off", BacktestConfig(name="caps-off", table="none", quote={"risk": False}), "caps"),
    ("edge-0", BacktestConfig(name="edge-0", table="none", quote={"base_edge": 0.0}), "base edge"),
    ("edge-1pt", BacktestConfig(name="edge-1pt", table="none", quote={"base_edge": 0.01}), "base edge"),
    ("edge-2pt", BacktestConfig(name="edge-2pt", table="none", quote={"base_edge": 0.02}), "base edge"),
    ("table", BacktestConfig(name="table", table="repo", table_policy="always", note="in-sample: table fitted on this flow"), "base edge"),
    # shock-0.5 / shock-2 were dropped: the joint engine's shock set is bootstrap replicas only (seeds are not scaled), so
    # the scale is inert; BacktestConfig.shock_scale remains for engines whose shock set carries correlation shifts
]


def per_rfq_pnl(store: BacktestStore, run_id: str) -> tuple[dict[str, float], dict[str, float]]:
    """rfq_id -> model P&L and gross markup of the fill (absent = no fill). Unresolved fills are marked to the base run's
    last fair (``model_pnl_ref``), so a paired delta between runs does not contain the runs' model disagreement."""
    ft = fill_table(store, run_id)
    return {f["rfq_id"]: f["model_pnl_ref"] for f in ft}, {f["rfq_id"]: f["markup"] for f in ft}


def compare_group(store: BacktestStore, group_id: str, base_name: str = "base") -> dict[str, Any]:
    runs = store.runs(group_id)
    by_name = {r["name"]: r["run_id"] for r in runs}
    if base_name not in by_name:
        raise KeyError(f"no {base_name!r} run in group {group_id}")
    universe = [(r["rfq_id"], json.loads(r["games_json"] or "[]")) for r in
                store.conn.execute("SELECT rfq_id, games_json FROM decisions WHERE run_id=? AND decision <> 'skipped:leg:cold_game'", (by_name[base_name],))]
    rfq_ids = [u[0] for u in universe]
    clusters = cluster_ids([u[1] for u in universe])
    pn = {n: per_rfq_pnl(store, rid) for n, rid in by_name.items()}
    out: dict[str, Any] = {"group": group_id, "base": base_name, "clusters_in_base": len(set(clusters)), "games": n_games(u[1] for u in universe),
                           "evaluated_rfqs": len(universe), "rows": []}
    for r in runs:
        name = r["name"]
        summ = json.loads(r["summary_json"] or "{}")
        ft = fill_table(store, r["run_id"])
        p = pnl_section(ft)
        es = store.conn.execute("SELECT MAX(es1) FROM es1_path WHERE run_id=?", (r["run_id"],)).fetchone()[0]
        row: dict[str, Any] = {
            "name": name, "run_id": r["run_id"], "switch": next((s for n, _, s in SUITE if n == name), ""),
            "quoted": summ.get("quoted"), "printed": summ.get("printed"), "win_fills": summ.get("win"), "strict_fills": summ.get("strict"),
            "filled": summ.get("filled"), "gross_markup": p["gross_markup"]["total"], "realized": p["realized"]["total"],
            "resolved_combos": p["resolved_combos"], "model_pnl": p["total_model_pnl"]["total"], "max_es1": es,
            "skips": {k: v for k, v in (summ.get("counts") or {}).items() if k != "quoted"}}
        if name != base_name:
            for key, idx in (("model_pnl", 0), ("gross_markup", 1)):
                a, b = pn[name][idx], pn[base_name][idx]
                diffs = [a.get(i, 0.0) - b.get(i, 0.0) for i in rfq_ids]
                mean, se, g = cluster_mean_se(diffs, clusters)
                tot, tse = mean * len(diffs), None if se is None else se * len(diffs)
                _, gse, gg, gu = game_total_se(diffs, [u[1] for u in universe])
                row[f"d_{key}"] = {"total": tot, "se": tse, "clusters": g, "verdict": verdict(tot, tse, g),
                                   "se_game": gse, "games": gg, "games_universe": gu, "verdict_game": verdict(tot, gse, gg)}
        out["rows"].append(row)
    return out


def fmt_compare(c: dict[str, Any]) -> str:
    L = [f"compare group {c['group']}: {c['evaluated_rfqs']} RFQs evaluated, {c['games']} games, {c['clusters_in_base']} independent game clusters (base: {c['base']})", ""]
    L.append(f"{'setting':<11}{'quoted':>7}{'win':>5}{'strict':>7}{'filled':>7}{'markup $':>10}{'realized $':>11}{'(n)':>5}{'model P&L $':>12}{'maxES1 $':>10}   Δ model P&L vs base (±se, clusters)  verdict")
    for r in c["rows"]:
        d = r.get("d_model_pnl")
        dtxt = "" if d is None else f"{d['total']:+9.2f} ± {('n/a' if d['se'] is None else format(d['se'], '.2f')):>6}  g={d['clusters']:<3} {d['verdict']} | per-game ± {('n/a' if d['se_game'] is None else format(d['se_game'], '.2f')):>6}  G={d['games']:<3} {d['verdict_game']}"
        L.append(f"{r['name']:<11}{r['quoted'] or 0:>7}{r['win_fills'] or 0:>5}{r['strict_fills'] or 0:>7}{r['filled'] or 0:>7}{r['gross_markup']:>10.2f}"
                 f"{r['realized']:>11.2f}{r['resolved_combos']:>5}{r['model_pnl']:>12.2f}{(r['max_es1'] or 0):>10.0f}   {dtxt}")
    return "\n".join(L)


def run_compare(args: Any, window: WindowConfig, say: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Replay (unless report_only), settle and compare a group. Progress goes to ``say``; the caller prints the table."""
    from .engine import Replayer
    names = set(args.only.split(",")) if args.only else None
    suite = [(n, bc) for n, bc, _ in SUITE if names is None or n in names or n == "base"]
    group = args.group or f"cmp-{window.end_ms}"
    if not args.report_only:
        say(f"compare: {len(suite)} settings over one pass: {', '.join(n for n, _ in suite)}")
        rp = Replayer(Path(args.source), window, [bc for _, bc in suite], Path(args.rfq_db), Path(args.books_db), Path(args.kalshi_db),
                      Path(args.out), group_id=group, run_prefix=f"{group}:")
        for s in rp.run():
            say(f"  {s.run_id}: quoted {s.quoted} printed {s.printed} win {s.win} strict {s.strict} filled {s.filled}  ({s.elapsed_s:.0f}s)")
    store = BacktestStore(args.out)
    store.begin()
    for r in store.runs(group):
        settle_run(store, r["run_id"], args.rfq_db, getattr(args, "history_db", None) or "./data/history.db", ref_run_id=base_run_id(store, r["run_id"]))
    store.commit()
    c = compare_group(store, group, base_name="base")
    store.close()
    rdir = Path(getattr(args, "reports_dir", None) or "data/backtest_reports")
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"compare-{group}.json").write_text(json.dumps(c, indent=1, sort_keys=True, default=str))
    return c
