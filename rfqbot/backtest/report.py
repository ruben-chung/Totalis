"""Per-run report: funnel, P&L (realized / marked, game-clustered), attribution by margin term, markouts, ES1 and caps, latency."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..slugs import E6, game_prefix_of
from .settle import sign_of
from .stats import cluster_ids, cluster_mean_se, cluster_total_se, game_total_se, n_games, verdict
from .store import BacktestStore

N_LEG_BUCKETS = (("1", lambda n: n == 1), ("2", lambda n: n == 2), ("3-4", lambda n: 3 <= n <= 4), ("5+", lambda n: n >= 5))
TERMS = ("base", "u_m", "u_d", "info", "wallet", "inventory", "floor", "rounding")

MARKOUT_FAIR_SOURCE = ("rfqbot.edge FairSource -> marginals.compute_game on the live book stores read up to the instant (exact, no bucketing); "
                       "the decision's p_fair comes from the replay's own as-of readers, so the two can differ slightly "
                       "(catalog and leg_matches are end-of-recording in both)")
ES1_HALF_CAP_FRAC = 0.075            # half of the 15% ES1 cap


def n_legs_bucket(n: int) -> str:
    return next(name for name, f in N_LEG_BUCKETS if f(n))


# ------------------------------------------------------------------ per-fill table
def margin_terms(r: sqlite3.Row | dict, qcfg: dict[str, Any]) -> dict[str, float]:
    """The logged margin split into its terms, per share, in probability points. Sums to |price - p_fair|.

    For a fitted node the curse is approximated as curse_mult * (u_m + u_d), which equals the live rule's
    ``MarginTerms.curse(sigma)`` at the node's own sigma and differs from it away from it; the difference
    lands in ``floor`` together with the clamp at the curse term. Without a node it is the stage 2 formula
    k_m u_m + k_d u_d + e_b (+ info + wallet). Then + e_i; the remainder to |price - fair| is tick rounding.
    """
    g = (lambda k: r[k] if r[k] is not None else 0.0)
    u_m, u_d, e_b, e_i, margin = g("u_m"), g("u_d"), g("e_b"), g("e_i"), g("margin")
    info, wallet = g("info_premium"), g("wallet_premium")
    if r["edge_source"] in ("model", "measured"):
        cm = g("curse_mult")
        parts = {"base": e_b, "u_m": cm * u_m, "u_d": cm * u_d, "info": info, "wallet": wallet}
    else:
        parts = {"base": e_b, "u_m": qcfg.get("k_m", 1.0) * u_m, "u_d": qcfg.get("k_d", 1.0) * u_d,
                 "info": info if r["edge_source"] else 0.0, "wallet": wallet if r["edge_source"] else 0.0}
    margin0 = sum(parts.values())
    parts["inventory"] = e_i
    parts["floor"] = margin - (margin0 + e_i)
    markup = abs((r["price_e6"] / E6) - r["p_fair"])
    parts["rounding"] = markup - margin
    return parts


def fill_table(store: BacktestStore, run_id: str) -> list[dict[str, Any]]:
    run = store.run(run_id)
    qcfg = json.loads(run["quote_json"]) if run else {}
    st = {r["rfq_id"]: r for r in store.conn.execute("SELECT * FROM settlements WHERE run_id=?", (run_id,))}
    out = []
    for r in store.conn.execute("SELECT * FROM decisions WHERE run_id=? AND decision='quoted' AND fill_size_e6 > 0 ORDER BY at_ms", (run_id,)):
        sign = sign_of(r["maker_action"])
        size, price, fair = r["fill_size_e6"] / E6, r["price_e6"] / E6, r["p_fair"]
        s = st.get(r["rfq_id"])
        status = s["status"] if s else "unsettled"
        payoff = s["payoff"] if s else None
        mark = s["mark"] if s else None
        value = payoff if payoff is not None else (mark if mark is not None else fair)       # what the position is worth
        terms = {k: v * size for k, v in margin_terms(r, qcfg).items()}
        out.append({
            "rfq_id": r["rfq_id"], "at_ms": r["at_ms"], "executed_at_ms": r["executed_at_ms"], "direction": r["direction"], "sport": r["sport"],
            "n_legs": r["n_legs"], "pair_class": r["pair_class"], "games": json.loads(r["games_json"] or "[]"), "sign": sign, "size": size,
            "price": price, "fair": fair, "markup": -sign * size * (price - fair), "status": status, "payoff": payoff,
            "realized": s["pnl"] if s else None, "mark": mark, "unrealized": s["unrealized"] if s else None,
            "unrealized_ref": s["unrealized_ref"] if s else None,
            "model_pnl": sign * size * (value - price),
            "model_pnl_ref": (s["pnl"] if s and s["pnl"] is not None else (s["unrealized_ref"] if s and s["unrealized_ref"] is not None else sign * size * (value - price))), "pricing_error": sign * size * (value - fair),
            "fill_strict": r["fill_strict"], "terms": terms, "legs_json": r["legs_json"], "condition_id": r["condition_id"],
        })
    return out


# ------------------------------------------------------------------ pieces
def funnel(store: BacktestStore, run_id: str) -> dict[str, Any]:
    run = store.run(run_id)
    summ = json.loads(run["summary_json"] or "{}") if run else {}
    pre = summ.get("prefilter", {})
    rows = store.conn.execute("SELECT direction, sport, n_legs, pair_class, decision, p_fair, printed, fill_win, fill_strict, fill_size_e6 FROM decisions WHERE run_id=?", (run_id,)).fetchall()
    out: dict[str, Any] = {"requests_seen": pre.get("requests"), "prefiltered_other_sport": pre.get("pre:other_sport", 0),
                           "prefiltered_unknown_market": pre.get("pre:unknown_market", 0), "evaluated": len(rows),
                           "priced": sum(1 for r in rows if r["p_fair"] is not None),
                           "quoted": sum(1 for r in rows if r["decision"] == "quoted"),
                           "printed": sum(1 for r in rows if r["printed"]), "win": sum(1 for r in rows if r["fill_win"]),
                           "strict": sum(1 for r in rows if r["fill_strict"]), "filled": sum(1 for r in rows if (r["fill_size_e6"] or 0) > 0)}
    out["skips"] = dict(Counter(r["decision"] for r in rows if r["decision"] != "quoted").most_common())
    for key, fn in (("direction", lambda r: r["direction"]), ("sport", lambda r: r["sport"]), ("n_legs", lambda r: n_legs_bucket(r["n_legs"])),
                    ("pair_class", lambda r: r["pair_class"] or "n/a")):
        acc: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            k = fn(r)
            c = acc[k]
            c["evaluated"] += 1
            c["priced"] += r["p_fair"] is not None
            c["quoted"] += r["decision"] == "quoted"
            c["printed"] += bool(r["printed"])
            c["win"] += bool(r["fill_win"])
            c["strict"] += bool(r["fill_strict"])
        out[f"by_{key}"] = {k: dict(v) for k, v in sorted(acc.items())}
    return out


def pnl_section(fills: list[dict[str, Any]]) -> dict[str, Any]:
    games = [f["games"] for f in fills]
    cl = cluster_ids(games)
    def block(sel: list[int], field: str) -> dict[str, Any]:
        vals = [fills[i][field] for i in sel]
        tot, se, g = cluster_total_se(vals, [cl[i] for i in sel]) if vals else (0.0, None, 0)
        _, gse, gg, gu = game_total_se(vals, [fills[i]["games"] for i in sel]) if vals else (0.0, None, 0, 0)
        return {"n": len(sel), "total": tot, "se": se, "clusters": g, "se_game": gse, "game_groups": gg, "game_universe": gu,
                "flag_game": verdict(tot, gse, gg) if sel else "none", "games": n_games(fills[i]["games"] for i in sel),
                "per_fill": (tot / len(sel)) if sel else None, "flag": verdict(tot, se, g) if sel else "none"}
    decisive = [i for i, f in enumerate(fills) if f["realized"] is not None]
    marked = [i for i, f in enumerate(fills) if f["realized"] is None and f["status"] not in ("void",)]
    void = [i for i, f in enumerate(fills) if f["status"] == "void"]
    allf = list(range(len(fills)))
    mk = block(allf, "markup")
    for i in allf:
        fills[i]["_marked"] = fills[i]["realized"] if fills[i]["realized"] is not None else fills[i]["unrealized"]
    [i for i in allf if fills[i]["_marked"] is not None]
    return {"fills": len(fills), "gross_markup": mk,
            "realized": block(decisive, "realized"), "mark_to_model_unresolved": block(
                [i for i in marked if fills[i]["unrealized"] is not None], "unrealized"),
            "resolved_combos": len(decisive), "void_combos": len(void),
            "unresolved_share": (1 - len(decisive) / len(fills)) if fills else None,
            "total_model_pnl": block(allf, "model_pnl"), "decisive_games": n_games(fills[i]["games"] for i in decisive)}


def attribution(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """payoff - price = (payoff - fair) + (fair - price): pricing error + margin, margin split by term. $ totals, resolved and marked."""
    def agg(sel: list[dict[str, Any]]) -> dict[str, Any]:
        if not sel:
            return {"n": 0}
        pe = sum(f["pricing_error"] for f in sel)
        mg = sum(f["markup"] for f in sel)
        terms = {t: sum(f["terms"][t] for f in sel) for t in TERMS}
        return {"n": len(sel), "pnl": pe + mg, "pricing_error": pe, "margin": mg, "margin_terms": terms,
                "term_check": mg - sum(terms.values())}
    return {"resolved": agg([f for f in fills if f["realized"] is not None]),
            "unresolved_marked_to_model": agg([f for f in fills if f["realized"] is None and f["status"] != "void"]),
            "all": agg([f for f in fills if f["status"] != "void"])}


def es1_section(store: BacktestStore, run_id: str) -> dict[str, Any]:
    rows = store.conn.execute("SELECT at_ms, es1, es1_frac, positions, pending, notional FROM es1_path WHERE run_id=? ORDER BY at_ms", (run_id,)).fetchall()
    out: dict[str, Any] = {"samples": len(rows)}
    if rows:
        m = max(rows, key=lambda r: r["es1"])
        out.update(max_es1=m["es1"], max_es1_frac=m["es1_frac"], max_at_ms=m["at_ms"], max_positions=m["positions"], max_pending=m["pending"],
                   max_notional=max(r["notional"] for r in rows), final_es1=rows[-1]["es1"],
                   share_of_time_above_half_cap=sum(1 for r in rows if (r["es1_frac"] or 0) >= ES1_HALF_CAP_FRAC) / len(rows))
    caps = store.conn.execute("SELECT binding_cap, decision, requested_shares, kelly_size, size_e6 FROM decisions WHERE run_id=? AND binding_cap IS NOT NULL", (run_id,)).fetchall()
    binding: Counter = Counter()
    cut: defaultdict = defaultdict(float)
    for r in caps:
        binding[r["binding_cap"]] += 1
        req = r["requested_shares"] or 0.0
        cap_to = req if r["kelly_size"] is None else min(req, r["kelly_size"])
        cut[r["binding_cap"]] += max(0.0, cap_to - (r["size_e6"] or 0) / E6)
    out["binding_cap_counts"] = dict(binding)
    out["shares_cut_by_cap"] = {k: round(v, 1) for k, v in cut.items()}
    return out


def latency_section(store: BacktestStore, run_id: str) -> dict[str, Any]:
    xs = sorted(r[0] for r in store.conn.execute("SELECT latency_ms FROM decisions WHERE run_id=? AND latency_ms IS NOT NULL", (run_id,)))
    if not xs:
        return {}
    return {"n": len(xs), "p50_ms": xs[len(xs) // 2], "p99_ms": xs[min(len(xs) - 1, int(0.99 * len(xs)))], "max_ms": xs[-1],
            "note": "includes cold-start joint model builds on first use of a game; the live bot builds them off the RFQ path"}


def markouts_section(fills: list[dict[str, Any]], rfq_db: Path, books_db: Path, kalshi_db: Path, horizons=(1, 5, 15, 30)) -> dict[str, Any]:
    from ..edge.config import EdgeConfig
    from ..edge.fairs import FairSource
    cfg = EdgeConfig(rfq_db=rfq_db, books_db=books_db, kalshi_db=kalshi_db)
    fs = FairSource(cfg, bucket_ms=1)       # exact instants: the default minute bucket reuses one compute per minute
    res: dict[int, list[tuple[float, float, list[str]]]] = {h: [] for h in horizons}
    missing = Counter()
    for f in fills:
        legs = json.loads(f["legs_json"])
        pids = [l["position_id"] for l in legs]
        prefixes = [game_prefix_of(l.get("slug")) for l in legs]
        t0 = f["executed_at_ms"] or f["at_ms"]
        c0 = fs.combo(pids, prefixes, t0).model
        if c0 is None:
            missing["fair0"] += 1
            continue
        for h in horizons:
            ch = fs.combo(pids, prefixes, t0 + h * 60_000).model
            if ch is None:
                missing[f"fair_{h}m"] += 1
                continue
            res[h].append((f["sign"] * (ch - c0), f["sign"] * (ch - f["price"]), f["games"]))
    out: dict[str, Any] = {"missing": dict(missing), "fair_source": MARKOUT_FAIR_SOURCE}
    for h, rows in res.items():
        if not rows:
            out[f"{h}m"] = {"n": 0}
            continue
        cl = cluster_ids([r[2] for r in rows])
        m0, se0, g = cluster_mean_se([r[0] for r in rows], cl)
        m1, se1, _ = cluster_mean_se([r[1] for r in rows], cl)
        out[f"{h}m"] = {"n": len(rows), "vs_fair_at_fill": m0, "se_fair": se0, "vs_our_price": m1, "se_price": se1, "clusters": g,
                        "games": n_games(r[2] for r in rows)}
    return out


# ------------------------------------------------------------------ assembly and printing
def build_report(store: BacktestStore, run_id: str, *, rfq_db: Path, books_db: Path, kalshi_db: Path, markouts: bool = True) -> dict[str, Any]:
    run = store.run(run_id)
    if run is None:
        raise KeyError(run_id)
    fills = fill_table(store, run_id)
    rep: dict[str, Any] = {"run_id": run_id, "name": run["name"], "group": run["group_id"], "config": json.loads(run["config_json"]),
                           "window": json.loads(run["window_json"]), "summary": json.loads(run["summary_json"] or "{}"),
                           "funnel": funnel(store, run_id), "pnl": pnl_section(fills), "attribution": attribution(fills),
                           "es1": es1_section(store, run_id), "latency": latency_section(store, run_id)}
    rep["funnel"]["strict_range"] = {"win_fills": rep["funnel"]["win"], "strict_fills": rep["funnel"]["strict"]}
    if markouts and fills:
        rep["markouts"] = markouts_section(fills, rfq_db, books_db, kalshi_db)
    for f in fills:
        f.pop("legs_json", None)
    return rep


def fmt(rep: dict[str, Any]) -> str:
    L = [f"== {rep['run_id']}  ({rep['name']})  window {rep['window']['start_ms']}..{rep['window']['end_ms']}"]
    f = rep["funnel"]
    L.append(f"requests seen {f['requests_seen']}  other sport {f['prefiltered_other_sport']}  unknown market {f['prefiltered_unknown_market']}  "
             f"evaluated {f['evaluated']}  priced {f['priced']}  quoted {f['quoted']}  printed {f['printed']}  "
             f"win {f['win']}  strict {f['strict']}  filled {f['filled']}")
    for k in ("by_direction", "by_sport", "by_n_legs", "by_pair_class"):
        L.append(f"  {k[3:]:<11} " + "  ".join(f"{a}: q{v.get('quoted', 0)} p{v.get('printed', 0)} w{v.get('win', 0)}/s{v.get('strict', 0)}" for a, v in f[k].items()))
    L.append("  skips: " + ", ".join(f"{k.replace('skipped:', '')}={v}" for k, v in list(f["skips"].items())[:8]))
    p = rep["pnl"]
    def b(x):
        se = "n/a" if x["se"] is None else f"{x['se']:.2f}"
        gse = "n/a" if x.get("se_game") is None else f"{x['se_game']:.2f}"
        return (f"{x['total']:+.2f} (se {se}, {x['n']} fills, {x['games']} games, {x['clusters']} clusters, {x['flag']}; "
                f"per-game se {gse}, {x.get('game_groups', 0)} games, {x.get('flag_game', '')})")
    L.append(f"gross markup  {b(p['gross_markup'])}")
    L.append(f"realized      {b(p['realized'])}   [{p['resolved_combos']} resolved combos, {p['void_combos']} void, unresolved share "
             f"{'-' if p['unresolved_share'] is None else format(p['unresolved_share'], '.0%')}]")
    L.append(f"marked        {b(p['mark_to_model_unresolved'])}")
    L.append(f"model P&L     {b(p['total_model_pnl'])}")
    a = rep["attribution"]["all"]
    if a.get("n"):
        L.append(f"attribution ({a['n']} fills): pnl {a['pnl']:+.2f} = pricing error {a['pricing_error']:+.2f} + margin {a['margin']:+.2f}; margin by term: "
                 + ", ".join(f"{t} {v:+.2f}" for t, v in a["margin_terms"].items()))
    mo = rep.get("markouts")
    if mo:
        L.append("markouts fair source: " + (mo.get("fair_source") or MARKOUT_FAIR_SOURCE))
        L.append("markouts (per share, maker): " + "  ".join(
            f"{h}: n{v['n']} fair {v['vs_fair_at_fill']:+.4f}" + ("" if v['se_fair'] is None else f"±{v['se_fair']:.4f}") + f" price {v['vs_our_price']:+.4f}"
            for h, v in mo.items() if isinstance(v, dict) and v.get("n")))
    e = rep["es1"]
    if e.get("samples"):
        L.append(f"ES1 max {e['max_es1']:.0f} ({e['max_es1_frac']:.1%} of capital) at {e['max_at_ms']}, {e['max_positions']} positions; "
                 f"caps binding {e['binding_cap_counts']}  shares cut {e['shares_cut_by_cap']}")
    la = rep["latency"]
    if la:
        L.append(f"latency p50 {la['p50_ms']:.1f} ms  p99 {la['p99_ms']:.1f} ms  max {la['max_ms']:.0f} ms")
    return "\n".join(L)


def report_runs(args: Any) -> list[tuple[str, Path]]:
    """Build and write the report JSON of each selected run; returns (formatted report, path) per run."""
    out: list[tuple[str, Path]] = []
    store = BacktestStore(args.out)
    ids = [r["run_id"] for r in store.runs(args.group)] if args.group else [args.run]
    for rid in ids:
        rep = build_report(store, rid, rfq_db=Path(args.rfq_db), books_db=Path(args.books_db), kalshi_db=Path(args.kalshi_db),
                           markouts=not args.no_markouts)
        path = Path(args.json) if args.json and len(ids) == 1 else Path(getattr(args, "reports_dir", None) or "data/backtest_reports") / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep, indent=1, sort_keys=True, default=str))
        out.append((fmt(rep), path))
    store.close()
    return out
