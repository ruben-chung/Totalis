import gzip
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from rfqbot.clusters import (GENERIC_LEAGUES, classify_pair, cluster, combo_probability, frechet_bounds, leg_ref)

FIX = Path(__file__).parent / "fixtures"
SAMPLE = FIX / "rfq_sample_75s.jsonl.gz"  # 75 s live RFQ capture, 2026-10-20 (ground truth for the shares)


def L(pid, cond, slug, oi=None):
    return leg_ref(pid, cond, slug, oi)


# ----------------------------------------------------------------- leg_ref
def test_registered_sport_game_key_matches_legspec_convention():
    nfl = L("1", "c1", "nfl-la-sea-2026-11-03-spread-home-7pt5")
    assert nfl.basis == "registry" and nfl.league == "nfl" and nfl.game_key == "nfl|2026-11-03|LAR@SEA"   # Rams: la -> LAR
    assert nfl.teams == {"nfl:LAR", "nfl:SEA"} and nfl.group is None
    nba = L("2", "c2", "nba-gsw-lal-2026-10-21-points-stephen-curry-24pt5")
    assert nba.game_key == "nba|2026-10-21|GSW@LAL"
    # the registry parser produces the same key from the full row
    from rfqbot.sports.nba_poly import parse_poly_nba
    assert parse_poly_nba("nba-gsw-lal-2026-10-21", "Warriors vs. Lakers", "Warriors", 0).game_key == nba.game_key


def test_generic_doubles_and_no_key():
    g = L("3", "c3", "ucl-psg-fcb-2026-10-20-total-3pt5")
    assert g.basis == "generic" and g.game_key == "ucl|2026-10-20|psg@fcb" and g.teams == {"ucl:psg", "ucl:fcb"}
    d = L("4", "c4", "atp-doubles-doumste-griezan-2026-10-22")
    assert d.basis == "doubles" and d.game_key == "atp-doubles|2026-10-22|doumste@griezan"
    w = L("5", "c5", "wta-doubles-tangxu-routsut-2026-10-19-set-1-winner")
    assert w.basis == "doubles" and w.league == "wta-doubles"
    for slug in ("iran-nuke-before-2027", "will-the-us-invade-iran-before-2027", "", None, "nfl-2026-10-20", "atp-doubles-x-y"):
        assert L("6", "c6", slug).game_key is None and L("6", "c6", slug).basis == "none"


def test_registered_sport_slug_that_is_not_a_game_gets_no_key():
    """A registered-sport slug whose tokens are not two team codes (nfl-afc-championship-2027-01-25-x) is
    not a game: no game key, so two such markets are no_game_key (refused), not same_game priced by the
    product fallback."""
    x = L("x", "cx", "nfl-afc-championship-2027-01-25-x")
    y = L("y", "cy", "nfl-afc-championship-2027-01-25-y")
    assert x.game_key is None and x.basis == "none" and x.teams == frozenset()
    assert classify_pair(x, y) == "no_game_key"
    cl = cluster([("x", "cx", "nfl-afc-championship-2027-01-25-x"), ("y", "cy", "nfl-afc-championship-2027-01-25-y")])
    assert not cl.certified_independent and "unpriceable_dependence" in cl.flags and len(cl.clusters) == 2
    assert combo_probability(cl, {"x": 0.5, "y": 0.5}, None).p is None
    # one unknown code is enough (nba-allstar-east-...), and a real game still keys
    assert L("z", "cz", "nba-allstar-east-2027-02-14").game_key is None
    assert L("w", "cw", "nba-gsw-lal-2026-10-21").game_key == "nba|2026-10-21|GSW@LAL"


def test_generic_basis_is_restricted_to_known_leagues():
    """Any three tokens plus a date match the generic shape; only league tokens in GENERIC_LEAGUES get a key."""
    a = L("a", "ca", "fed-rate-cut-2026-12-10-25bps")
    b = L("b", "cb", "fed-rate-cut-2026-12-10-50bps")
    assert a.game_key is None and a.basis == "none" and classify_pair(a, b) == "no_game_key"
    assert "fed" not in GENERIC_LEAGUES and "ucl" in GENERIC_LEAGUES and "nfl" in GENERIC_LEAGUES
    assert L("c", "cc", "ucl-psg-fcb-2026-10-20-total-3pt5").game_key == "ucl|2026-10-20|psg@fcb"


