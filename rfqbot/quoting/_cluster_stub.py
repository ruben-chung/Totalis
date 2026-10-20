"""Local stand-in for ``rfqbot.clusters`` (NBA session, in flight). Same interface, same class
order as docs/independence-classifier-brief.md; delete when the real module lands
(``assemble.py`` imports ``rfqbot.clusters`` first and falls back to this)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

GAME_RE = re.compile(r"^([a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")
DOUBLES_RE = re.compile(r"^([a-z]+-doubles-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2})(?:-|$)")
EXCLUSIVE_GROUPS = {
    "nfl": ("exact-margin-", "highest-scoring-quarter-"),
    "ucl": ("exact-score-", "halftime-result-", "second-half-result-"),
    "epl": ("exact-score-", "halftime-result-", "second-half-result-"),
}


@dataclass(frozen=True)
class Leg:
    position_id: str
    condition_id: str | None
    slug: str | None


@dataclass
class Cluster:
    game_key: str | None
    legs: list[Leg]
    pair_class: str = "single"          # same_market | same_group | same_game | no_game_key | single


@dataclass
class Clustering:
    clusters: list[Cluster]
    pair_classes: dict[tuple[str, str], str] = field(default_factory=dict)
    certified_independent: bool = False
    flags: list[str] = field(default_factory=list)


def game_key_of(slug: str | None) -> str | None:
    if not slug:
        return None
    m = GAME_RE.match(slug) or DOUBLES_RE.match(slug)
    return m.group(1) if m else None


def _group_of(slug: str | None, game: str | None) -> str | None:
    if not slug or not game:
        return None
    rest = slug[len(game) + 1:]
    for pfx in EXCLUSIVE_GROUPS.get(game.split("-")[0], ()):
        if rest.startswith(pfx):
            return pfx
    return None


def classify_pair(a: Leg, b: Leg) -> str:
    if a.condition_id and a.condition_id == b.condition_id:
        return "same_market"
    ga, gb = game_key_of(a.slug), game_key_of(b.slug)
    if ga and ga == gb:
        if _group_of(a.slug, ga) and _group_of(a.slug, ga) == _group_of(b.slug, gb):
            return "same_group"
        return "same_game"
    if ga is None or gb is None:
        return "no_game_key"
    return "disjoint"


def cluster(legs: list[Any]) -> Clustering:
    ls = [l if isinstance(l, Leg) else Leg(str(l["position_id"]), l["condition_id"], l["slug"]) for l in legs]
    pair_classes: dict[tuple[str, str], str] = {}
    parent = {l.position_id: l.position_id for l in ls}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ls)):
        for j in range(i + 1, len(ls)):
            c = classify_pair(ls[i], ls[j])
            pair_classes[(ls[i].position_id, ls[j].position_id)] = c
            if c in ("same_market", "same_group", "same_game"):
                parent[find(ls[i].position_id)] = find(ls[j].position_id)
    groups: dict[str, list[Leg]] = {}
    for l in ls:
        groups.setdefault(find(l.position_id), []).append(l)
    clusters = []
    flags: list[str] = []
    for members in groups.values():
        gk = game_key_of(members[0].slug)
        if gk is None:
            flags.append("no_game_key")
            pc = "no_game_key"
        elif len(members) == 1:
            pc = "single"
        else:
            classes = {pair_classes.get((a.position_id, b.position_id)) or pair_classes.get((b.position_id, a.position_id))
                       for a in members for b in members if a is not b}
            pc = "same_market" if "same_market" in classes else "same_group" if "same_group" in classes else "same_game"
        clusters.append(Cluster(gk, members, pc))
    certified = all(c.pair_class == "single" for c in clusters) and "no_game_key" not in flags
    return Clustering(clusters, pair_classes, certified, flags)
