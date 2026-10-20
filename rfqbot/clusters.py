"""Leg independence classifier + cluster pricing for combo RFQs.

Why: in the 2026-10-20 75 s live sample 97.2% of two-leg combos pair legs from different games,
so their fair price is the product of the marginals; the rest need the joint engine
(``rfqbot.pricing``) or cannot be priced at all. This module decides which is which,
deterministically, from catalog rows (position_id, condition_id, slug[, outcome_index]) and
assembles a combo probability from per-cluster probabilities.

Pair classes, ordered; the first that applies wins (``classify_pair``):
  1. ``same_market``          same condition_id. Two different positions of one market are
                              complementary (joint 0); the same position twice is just that leg.
  2. ``same_group``           siblings in a mutually exclusive group of one game: per-sport slug
                              families in ``EXCLUSIVE_FAMILIES`` (NFL exact-margin-*,
                              highest-scoring-quarter-*; soccer exact-score-*, halftime-result-*,
                              second-half-result-*). Two YES legs cannot both pay (joint 0); with a
                              NO leg involved the engine decides.
  3. ``same_game``            same game key: the joint engine's business (over/under at one line,
                              two covers of one team, ...).
  4. ``no_game_key``          either leg has no game key (politics, futures, props without a game
                              slug). Never certified: the combo is flagged ``unpriceable_dependence``.
  5. ``shared_team_diff_game`` different games, same league, a team code in common. Priced as
                              independent, tagged ``shared_team`` for stage 3 exposure netting.
  6. ``disjoint``             certified independent.

Game key (``leg_ref``): for a registered sport (``rfqbot.sports.registry.sport_for_slug``) it is
``LegSpec.game_key`` as the sport's slug-only parse would produce it, ``{sport}|{slug_date}|{AWAY}@{HOME}``
with canonical team codes from the sport's team table (so an NFL slug date stays the UTC slug
date, exactly as nfl_poly emits it). Otherwise the generic team-sport prefix
``{league}-{a}-{b}-{YYYY-MM-DD}`` (100% of team-sport slugs) gives ``{league}|{date}|{a}@{b}``, and the
tennis doubles shape ``{atp|wta}-doubles-{a}-{b}-{date}`` gives ``{league}-doubles|{date}|{a}@{b}``.
Anything else -> no game key. Shared-team detection uses canonical codes for registered sports
and the generic slug codes otherwise, always qualified by league so "mia" in the NBA and the NFL
never collide.

Pricing (``combo_probability``): the combo pays only when every leg pays, so
P = product over clusters of P(cluster). A one-leg cluster is its marginal. A multi-leg cluster
is ``joint_fn(cluster)`` (the engine); when that returns None the fallback is the independent
product clamped to the cluster's Fréchet bounds [max(0, sum p - (n-1)), min p], with flag
``fallback:{cluster key}``. ``same_market`` / ``same_group`` YES-YES clusters are 0 without asking
the engine. A clustering with ``no_game_key`` legs returns ``p=None`` and the flag
``unpriceable_dependence``. The result also carries the fully independent product and the
Fréchet bounds of the whole combo, so callers can see how much the engine moved the price.

Combo side semantics (what the combo YES pays, maker direction, size units) live with quote
assembly in ``rfqbot.quoting.semantics`` (NFL session), not here.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

PAIR_CLASSES = ("same_market", "same_group", "same_game", "no_game_key", "shared_team_diff_game", "disjoint")
PairClass = str

#: league token (or registered sport name) -> slug family prefixes whose markets are mutually exclusive
#: within one game. ``"*"`` applies to every league (the soccer families only occur in soccer slugs).
EXCLUSIVE_FAMILIES: dict[str, tuple[str, ...]] = {
    "nfl": ("exact-margin-", "highest-scoring-quarter-"),
    "*": ("exact-score-", "halftime-result-", "second-half-result-"),
}

_GENERIC = re.compile(r"^(?P<league>[a-z0-9]+)-(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<rest>.*))?$")
_DOUBLES = re.compile(r"^(?P<league>atp|wta)-doubles-(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<rest>.*))?$")


@dataclass(frozen=True)
class LegRef:
    position_id: str
    condition_id: str | None
    slug: str | None
    outcome_index: int | None = None
    league: str | None = None          # sport name for registered sports, else the slug's league token
    game_key: str | None = None
    teams: frozenset[str] = frozenset()   # "{league}:{code}"
    group: str | None = None           # "{game_key}|{family}" for exclusive families
    basis: str = "none"                # registry | generic | doubles | none

    @property
    def is_yes(self) -> bool | None:
        return None if self.outcome_index is None else self.outcome_index == 0


def _registered(slug: str):
    try:
        from .sports import registry
        from . import sports  # noqa: F401  (registers built-in sports)
        return registry.sport_for_slug(slug)
    except Exception:  # registry unavailable
        return None


def _team_code(sport: str, slug_code: str) -> str:
    """Canonical code from rfqbot.sports.{sport}_teams.by_poly_slug; the uppercase slug code if no table."""
    try:
        mod = importlib.import_module(f"rfqbot.sports.{sport}_teams")
        return mod.by_poly_slug(slug_code).code
    except Exception:
        return slug_code.upper()


def leg_ref(position_id: str, condition_id: str | None, slug: str | None, outcome_index: int | None = None) -> LegRef:
    """Classify one catalog row's slug into league / game key / teams / exclusive group."""
    slug = slug or ""
    m = _DOUBLES.match(slug)
    if m:
        league = f"{m['league']}-doubles"
        key = f"{league}|{m['date']}|{m['a']}@{m['b']}"
        return LegRef(position_id, condition_id, slug, outcome_index, league, key,
                      frozenset({f"{league}:{m['a']}", f"{league}:{m['b']}"}), None, "doubles")
    m = _GENERIC.match(slug)
    if not m:
        return LegRef(position_id, condition_id, slug or None, outcome_index)
    sp = _registered(slug)
    if sp is not None:
        league, basis = sp.sport, "registry"
        a, b = _team_code(sp.sport, m["a"]), _team_code(sp.sport, m["b"])
    else:
        league, basis = m["league"], "generic"
        a, b = m["a"], m["b"]
    key = f"{league}|{m['date']}|{a}@{b}"
    rest = m["rest"] or ""
    group = None
    for fam in EXCLUSIVE_FAMILIES.get(league, ()) + EXCLUSIVE_FAMILIES.get("*", ()):
        if rest.startswith(fam):
            group = f"{key}|{fam.rstrip('-')}"
            break
    return LegRef(position_id, condition_id, slug, outcome_index, league, key, frozenset({f"{league}:{a}", f"{league}:{b}"}), group, basis)