def test_exclusive_groups():
    a = L("7", "c7", "nfl-ind-jax-2026-11-01-exact-margin-long-7-12")
    b = L("8", "c8", "nfl-ind-jax-2026-11-01-exact-margin-short-1-6")
    assert a.group == b.group == "nfl|2026-11-01|IND@JAX|exact-margin"
    q = L("9", "c9", "nfl-ind-jax-2026-11-01-highest-scoring-quarter-q2")
    assert q.group == "nfl|2026-11-01|IND@JAX|highest-scoring-quarter" and q.group != a.group
    s1 = L("10", "c10", "ucl-psg-fcb-2026-10-20-exact-score-2-1")
    s2 = L("11", "c11", "ucl-psg-fcb-2026-10-20-exact-score-1-1")
    assert s1.group == s2.group == "ucl|2026-10-20|psg@fcb|exact-score"
    assert L("12", "c12", "ucl-psg-fcb-2026-10-20-halftime-result-draw").group.endswith("|halftime-result")
    assert L("13", "c13", "ucl-psg-fcb-2026-10-20-total-3pt5").group is None


# ----------------------------------------------------------- classify_pair
def test_every_class_in_order():
    base = "ucl-psg-fcb-2026-10-20"
    yes = L("p0", "cA", f"{base}-total-3pt5", 0)
    no = L("p1", "cA", f"{base}-total-3pt5", 1)
    assert classify_pair(yes, no) == "same_market" and classify_pair(yes, yes) == "same_market"
    es1 = L("p2", "cB", f"{base}-exact-score-2-1", 0)
    es2 = L("p3", "cC", f"{base}-exact-score-1-1", 0)
    assert classify_pair(es1, es2) == "same_group"
    assert classify_pair(es1, yes) == "same_game"                       # exclusive family vs another family of the game
    assert classify_pair(yes, L("p4", "cD", f"{base}-team-total-away-1pt5")) == "same_game"
    pol = L("p5", "cE", "iran-nuke-before-2027")
    assert classify_pair(yes, pol) == "no_game_key" and classify_pair(pol, L("p6", "cF", "will-the-us-invade-iran-before-2027")) == "no_game_key"
    assert classify_pair(yes, L("p7", "cG", "ucl-fcb-rma-2026-10-27")) == "shared_team_diff_game"
    assert classify_pair(yes, L("p8", "cH", "ucl-rma-juv-2026-10-27")) == "disjoint"
    # same code in different leagues never counts as a shared team
    assert classify_pair(L("p9", "cI", "nba-mia-cha-2026-10-24"), L("p10", "cJ", "nfl-mia-atl-2026-10-26")) == "disjoint"
    # same game key on different dates is a different game
    assert classify_pair(yes, L("p11", "cK", "ucl-psg-fcb-2026-11-20-total-3pt5")) == "shared_team_diff_game"
    # order: same condition wins over everything, even when slugs are missing
    assert classify_pair(L("p12", "cZ", None), L("p13", "cZ", None)) == "same_market"


# ------------------------------------------------------------------ cluster
def test_cluster_union_and_certification():
    rows = [("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5"), ("b", "c2", "ucl-psg-fcb-2026-10-20-team-total-away-1pt5"),
            ("c", "c3", "nba-gsw-lal-2026-10-21"), ("d", "c4", "atp-medjedo-nakashi-2026-10-19")]
    cl = cluster(rows)
    assert [sorted(c.position_ids) for c in cl.clusters] == [["a", "b"], ["c"], ["d"]]
    assert cl.clusters[0].game_key == "ucl|2026-10-20|psg@fcb" and cl.clusters[0].classes == {"same_game"}
    assert cl.certified_independent is False and cl.flags == ["needs_joint_engine"]
    assert cl.class_counts == {"same_game": 1, "disjoint": 5}
    ok = cluster(rows[2:])
    assert ok.certified_independent is True and ok.flags == [] and len(ok.clusters) == 2
    # transitive: a-b same game, b-c same game -> one cluster of three even if a-c were not directly compared first
    three = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5"), ("b", "c2", "ucl-psg-fcb-2026-10-20-total-2pt5"),
                     ("c", "c3", "ucl-psg-fcb-2026-10-20-exact-score-1-0")])
    assert len(three.clusters) == 1 and len(three.clusters[0]) == 3
    # dicts with outcome_index are accepted
    d = cluster([{"position_id": "x", "condition_id": "c", "slug": "nba-gsw-lal-2026-10-21", "outcome_index": 1}])
    assert d.legs[0].is_first_outcome is False and d.certified_independent


