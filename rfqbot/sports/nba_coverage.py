"""NBA coverage report, standalone: ``python -m rfqbot.sports.nba_coverage [--db PATH] [--kalshi] [--gamma N]``.

Builds on the shared, sport-generic ``coverage.report`` (parse rate by family/period, every
unparsed row, match confidence by family, families with no Kalshi counterpart, distinct
settlement notes) and adds what is NBA-specific:

  * ``--kalshi``: sync the registered NBA Kalshi series into ``kalshi_markets`` first, through
    the shared ``KalshiSync`` (one request at a time, backoff on 429).
  * player-prop match rate under the single-candidate rule in ``nba_players`` (same game,
    stat, line, exactly one surname+initial candidate), cross-checked against the shared
    matcher's result for the same legs; any disagreement is printed.
  * ``--gamma N``: validate a stratified sample of N markets against the Gamma API (line,
    sportsMarketType, gameStartTime vs the ET slug date, outcomes, position ids, OT wording
    via the NBA phrases registered in ``SportSpec.extra``) and report disagreements, plus
    NBA-only checks (did-not-play sentence, halftime-tie sentence, spread resolves to the
    favourite, cancellation / postponement sentences). It also counts how many full-game
    spread/total descriptions state OT (the catalog parser records them as "unstated"
    because two templates exist).

Exit status 1 when any row failed to parse or any Gamma disagreement was found.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

from ..store import Store
from . import nba_players, registry
from .coverage import GAMMA_URL, check_gamma, load_kalshi_index, load_poly_specs, report
from .legspec import LegSpec
from .matcher import KalshiIndex, match
from .nba import NBA  # noqa: F401  (registers "nba")

log = logging.getLogger(__name__)

_OT_INCLUDED_PHRASES = ("including any overtime periods", "including all overtime periods", "including overtime")
_OT_EXCLUDED_PHRASES = ("at halftime only", "first half only", "halftime score only")


def player_prop_report(parsed: list[tuple[Any, LegSpec]], idx: KalshiIndex, out=print) -> dict[str, int]:
    """Single-candidate rule over every Polymarket player-prop leg, compared with the shared matcher."""
    kalshi_specs = [k for ks in idx.by_contract.values() for k in ks if k.family in nba_players.PLAYER_FAMILIES]
    by_game: dict[str, list[LegSpec]] = defaultdict(list)
    for k in kalshi_specs:
        by_game[k.game_key].append(k)
    counts: Counter = Counter()
    disagreements: list[str] = []
    no_reason: Counter = Counter()
    for _, p in parsed:
        if p.family not in nba_players.PLAYER_FAMILIES:
            continue
        counts["legs"] += 1
        rule = nba_players.match_player_prop(p, by_game.get(p.game_key, []))
        m = match(p, idx)
        if rule is None:
            counts["rule_none"] += 1
            pool = [k for k in by_game.get(p.game_key, []) if k.family == p.family]
            same_name = [k for k in pool if nba_players.initial_surname(k.venue_ids.get("player", "")) == nba_players.initial_surname(p.venue_ids["player"])]
            if not by_game.get(p.game_key):
                no_reason["no kalshi player markets for game"] += 1
            elif not pool:
                no_reason[f"no kalshi {p.family} for game"] += 1
            elif not same_name:
                no_reason["player not listed on kalshi for this stat"] += 1
            elif len({k.bucket for k in same_name}) > 1:
                no_reason["several kalshi players share initial+surname"] += 1
            else:
                no_reason["player listed but not at this line"] += 1
            if m.confidence in ("exact", "rules_differ"):
                disagreements.append(f"rule none but matcher {m.confidence} {m.kalshi_ticker}: {p.venue_ids['slug']}")
        else:
            kspec, kside = rule
            counts["rule_match"] += 1
            if m.kalshi_ticker != kspec.venue_ids["ticker"] or m.kalshi_side != kside or m.confidence not in ("exact", "rules_differ"):
                disagreements.append(f"rule {kspec.venue_ids['ticker']}/{kside} but matcher {m.confidence} {m.kalshi_ticker}/{m.kalshi_side}: {p.venue_ids['slug']}")
            counts[f"matcher_{m.confidence}"] += 1
        if m.confidence == "shifted_line":
            counts["shifted_line_available"] += 1
    collisions = nba_players.bucket_collisions(kalshi_specs)
    out("\nplayer props (single-candidate rule: same game, stat, line, exactly one initial+surname candidate)")
    if collisions:
        out("  INITIAL+SURNAME COLLISIONS on Kalshi (the rule refuses these; the shared matcher cannot tell them apart):")
        for (g, b), names in sorted(collisions.items()):
            out(f"    {g} {b}: {sorted(names)}")
    else:
        out("  no initial+surname collisions within any game on Kalshi")
    counts["collisions"] = len(collisions)
    legs = counts["legs"] or 1
    out(f"  legs {counts['legs']}  matched {counts['rule_match']} ({100 * counts['rule_match'] / legs:.1f}%)  "
        f"unmatched {counts['rule_none']}  (matcher offers a shifted line for {counts['shifted_line_available']} of the unmatched)")
    for reason, n in no_reason.most_common():
        out(f"    {n:>5}  {reason}")
    if disagreements:
        out("  DISAGREEMENTS between the rule and the shared matcher:")
        for d_ in disagreements[:50]:
            out(f"    {d_}")
    else:
        out("  rule and shared matcher agree on every player-prop leg")
    counts["disagreements"] = len(disagreements)
    return dict(counts)


def nba_gamma_problems(m: dict[str, Any], specs: list[LegSpec]) -> tuple[list[str], dict[str, int]]:
    """Shared Gamma checks (NBA OT phrases come from SportSpec.extra) plus NBA-only checks.

    The one shared problem dropped here is "parsed ot=unstated but description mentions overtime":
    full-game spread/total descriptions come in two templates, one of which states OT, and the
    catalog row cannot tell them apart, so the parser records unstated by design. It is counted
    in the returned stats instead."""
    sp = registry.get("nba")
    problems = [p for p in check_gamma(m, specs, sp) if p != "parsed ot=unstated but description mentions overtime"]
    desc = (m.get("description") or "").lower()
    s0 = specs[0]
    ot = s0.rules.get("ot")
    stats: Counter = Counter()
    says_included = any(p in desc for p in _OT_INCLUDED_PHRASES)
    if ot == "excluded" and not any(p in desc for p in _OT_EXCLUDED_PHRASES):
        problems.append("parsed ot=excluded but description does not say halftime only")
    if ot == "unstated":
        stats[f"{s0.family}/{s0.period} ot {'stated' if says_included else 'not stated'} in description"] += 1
    if s0.family in nba_players.PLAYER_FAMILIES:
        if "does not take the court at any point, the market will resolve \"no\"" not in desc:
            problems.append("player prop description lacks the did-not-play -> No sentence")
        name = s0.venue_ids.get("player", "")
        if name.lower() not in desc:
            problems.append(f"player name {name!r} not in description")
    if s0.family == "moneyline" and s0.period == "1H" and "tied at halftime, this market will resolve 50-50" not in desc:
        problems.append("1H moneyline description lacks the halftime-tie 50-50 sentence")
    if s0.family == "spread":
        from . import nba_teams
        nick = nba_teams.by_code(s0.team).nickname.lower()
        if f'resolve to "{nick}"' not in desc:
            problems.append(f"spread description does not resolve to {nick!r} first")
    if "resolve 50-50" not in desc:
        problems.append("description lacks the cancellation 50-50 sentence")
    if "postponed" not in desc:
        problems.append("description lacks the postponement sentence")
    return problems, dict(stats)


async def gamma_validate_nba(parsed: list[tuple[Any, LegSpec]], sample: int, *, seed: int = 1, out=print,
                             client: httpx.AsyncClient | None = None) -> int:
    rng = random.Random(seed)
    by_slug: dict[str, list[LegSpec]] = defaultdict(list)
    for _, s in parsed:
        by_slug[s.venue_ids["slug"]].append(s)
    first: dict[tuple[str, str], str] = {}
    for slug, specs in by_slug.items():
        first.setdefault((specs[0].family, specs[0].period), slug)
    chosen = list(first.values())
    rest = [s for s in by_slug if s not in set(chosen)]
    rng.shuffle(rest)
    chosen += rest[: max(0, sample - len(chosen))]
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 (rfqbot nba-coverage)"})
    n_bad = n_missing = 0
    stats: Counter = Counter()
    try:
        for slug in chosen:
            r = await client.get(f"{GAMMA_URL}/markets", params={"slug": slug})
            r.raise_for_status()
            body = r.json()
            if not body:
                n_missing += 1
                out(f"  {slug}: NOT FOUND on gamma (catalog row with no live Gamma market)")
                continue
            problems, st = nba_gamma_problems(body[0], by_slug[slug])
            stats.update(st)
            if problems:
                n_bad += 1
                out(f"  {slug}: " + "; ".join(problems))
            await asyncio.sleep(0.15)
    finally:
        if own:
            await client.aclose()
    out(f"  checked {len(chosen)} markets, {n_bad} with disagreements, {n_missing} not on gamma")
    for k, v in sorted(stats.items()):
        out(f"    {v:>4}  {k}")
    return n_bad


async def sync_kalshi(store: Store) -> None:
    from ..kalshi import KalshiSync
    res = await KalshiSync(store).sync("nba")
    total = sum(n for n, _ in res.values())
    errs = sum(e for _, e in res.values())
    print(f"kalshi sync: {total} markets, {errs} parse errors")
    for s, (n, e) in sorted(res.items()):
        if n or e:
            print(f"  {s:<20} {n:>5} markets  {e:>3} parse errors")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rfqbot.sports.nba_coverage", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="SQLite path (default: RFQ_DB_PATH from .env, else data/rfq.db)")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--kalshi", action="store_true", help="sync Kalshi NBA markets into kalshi_markets first")
    ap.add_argument("--gamma", type=int, default=0, metavar="N", help="validate a sample of N markets against Gamma")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = args.db
    if db is None:
        try:
            from ..config import load_settings
            db = load_settings(args.env).db_path
        except Exception:  # noqa: BLE001  config is optional for a report
            db = "data/rfq.db"
    path = Path(db)
    if not path.exists():
        print(f"no database at {path}", file=sys.stderr)
        return 2
    store = Store(path)
    try:
        if args.kalshi:
            asyncio.run(sync_kalshi(store))
        res = report(store, "nba", gamma_sample=0, seed=args.seed)
        parsed, _failed, _ = load_poly_specs(store, registry.get("nba"))
        idx, _, _ = load_kalshi_index(store, "nba")
        if idx.n:
            res["player_props"] = player_prop_report(parsed, idx)
        if args.gamma:
            print(f"\nGamma validation on a sample of {args.gamma} markets")
            res["gamma_disagreements"] = asyncio.run(gamma_validate_nba(parsed, args.gamma, seed=args.seed))
    finally:
        store.close()
    print("\nsummary: " + json.dumps({k: v for k, v in res.items() if not isinstance(v, dict)}))
    return 1 if res["failed"] or res.get("gamma_disagreements") else 0


if __name__ == "__main__":
    sys.exit(main())