def classify_pair(a: LegRef, b: LegRef) -> PairClass:
    if a.condition_id and a.condition_id == b.condition_id:
        return "same_market"
    if a.group is not None and a.group == b.group:
        return "same_group"
    if a.game_key is not None and a.game_key == b.game_key:
        return "same_game"
    if a.game_key is None or b.game_key is None:
        return "no_game_key"
    if a.teams & b.teams:
        return "shared_team_diff_game"
    return "disjoint"


# ---------------------------------------------------------------------- cluster
@dataclass
class Cluster:
    game_key: str | None
    legs: list[LegRef]
    classes: set[str] = field(default_factory=set)   # pair classes that joined these legs

    @property
    def key(self) -> str:
        return self.game_key or f"nokey:{self.legs[0].position_id}"

    @property
    def position_ids(self) -> list[str]:
        return [l.position_id for l in self.legs]

    def __len__(self) -> int:
        return len(self.legs)


@dataclass
class Clustering:
    clusters: list[Cluster]
    pair_classes: dict[tuple[str, str], PairClass]
    certified_independent: bool
    flags: list[str] = field(default_factory=list)
    legs: list[LegRef] = field(default_factory=list)

    @property
    def class_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.pair_classes.values():
            out[c] = out.get(c, 0) + 1
        return out


def _as_ref(row: Any) -> LegRef:
    if isinstance(row, LegRef):
        return row
    if isinstance(row, Mapping):
        return leg_ref(str(row["position_id"]), row.get("condition_id"), row.get("slug"), row.get("outcome_index"))
    pid, cond, slug = row[0], row[1], row[2]
    oi = row[3] if len(row) > 3 else None
    return leg_ref(str(pid), cond, slug, oi)