def test_no_game_key_is_never_certified():
    cl = cluster([("a", "c1", "iran-nuke-before-2027"), ("b", "c2", "will-the-us-invade-iran-before-2027")])
    assert cl.certified_independent is False and "unpriceable_dependence" in cl.flags
    assert len(cl.clusters) == 2                                  # not joined: we do not know they are related
    mixed = cluster([("a", "c1", "iran-nuke-before-2027"), ("b", "c2", "nba-gsw-lal-2026-10-21")])
    assert "unpriceable_dependence" in mixed.flags and not mixed.certified_independent
    single = cluster([("a", "c1", "iran-nuke-before-2027")])
    assert single.certified_independent is True                  # one leg alone is just its marginal


def test_shared_team_flag():
    cl = cluster([("a", "c1", "cs2-newvis-don-2026-10-20"), ("b", "c2", "cs2-newvis-xcity1-2026-10-20")])
    assert cl.certified_independent is True and cl.flags == ["shared_team"]
    assert cl.pair_classes[("a", "b")] == "shared_team_diff_game"


# -------------------------------------------------------- combo_probability
def test_frechet_bounds():
    assert frechet_bounds([0.6, 0.7]) == (pytest.approx(0.3), 0.6)
    assert frechet_bounds([0.2, 0.3]) == (0.0, 0.2)
    assert frechet_bounds([]) == (1.0, 1.0)


def test_combo_probability_independent_and_engine_paths():
    rows = [("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5"), ("b", "c2", "ucl-psg-fcb-2026-10-20-team-total-away-1pt5"),
            ("c", "c3", "nba-gsw-lal-2026-10-21")]
    cl = cluster(rows)
    p = {"a": 0.5, "b": 0.6, "c": 0.4}
    # no engine -> Fréchet-clamped product for the same-game cluster, flagged
    r = combo_probability(cl, p, None)
    assert r.p == pytest.approx(0.5 * 0.6 * 0.4) and r.independent_product == pytest.approx(0.12)
    assert (r.frechet_lower, r.frechet_upper) == (0.0, 0.4)
    assert "fallback:ucl|2026-10-20|psg@fcb" in r.flags and "needs_joint_engine" in r.flags
    assert [c.source for c in r.clusters] == ["fallback", "marginal"]
    # engine result replaces the product for that cluster only
    r2 = combo_probability(cl, p, lambda c: 0.45 if c.game_key and c.game_key.startswith("ucl") else None)
    assert r2.p == pytest.approx(0.45 * 0.4) and r2.clusters[0].source == "engine" and not [f for f in r2.flags if f.startswith("fallback")]
    # certified independent: plain product, no flags
    r3 = combo_probability(cluster(rows[2:]), {"c": 0.4}, lambda c: 99)
    assert r3.p == 0.4 and r3.flags == [] and r3.clusters[0].source == "marginal"
    # fallback is clamped to the cluster's Fréchet bounds (the plain product always lies inside, so equal here)
    hi = combo_probability(cluster(rows[:2]), {"a": 0.9, "b": 0.95}, None)
    assert hi.clusters[0].p == pytest.approx(min(0.9, max(0.85, 0.855)))


