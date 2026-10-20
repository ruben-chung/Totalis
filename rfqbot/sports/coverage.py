"""`coverage --sport X` report: how much of a sport's catalog parses, matches Kalshi, and agrees with Gamma."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from ..kalshi import spec_from_row
from ..store import Store
from . import registry
from .legspec import LegSpec, et_date_candidates
from .matcher import KalshiIndex, match
from .nfl_poly import ET, player_prop_family, slug_family_token
from .registry import SportSpec

log = logging.getLogger(__name__)
GAMMA_URL = "https://gamma-api.polymarket.com"

# Description phrases that confirm a parsed OT rule. A sport overrides or extends these via
# SportSpec.extra["ot_phrases"] = {"included": [...], "excluded": [...]} (lowercase substrings).
DEFAULT_OT_PHRASES: dict[str, tuple[str, ...]] = {
    "included": ("overtime is included", "any overtime count", "including any overtime", "including overtime"),
    "excluded": ("overtime is not included", "excluding overtime", "overtime does not count"),
}

def load_poly_specs(store: Store, sp: SportSpec) -> tuple[list[tuple[Any, LegSpec]], list[tuple[Any, str]], int]:
    """-> (parsed rows, failed rows, rows tagged with the sport whose slug lacks its prefix)."""
    parsed, failed = [], []
    rows = store.conn.execute("SELECT * FROM markets WHERE slug LIKE ? ORDER BY slug, outcome_index", (sp.slug_prefix + "%",)).fetchall()
    for r in rows:
        try:
            parsed.append((r, sp.parse_poly(r["slug"], r["title"], r["outcome"], r["outcome_index"], position_id=r["position_id"])))
        except ValueError as e:
            prop = player_prop_family(r["slug"]) if sp.sport == "nfl" else None
            failed.append((r, f"player prop ({prop}): not modeled" if prop else str(e)))
    foreign = store.scalar("SELECT COUNT(*) FROM markets WHERE tags LIKE ? AND slug NOT LIKE ?",
                           f'%"{sp.sport}"%', sp.slug_prefix + "%") or 0
    return parsed, failed, foreign


def load_kalshi_index(store: Store, sport: str = "nfl") -> tuple[KalshiIndex, int, Counter]:
    idx = KalshiIndex()
    errors: Counter = Counter()
    n_rows = 0
    for r in store.iter_kalshi_markets(sport, latest_only=True):
        n_rows += 1
        if not r["parse_ok"]:
            errors[(r["series_ticker"], (r["parse_error"] or "")[:60])] += 1
            continue
        idx.add(spec_from_row(r))
    return idx, n_rows, errors


def report(store: Store, sport: str = "nfl", *, gamma_sample: int = 0, seed: int = 1, out=print) -> dict[str, Any]:
    sp = registry.get(sport)
    parsed, failed, foreign = load_poly_specs(store, sp)
    total = len(parsed) + len(failed)
    out(f"{sport.upper()} position rows: {total}  parsed: {len(parsed)} ({100 * len(parsed) / max(total, 1):.2f}%)  failed: {len(failed)}  "
        f"markets: {len({r['slug'] for r, _ in parsed} | {r['slug'] for r, _ in failed})}  "
        f"(rows tagged {sport} with a non-{sport} slug: {foreign})")

    by_fp_ok: Counter = Counter((s.family, s.period) for _, s in parsed)
    by_fp_fail: Counter = Counter((slug_family_token(r["slug"]), "?") for r, _ in failed)
    out("\nparsed by family / period")
    for (fam, per), n in sorted(by_fp_ok.items()):
        out(f"  {fam:<24} {per:<3} {n:>6}")
    props = [(r, e) for r, e in failed if e.startswith("player prop")]
    failed = [(r, e) for r, e in failed if not e.startswith("player prop")]
    if props:
        out(f"\nplayer props (not modeled): {len(props)} rows, by token: " + ", ".join(
            f"{k}={n}" for k, n in Counter(e.split('(')[1].split(')')[0] for _, e in props).most_common()))
    if failed:
        out("\nUNPARSED rows (slug | title | outcome | error)")
        for r, err in failed[:200]:
            out(f"  {r['slug']} | {r['title']} | {r['outcome']} | {err}")
        out("  unparsed by slug family: " + ", ".join(f"{k[0]}={n}" for k, n in by_fp_fail.most_common()))
    else:
        out("\nunparsed rows: none")

    games = sorted({(s.away, s.home, s.game_date) for _, s in parsed})
    out(f"\ngames in catalog: {len(games)}")

    idx, k_rows, k_errors = load_kalshi_index(store, sport)
    out(f"\nkalshi_markets rows: {k_rows}  parsed into index: {idx.n}  parse errors: {sum(k_errors.values())}")
    for (series, err), n in k_errors.most_common(20):
        out(f"  {n:>5} {series:<18} {err}")
    result: dict[str, Any] = {"rows": total, "parsed": len(parsed), "failed": len(failed), "player_props": len(props), "kalshi_rows": k_rows}
    if idx.n == 0:
        out(f"  (run `python -m rfqbot kalshi-sync --sport {sport}` to populate; skipping match report)")
    else:
        conf_by_family: dict[str, Counter] = defaultdict(Counter)
        notes_seen: Counter = Counter()
        none_reasons: Counter = Counter()
        kgames = {(a, h, d) for (a, h), ds in idx.games.items() for d in ds}
        for _, s in parsed:
            m = match(s, idx)
            conf_by_family[s.family][m.confidence] += 1
            for n in m.notes:
                if n.startswith("line:"):
                    continue
                (none_reasons if n.startswith("none:") else notes_seen)[n] += 1
        out("\nmatch confidence by family (position rows)")
        out(f"  {'family':<24} {'exact':>7} {'rules_differ':>13} {'shifted_line':>13} {'none':>7}")
        tot: Counter = Counter()
        for fam in sorted(conf_by_family):
            c = conf_by_family[fam]
            tot.update(c)
            out(f"  {fam:<24} {c['exact']:>7} {c['rules_differ']:>13} {c['shifted_line']:>13} {c['none']:>7}")
        out(f"  {'TOTAL':<24} {tot['exact']:>7} {tot['rules_differ']:>13} {tot['shifted_line']:>13} {tot['none']:>7}")
        result["confidence"] = dict(tot)

        poly_games_in_kalshi = sum(1 for a, h, d in games if any((a, h, dd) in kgames for dd in et_date_candidates(
            LegSpec(venue="polymarket", sport=sport, away=a, home=h, game_date=d, family="total", period="FG", side="over", line=1.5, game_date_basis=sp.game_date_basis))))
        out(f"\npoly games with a kalshi game: {poly_games_in_kalshi}/{len(games)}  (kalshi games indexed: {len(kgames)})")

        out("\npoly family/period with NO kalshi counterpart")
        kalshi_fp = {(f, p) for f, p, _ in sp.kalshi_series.values()}
        for (fam, per), n in sorted(by_fp_ok.items()):
            if (fam, per) not in kalshi_fp:
                out(f"  {fam:<24} {per:<3} {n:>6} rows  no kalshi series")
            elif (fam, per) not in idx.families:
                series = [s for s, (f, p, _) in sp.kalshi_series.items() if (f, p) == (fam, per)]
                out(f"  {fam:<24} {per:<3} {n:>6} rows  series {'/'.join(series)} exists but no open markets")
        out("\nkalshi game-level series with no poly family (not synced): " + ", ".join(sp.kalshi_series_without_poly))

        out("\ndistinct settlement-rule notes (rows affected)")
        for n, c in sorted(notes_seen.items(), key=lambda kv: (-kv[1], kv[0])):
            out(f"  {c:>6}  {n}")
        out("\nunmatched reasons (rows)")
        for n, c in none_reasons.most_common():
            out(f"  {c:>6}  {n}")

    if gamma_sample:
        out(f"\nGamma validation on a sample of {gamma_sample} markets")
        disagreements = asyncio.run(gamma_validate(parsed, gamma_sample, sp, seed=seed, out=out))
        result["gamma_disagreements"] = disagreements
    return result


async def gamma_validate(parsed: list[tuple[Any, LegSpec]], sample: int, sp: SportSpec, *, seed: int = 1, out=print,
                         client: httpx.AsyncClient | None = None) -> int:
    """Fetch /markets?slug= for a stratified sample (one slug per family/period first, then random)
    and report every field where the slug-derived parse disagrees with Gamma."""
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
    chosen = chosen[:sample] if sample < len(first) else chosen

    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    n_bad = 0
    try:
        for slug in chosen:
            r = await client.get(f"{GAMMA_URL}/markets", params={"slug": slug})
            r.raise_for_status()
            body = r.json()
            if not body:
                out(f"  {slug}: NOT FOUND on gamma"); n_bad += 1
                continue
            m = body[0]
            specs = by_slug[slug]
            problems = check_gamma(m, specs, sp)
            if problems:
                n_bad += 1
                out(f"  {slug}: " + "; ".join(problems))
            await asyncio.sleep(0.1)
    finally:
        if own:
            await client.aclose()
    out(f"  checked {len(chosen)} markets, {n_bad} with disagreements")
    return n_bad


def check_gamma(m: dict[str, Any], specs: list[LegSpec], sp: SportSpec) -> list[str]:
    """Compare one Gamma market record against the LegSpecs parsed from its catalog rows."""
    s0 = specs[0]
    problems: list[str] = []
    smt = m.get("sportsMarketType")
    fp = sp.gamma_types.get(smt or "")
    if fp is None:
        problems.append(f"unknown sportsMarketType {smt!r}")
    elif fp != (s0.family, s0.period):
        problems.append(f"sportsMarketType {smt} -> {fp} != parsed {(s0.family, s0.period)}")

    gline = m.get("line")
    if s0.line is None:
        if gline not in (None, ""):
            problems.append(f"gamma line {gline} but parsed none")
    else:
        try:
            gl = float(gline)
        except (TypeError, ValueError):
            problems.append(f"gamma line {gline!r} unusable, parsed {s0.line}")
        else:
            # Gamma spread lines are negative for the title team on BOTH spread-home and spread-away;
            # our spread line is negative for the subject team, so they must be equal. Totals positive.
            if gl != s0.line:
                problems.append(f"gamma line {gl} != parsed {s0.line}")

    gst = m.get("gameStartTime")
    if gst:
        try:
            ko = datetime.fromisoformat(gst.replace(" ", "T").replace("+00", "+00:00"))
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
        except ValueError:
            problems.append(f"gameStartTime {gst!r} unparseable")
        else:
            utc_date, et_date = ko.astimezone(timezone.utc).date().isoformat(), ko.astimezone(ET).date().isoformat()
            slug_expect = utc_date if sp.slug_date_basis == "utc" else et_date
            if slug_expect != s0.game_date:
                problems.append(f"slug date {s0.game_date} != gameStartTime {sp.slug_date_basis.upper()} date {slug_expect}")
            if et_date not in et_date_candidates(s0):
                problems.append(f"ET date {et_date} not in candidates {et_date_candidates(s0)}")
    try:
        outcomes = json.loads(m.get("outcomes") or "[]") if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
    except ValueError:
        outcomes = []
    want = [sp.side for sp in sorted(specs, key=lambda sp: int(sp.venue_ids.get("position_id", "0")))]
    if len(outcomes) != 2 or len(specs) != 2:
        problems.append(f"outcomes {outcomes} vs parsed sides {want}")
    else:
        sides = sorted(x.side for x in specs)
        if sorted(o.lower() for o in outcomes) != sides:
            # team outcomes: the catalog row's outcome label must have produced the parsed side already
            labels = {x.venue_ids.get("outcome_label", "").lower() for x in specs}
            if labels != {o.lower() for o in outcomes}:
                problems.append(f"outcomes {outcomes} != catalog outcome labels {sorted(labels)}")
    desc = (m.get("description") or "").lower()
    ot = s0.rules.get("ot")
    phrases = {**DEFAULT_OT_PHRASES, **sp.extra.get("ot_phrases", {})}
    if ot in ("included", "excluded") and "overtime" in desc and not any(ph in desc for ph in phrases[ot]):
        problems.append(f"parsed ot={ot} but description does not say so")
    if ot == "unstated" and "overtime" in desc:
        problems.append("parsed ot=unstated but description mentions overtime")
    if s0.family == "moneyline" and "resolve 50-50" not in desc and "tie" in desc:
        problems.append("moneyline tie rule not 50-50 in description")
    pids = m.get("positionIds")
    if isinstance(pids, str):
        try:
            pids = json.loads(pids)
        except ValueError:
            pids = None
    if pids and sorted(str(p) for p in pids) != sorted(sp.venue_ids.get("position_id", "") for sp in specs):
        problems.append("positionIds differ from catalog")
    return problems
