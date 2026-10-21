"""`python -m rfqbot joint` implementation."""
from __future__ import annotations

import itertools
import sys
import time
from typing import Any

from ..sports.legspec import canonical_key
from .joint import DEFAULT_SHOCKS, GameModel, build_game
from .joint_nfl import EngineConfig

PAIR_FAMILIES = ("moneyline", "spread", "total", "team_total")


def load_quotes(args: Any, settings: Any) -> list[Any]:
    if args.synthetic:
        from .synthetic import make_truth, quotes_from_truth
        return quotes_from_truth(make_truth(), game=args.game, seed=args.seed)
    from ..ladders import game_markets
    kw = {}
    if args.books_db:
        kw["books_db"] = args.books_db
    return list(game_markets(args.game, args.at_ms, **kw))


def run_joint(args: Any, settings: Any) -> int:
    quotes = load_quotes(args, settings)
    if not quotes:
        print("no quotes for this game", file=sys.stderr)
        return 2
    two_sided = sum(1 for q in quotes if q.best_bid is not None and q.best_ask is not None)
    print(f"game {args.game}: {len(quotes)} quotes, {two_sided} two-sided")
    t0 = time.perf_counter()
    model = build_game(quotes, config=EngineConfig(n_scenarios=args.n), seed=args.seed)
    print(f"built in {time.perf_counter() - t0:.2f}s  (fit+ipf {model.timings['fit_ipf']:.2f}s, scenarios {model.timings['scenarios']:.2f}s)  "
          f"n={model.scen.n}  game_key={model.game_key}")
    print_report(model, pairs=args.pairs)
    return 0


def print_report(model: GameModel, *, pairs: bool = False, out=print) -> None:
    for per, pm in model.periods.items():
        out(f"\n[{per}] fitted: {sorted(pm.pmfs)}  ipf sweeps={pm.ipf_sweeps} change={pm.ipf_change:.1e} "
            f"residual={ {k: round(v, 4) for k, v in pm.ipf_residual.items()} }")
        out(f"      means M={pm.moments('M')[0]:+.2f} T={pm.moments('T')[0]:.2f} H={pm.moments('H')[0]:.2f} A={pm.moments('A')[0]:.2f}  "
            f"sds M={pm.moments('M')[1]:.2f} T={pm.moments('T')[1]:.2f}  corr(M,T)={pm.corr('M','T'):+.3f} corr(H,A)={pm.corr('H','A'):+.3f}  p_tie={pm.p_tie():.4f}")
        for n in pm.notes:
            out(f"      note: {n}")
        for ref, r in pm.violations:
            out(f"      VIOLATION {ref}: {r:+.1f} err from monotone fit")
    out(f"\nhalves coupling corr H={model.scen.corr_h:.3f} A={model.scen.corr_a:.3f}  residual H={model.scen.halves_residual.get('H', 0):.4f} "
        f"A={model.scen.halves_residual.get('A', 0):.4f}  clamp rate={model.scen.clamp_rate:.2%}")
    out("flags: " + ("; ".join(model.flags) if model.flags else "none"))
    checks = model.marginal_check()
    for label, sel in (("fitted families, informative quotes", lambda c: c.fitted and c.informative),
                       ("other modeled legs, informative quotes", lambda c: not c.fitted and c.informative),
                       ("placeholder quotes (err > 0.05)", lambda c: not c.informative)):
        cs = [c for c in checks if c.diff is not None and sel(c)]
        if not cs:
            continue
        diffs = [c.diff for c in cs]
        out(f"\nmarginal consistency, {label}: {len(cs)} legs, max |engine - fair| = {max(diffs):.4f}, "
            f"mean = {sum(diffs) / len(diffs):.4f}, flagged (> 3 err and > 0.01) = {sum(1 for c in cs if c.flagged)}")
        for c in sorted(cs, key=lambda c: -c.diff)[:5]:
            out(f"   {c.diff:.4f}  fair={c.fair:.4f} engine={c.engine:.4f}  {c.key}{'  FLAG' if c.flagged else ''}")
    unsupported = sorted({canonical_key(l.spec).split('|')[4] + '/' + l.spec.period for l in model.legs if model.p_leg(l.spec) is None})
    if unsupported:
        out("not modeled (caller falls back): " + ", ".join(unsupported))
    if pairs:
        print_pairs(model, out=out)


def print_pairs(model: GameModel, *, out=print, max_per_family: int = 2) -> None:
    picks = []
    seen: dict[tuple[str, str], int] = {}
    for leg in model.legs:
        s = leg.spec
        if s.family not in PAIR_FAMILIES or s.period not in ("FG", "1H") or leg.fair is None:
            continue
        if s.family in ("spread", "total", "team_total") and not (0.35 <= leg.fair.p <= 0.65):
            continue
        k = (s.family, s.period)
        if seen.get(k, 0) >= max_per_family:
            continue
        seen[k] = seen.get(k, 0) + 1
        picks.append(leg)
    out(f"\npairwise joint vs independent ({len(picks)} legs, {len(picks) * (len(picks) - 1) // 2} pairs); interval from {len(DEFAULT_SHOCKS)} shocks")
    out(f"  {'leg A':<48} {'leg B':<48} {'p_a':>6} {'p_b':>6} {'indep':>6} {'joint':>6} {'lo':>6} {'hi':>6} {'lift':>6}")
    t0 = time.perf_counter(); n = 0
    for a, b in itertools.combinations(picks, 2):
        q = model.joint([a.spec, b.spec])
        n += 1
        lift = q.p_joint / q.p_independent if q.p_independent else float("nan")
        out(f"  {_short(a.spec):<48} {_short(b.spec):<48} {q.p_legs[0]:6.3f} {q.p_legs[1]:6.3f} {q.p_independent:6.3f} "
            f"{q.p_joint:6.3f} {q.lo:6.3f} {q.hi:6.3f} {lift:6.2f}")
    if n:
        out(f"  {n} pairs in {time.perf_counter() - t0:.2f}s ({(time.perf_counter() - t0) / n * 1000:.1f} ms/pair incl. shocks)")


def _short(spec) -> str:
    k = canonical_key(spec).split("|")
    return "|".join(k[3:])