def test_combo_probability_impossible_and_unpriceable():
    same_mkt = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 0), ("b", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 1)])
    r = combo_probability(same_mkt, {"a": 0.5, "b": 0.5}, lambda c: 0.99)
    assert r.p == 0.0 and r.clusters[0].source == "impossible"
    twice = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 0), ("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 0)])
    assert combo_probability(twice, {"a": 0.5}, None).p == 0.5
    # a duplicated leg beside a third same-game leg is not "impossible": dedupe by position first, then the engine
    dup3 = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 0), ("a", "c1", "ucl-psg-fcb-2026-10-20-total-3pt5", 0),
                    ("c", "c3", "ucl-psg-fcb-2026-10-20-team-total-away-1pt5", 0)])
    seen = []
    r3 = combo_probability(dup3, {"a": 0.5, "c": 0.6}, lambda c: seen.append(sorted(c.position_ids)) or 0.25)
    assert r3.p == 0.25 and r3.clusters[0].source == "engine" and seen == [["a", "c"]] and r3.independent_product == pytest.approx(0.3)
    assert combo_probability(dup3, {"a": 0.5, "c": 0.6}, None).p == pytest.approx(min(0.5, 0.3))
    # two same-game legs with no catalog condition_id (None) are not "same market": None never counts as equal
    nocond = cluster([("a", None, "ucl-psg-fcb-2026-10-20-total-3pt5", 0), ("b", None, "ucl-psg-fcb-2026-10-20-team-total-away-1pt5", 0)])
    assert nocond.pair_classes[("a", "b")] == "same_game"
    rn = combo_probability(nocond, {"a": 0.5, "b": 0.6}, lambda c: 0.4)
    assert rn.p == 0.4 and rn.clusters[0].source == "engine"
    grp = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-exact-score-2-1", 0), ("b", "c2", "ucl-psg-fcb-2026-10-20-exact-score-1-1", 0)])
    assert combo_probability(grp, {"a": 0.1, "b": 0.1}, lambda c: 0.5).p == 0.0
    grp_no = cluster([("a", "c1", "ucl-psg-fcb-2026-10-20-exact-score-2-1", 0), ("b", "c2", "ucl-psg-fcb-2026-10-20-exact-score-1-1", 1)])
    assert combo_probability(grp_no, {"a": 0.1, "b": 0.9}, lambda c: 0.1).clusters[0].source == "engine"   # YES + NO: engine decides
    pol = cluster([("a", "c1", "iran-nuke-before-2027"), ("b", "c2", "will-the-us-invade-iran-before-2027")])
    u = combo_probability(pol, {"a": 0.3, "b": 0.2}, None)
    assert u.p is None and "unpriceable_dependence" in u.flags and u.independent_product == pytest.approx(0.06)
    miss = combo_probability(cluster([("a", "c1", "nba-gsw-lal-2026-10-21")]), {}, None)
    assert miss.p is None and any(f.startswith("missing_marginals:a") for f in miss.flags)


# ----------------------------------------------------------- regression
def test_live_sample_class_shares():
    """Ground truth measured by the planning session on the 75 s sample, per distinct two-leg combo:
    disjoint 97.2%, same_game 2.6%, no_game_key 0.1%, shared_team 0.1% (within a point)."""
    legs = json.load(gzip.open(FIX / "clusters_75s_legs.json.gz", "rt"))
    refs = {pid: leg_ref(pid, v[0] if v else None, v[1] if v else None) for pid, v in legs.items()}
    # every generic-basis league token in the sample is allowlisted, and every keyed generic leg uses one
    generic = {r.league for r in refs.values() if r.basis == "generic"}
    assert generic and generic <= GENERIC_LEAGUES
    shaped = {r.slug.split("-")[0] for r in refs.values() if r.slug and r.basis == "none" and re.match(r"^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}", r.slug)}
    assert not shaped - {"nfl", "nba"}      # the only keyless game-shaped slugs are registered-sport non-games
    per_combo: Counter = Counter()
    seen: set[str] = set()
    n_req = 0
    with gzip.open(SAMPLE, "rt") as f:
        for line in f:
            pl = json.loads(json.loads(line)["p"])
            if pl.get("type") != "RFQ_REQUEST" or len(pl["leg_position_ids"]) != 2:
                continue
            n_req += 1
            if pl["condition_id"] in seen:
                continue
            seen.add(pl["condition_id"])
            a, b = pl["leg_position_ids"]
            per_combo[classify_pair(refs[a], refs[b])] += 1
    tot = sum(per_combo.values())
    assert n_req > 5000 and tot > 3000
    share = {k: 100 * v / tot for k, v in per_combo.items()}
    assert share["disjoint"] == pytest.approx(97.2, abs=1.0)
    assert share["same_game"] == pytest.approx(2.6, abs=1.0)
    assert share["no_game_key"] == pytest.approx(0.1, abs=1.0)
    assert share["shared_team_diff_game"] == pytest.approx(0.1, abs=1.0)
    assert "same_market" not in share and "same_group" not in share