def cluster(legs: Iterable[Any]) -> Clustering:
    """Union legs joined by same_market / same_group / same_game into clusters (union-find).
    ``legs`` are LegRefs, dicts or tuples (position_id, condition_id, slug[, outcome_index])."""
    refs = [_as_ref(r) for r in legs]
    parent = list(range(len(refs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pair_classes: dict[tuple[str, str], PairClass] = {}
    joined: dict[int, set[str]] = {}
    flags: set[str] = set()
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            c = classify_pair(refs[i], refs[j])
            pair_classes[(refs[i].position_id, refs[j].position_id)] = c
            if c in ("same_market", "same_group", "same_game"):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
                joined.setdefault(find(i), set()).add(c)
            elif c == "no_game_key":
                flags.add("unpriceable_dependence")
            elif c == "shared_team_diff_game":
                flags.add("shared_team")
    groups: dict[int, list[int]] = {}
    for i in range(len(refs)):
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for root, members in groups.items():
        cl = Cluster(refs[members[0]].game_key, [refs[k] for k in members], set(joined.get(root, ())))
        clusters.append(cl)
    # a single no-game-key leg on its own is just its marginal; only a PAIR involving one is unpriceable
    certified = all(len(c) == 1 for c in clusters) and "unpriceable_dependence" not in flags
    if any(len(c) > 1 for c in clusters):
        flags.add("needs_joint_engine")
    return Clustering(clusters, pair_classes, certified, sorted(flags), refs)


# ---------------------------------------------------------------------- pricing
JointFn = Callable[[Cluster], "float | None"]


@dataclass
class ClusterProb:
    key: str
    position_ids: list[str]
    p: float | None
    source: str              # marginal | engine | fallback | impossible | unpriceable


@dataclass
class ComboProb:
    p: float | None
    clusters: list[ClusterProb]
    independent_product: float | None
    frechet_lower: float | None
    frechet_upper: float | None
    flags: list[str]


def frechet_bounds(ps: Sequence[float]) -> tuple[float, float]:
    """Bounds on P(all of n events) given their marginals: [max(0, sum p - (n-1)), min p]."""
    if not ps:
        return 1.0, 1.0
    return max(0.0, sum(ps) - (len(ps) - 1)), min(ps)


def _product(ps: Iterable[float]) -> float:
    out = 1.0
    for p in ps:
        out *= p
    return out


def combo_probability(clustering: Clustering, p_leg: Mapping[str, float], joint_fn: JointFn | None = None) -> ComboProb:
    """Combo YES probability = product over clusters; multi-leg clusters via ``joint_fn`` (engine),
    else the Fréchet-clamped independent product with a ``fallback:`` flag. ``p_leg`` maps
    position_id -> marginal probability that the leg pays."""
    flags = list(clustering.flags)
    out: list[ClusterProb] = []
    missing = [l.position_id for l in clustering.legs if l.position_id not in p_leg]
    if missing:
        flags.append("missing_marginals:" + ",".join(missing))
    all_ps = [p_leg[l.position_id] for l in clustering.legs if l.position_id in p_leg]
    indep = _product(all_ps) if not missing else None
    lo, hi = frechet_bounds(all_ps) if not missing else (None, None)
    unpriceable = "unpriceable_dependence" in clustering.flags
    for cl in clustering.clusters:
        ps = [p_leg[pid] for pid in cl.position_ids if pid in p_leg]
        if len(ps) != len(cl):
            out.append(ClusterProb(cl.key, cl.position_ids, None, "unpriceable")); continue
        if len(cl) == 1:
            out.append(ClusterProb(cl.key, cl.position_ids, ps[0], "marginal")); continue
        # structurally impossible combinations: two different positions of one market, or two YES
        # legs of one exclusive group
        if len({l.position_id for l in cl.legs}) == 1:
            out.append(ClusterProb(cl.key, cl.position_ids, ps[0], "marginal")); continue
        if len({l.condition_id for l in cl.legs}) < len(cl):
            out.append(ClusterProb(cl.key, cl.position_ids, 0.0, "impossible")); continue
        if "same_group" in cl.classes and all(l.is_yes for l in cl.legs) and len({l.group for l in cl.legs}) == 1:
            out.append(ClusterProb(cl.key, cl.position_ids, 0.0, "impossible")); continue
        p = joint_fn(cl) if joint_fn is not None else None
        if p is not None:
            out.append(ClusterProb(cl.key, cl.position_ids, float(p), "engine"))
        else:
            clo, chi = frechet_bounds(ps)
            out.append(ClusterProb(cl.key, cl.position_ids, min(chi, max(clo, _product(ps))), "fallback"))
            flags.append(f"fallback:{cl.key}")
    if unpriceable or any(c.p is None for c in out):
        if unpriceable and "unpriceable_dependence" not in flags:
            flags.append("unpriceable_dependence")
        return ComboProb(None, out, indep, lo, hi, sorted(set(flags)))
    return ComboProb(_product(c.p for c in out), out, indep, lo, hi, sorted(set(flags)))
