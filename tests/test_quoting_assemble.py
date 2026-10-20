import json

import pytest

from rfqbot.marginals import Marginal
from rfqbot.messages import RequestedSize, RfqRequest
from rfqbot.pricing.joint import build_game
from rfqbot.pricing.joint_nfl import EngineConfig
from rfqbot.pricing.synthetic import make_truth, quotes_from_truth
from rfqbot.quoting import Assembler, QuoteConfig
from rfqbot.quoting.cache import GameCache, GameEntry
from rfqbot.quoting.config import load_quote_config
from rfqbot.quoting.decisions import DecisionLog
from rfqbot.sports import registry

GAME = "nfl-dal-phi-2026-10-27"
NBA = "nba-gsw-lal-2026-10-21"


def marginal(pid, p, err=0.01, tier="ok", flags=(), conf="none"):
    return Marginal(position_id=pid, p=p, err=err, p_poly=p, p_kalshi=None, w_poly=1.0, w_kalshi=0.0,
                    kalshi_confidence=conf, flags=tuple(flags), tier=tier, at_ms=1, explain="")


@pytest.fixture(scope="module")
def nfl_entry():
    quotes = quotes_from_truth(make_truth(), game=GAME)
    model = build_game(quotes, config=EngineConfig(tie_prob=None))
    sp = registry.get("nfl")
    e = GameEntry(prefix=GAME, sport="nfl", quotes=quotes, model=model, model_built_at=1.0, refreshed_at_ms=1)
    for q in quotes:
        p = (q.best_bid + q.best_ask) / 2
        e.marginals[q.position_id] = (q, None, marginal(q.position_id, p, 0.01))
        e.specs[q.position_id] = sp.parse_poly(q.slug, q.title, q.outcome, q.outcome_index, position_id=q.position_id)
    return e


class FakeStore:
    def __init__(self, entries):
        self.rows = {}
        for e in entries:
            for q in e.quotes:
                self.rows[q.position_id] = {"position_id": q.position_id, "condition_id": "c" + q.slug + str(q.outcome_index // 2), "slug": q.slug, "outcome": q.outcome}

    def add(self, pid, slug, outcome, cond):
        self.rows[pid] = {"position_id": pid, "condition_id": cond, "slug": slug, "outcome": outcome}

    def get_market(self, pid):
        return self.rows.get(pid)


def nba_entry():
    from rfqbot.pricing.quotes import Quote
    qs = [Quote("n1", "t1", f"{NBA}-spread-home-6pt5", "Spread: Lakers (-6.5)", "Lakers", 0, 0.48, 0.50, 500, 500, at_ms=1),
          Quote("n2", "t2", f"{NBA}-spread-home-6pt5", "Spread: Lakers (-6.5)", "Warriors", 1, 0.50, 0.52, 500, 500, at_ms=1),
          Quote("n3", "t3", f"{NBA}-total-230pt5", "Warriors vs. Lakers: O/U 230.5", "Over", 0, 0.45, 0.47, 500, 500, at_ms=1)]
    e = GameEntry(prefix=NBA, sport="nba", quotes=qs, refreshed_at_ms=1)
    for q in qs:
        e.marginals[q.position_id] = (q, None, marginal(q.position_id, (q.best_bid + q.best_ask) / 2, 0.01))
    return e


@pytest.fixture
def world(nfl_entry):
    cache = GameCache(QuoteConfig(), build_models=False)
    cache.entries[GAME] = nfl_entry
    cache.entries[NBA] = nba_entry()
    store = FakeStore([nfl_entry, cache.entries[NBA]])
    asm = Assembler(cache, QuoteConfig(), decisions=DecisionLog(None), now_ms=lambda: 1_000_000)
    return cache, store, asm


def rfq(legs, direction="BUY", unit="notional", value=100_000_000, rid="r1", req="req_a", cond="cond1"):
    return RfqRequest(type="RFQ_REQUEST", rfq_id=rid, requestor_public_id=req, leg_position_ids=tuple(legs), condition_id=cond,
                      yes_position_id="y", no_position_id="n", direction=direction, side="YES",
                      requested_size=RequestedSize(unit=unit, value_e6=value), submission_deadline_ms=0)


def leg(entry, slug_suffix, outcome):
    return next(q.position_id for q in entry.quotes if q.slug == f"{entry.prefix}{slug_suffix}" and q.outcome == outcome)


def test_single_leg_buy_and_sell_prices(world, nfl_entry):
    cache, store, asm = world
    pid = leg(nfl_entry, "-total-44pt5", "Over")
    p = nfl_entry.marginals[pid][2].p
    q = asm.handle(rfq([pid]), store)
    assert q is not None and q.price_e6 / 1e6 >= p + 0.005         # maker sells YES above fair
    d = asm.decisions.read("r1")[-1]
    assert d["decision"] == "quoted" and d["maker_action"] == "sell_yes" and d["certified"] == 1
    assert abs(d["p_fair"] - p) < 1e-9 and abs(d["margin"] - (0.01 + 0.005)) < 1e-9 and d["u_d"] == 0
    assert d["size_e6"] * d["price_e6"] // 1_000_000 <= 100_000_000   # within the notional budget
    s = asm.handle(rfq([pid], "SELL", "shares", 40_000_000, rid="r2"), store)
    assert s is not None and s.price_e6 / 1e6 <= p - 0.005 and s.size_e6 == 40_000_000
    legs = json.loads(asm.decisions.read("r2")[-1]["legs_json"])
    assert legs[0]["key"].startswith("nfl|2026-10-27|DAL@PHI|FG|total")


def test_certified_product_and_uncertainty(world, nfl_entry):
    cache, store, asm = world
    a = leg(nfl_entry, "-total-44pt5", "Over")
    q = asm.handle(rfq([a, "n3"], rid="r3"), store)
    d = asm.decisions.read("r3")[-1]
    pa, pb = nfl_entry.marginals[a][2].p, 0.46
    assert d["certified"] == 1 and abs(d["p_fair"] - pa * pb) < 1e-9
    assert abs(d["u_m"] - pa * pb * ((0.01 / pa) ** 2 + (0.01 / pb) ** 2) ** 0.5) < 1e-9
    assert q.price_e6 / 1e6 >= d["p_fair"] and "indep 2" in d["reason"]


def test_same_game_cluster_uses_joint(world, nfl_entry):
    cache, store, asm = world
    a = leg(nfl_entry, "-spread-home-3pt5", "Eagles")
    b = leg(nfl_entry, "-total-44pt5", "Over")
    q = asm.handle(rfq([a, b], rid="r4"), store)
    d = asm.decisions.read("r4")[-1]
    cl = json.loads(d["clusters_json"])
    assert d["decision"] == "quoted" and len(cl) == 1 and cl[0]["source"] == "joint" and d["certified"] == 0
    jq = nfl_entry.model.joint([nfl_entry.specs[a], nfl_entry.specs[b]])
    assert abs(d["p_fair"] - jq.p_cal) < 1e-9 and d["u_d"] > 0 and abs(d["u_d"] - (jq.hi - jq.lo) / 2) < 1e-9
    assert d["model_built_at"] == 1.0 and "1 joint" in d["reason"]
    # opposite sides of one market: impossible, still priced (sell YES at the margin)
    over, under = b, leg(nfl_entry, "-total-44pt5", "Under")
    asm.handle(rfq([over, under], rid="r5"), store)
    d5 = asm.decisions.read("r5")[-1]
    assert d5["p_fair"] == 0 and "[impossible]" in d5["reason"] and d5["price_e6"] == 5_000


def test_skip_reasons(world, nfl_entry):
    cache, store, asm = world
    a = leg(nfl_entry, "-total-44pt5", "Over")
    asm.handle(rfq(["zzz"], rid="s1"), store)
    assert asm.decisions.read("s1")[-1]["decision"] == "skipped:leg:unknown_market"
    store.add("pol1", "iran-president-2026", "Yes", "cpol")
    asm.handle(rfq([a, "pol1"], rid="s2"), store)
    assert asm.decisions.read("s2")[-1]["decision"] == "skipped:unpriceable_dependence"
    store.add("tt1", "atp-doubles-ab-cd-2026-10-21-winner", "Yes", "ctt")
    asm.handle(rfq([tt := "tt1"], rid="s3"), store)
    assert asm.decisions.read("s3")[-1]["decision"] == "skipped:leg:sport_not_quoted"
    # NBA same-game pair: no engine
    asm.handle(rfq(["n1", "n3"], rid="s4"), store)
    assert asm.decisions.read("s4")[-1]["decision"] == "skipped:unsupported:nba_same_game"
    # leg with tier skip / p None
    bad = leg(nfl_entry, "-total-46pt5", "Over")
    q, m, _ = nfl_entry.marginals[bad]
    nfl_entry.marginals[bad] = (q, m, marginal(bad, None, None, "skip", ("no_quote",)))
    asm.handle(rfq([bad], rid="s5"), store)
    assert asm.decisions.read("s5")[-1]["decision"] == "skipped:leg:no_quote"
    nfl_entry.marginals[bad] = (q, m, marginal(bad, 0.5, 0.2, "skip", ("disagree_hard",)))
    asm.handle(rfq([bad], rid="s6"), store)
    assert asm.decisions.read("s6")[-1]["decision"] == "skipped:leg:disagree_hard"
    # cold game: tracked, not quoted
    store.add("cold1", "nfl-kc-sea-2026-10-26", "Chiefs", "ckc")
    asm.handle(rfq(["cold1"], rid="s7"), store)
    assert asm.decisions.read("s7")[-1]["decision"] == "skipped:leg:cold_game" and "nfl-kc-sea-2026-10-26" in cache._pending
    # unsupported family inside a same-game cluster
    store.add("hsq", f"{GAME}-highest-scoring-quarter-q1", "Yes", "chsq")
    nfl_entry.marginals["hsq"] = (None, None, marginal("hsq", 0.2))
    nfl_entry.specs["hsq"] = registry.get("nfl").parse_poly(f"{GAME}-highest-scoring-quarter-q1", "Cowboys vs. Eagles: Highest Scoring Quarter - Q1", "Yes", 0)
    asm.handle(rfq([a, "hsq"], rid="s8"), store)
    assert asm.decisions.read("s8")[-1]["decision"].startswith("skipped:unsupported:highest_scoring_quarter")
    # price above the BUY safety threshold
    hi = leg(nfl_entry, "-spread-home-20pt5", "Cowboys")    # p ~ 0.98
    asm.handle(rfq([hi], rid="s9"), store)
    assert asm.decisions.read("s9")[-1]["decision"] == "skipped:price_above_safety"
    # SELL on a near-zero leg: bid would be below the minimum
    lo = leg(nfl_entry, "-1h-spread-home-21pt5", "Eagles")        # p ~ 0.01
    asm.handle(rfq([lo], "SELL", "shares", 100_000_000, rid="s10"), store)
    assert asm.decisions.read("s10")[-1]["decision"] == "skipped:price_below_min"
    # unsupported RFQ shape
    asm.handle(rfq([a], "BUY", "shares", 1_000_000, rid="s11"), store)
    assert asm.decisions.read("s11")[-1]["decision"] == "skipped:unsupported_rfq"
    assert asm.stats()["seen"] == 11


def test_size_caps(world, nfl_entry):
    cache, store, asm = world
    a = leg(nfl_entry, "-total-44pt5", "Over")
    q = asm.handle(rfq([a], value=10_000_000_000, rid="c1"), store)      # $10k requested
    assert q.size_e6 * q.price_e6 / 1e12 <= 500.0 + 1e-6                 # QUOTE_MAX_SIZE_USD
    qq, m, _ = nfl_entry.marginals[a]
    nfl_entry.marginals[a] = (qq, m, marginal(a, m.p if m else 0.5, 0.01, "stale", ("stale:poly",)))
    q2 = asm.handle(rfq([a], value=10_000_000_000, rid="c2"), store)
    assert q2.size_e6 * q2.price_e6 / 1e12 <= 500.0 * 0.25 + 1e-6        # stale tier x0.25
    nfl_entry.marginals[a] = (qq, m, _)
    legs4 = [a, leg(nfl_entry, "-1h-total-22pt5", "Over"), "n3", leg(nfl_entry, "-team-total-phi-23pt5", "Over")]
    # 4 legs of which 3 same game -> joint cluster + nba leg; cap x0.25 for 4 legs
    q4 = asm.handle(rfq(legs4, value=10_000_000_000, rid="c4"), store)
    d4 = asm.decisions.read("c4")[-1]
    if q4 is not None:
        assert q4.size_e6 * q4.price_e6 / 1e12 <= 500.0 * 0.25 + 1e-6
    asm.handle(rfq([a], value=1_000_000, rid="c5"), store)                # $1 requested < min $5
    assert asm.decisions.read("c5")[-1]["decision"] == "skipped:size_below_min"


def test_round_trip_flag_and_decision_round_trip(world, nfl_entry, tmp_path):
    cache, store, asm = world
    a = leg(nfl_entry, "-total-44pt5", "Over")
    asm.handle(rfq([a], rid="t1", cond="cx"), store)
    asm.handle(rfq([a], "SELL", "shares", 50_000_000, rid="t2", cond="cx"), store)
    assert "[round_trip]" in asm.decisions.read("t2")[-1]["reason"]
    log = DecisionLog(tmp_path / "d.db")
    asm2 = Assembler(cache, QuoteConfig(), decisions=log, now_ms=lambda: 5)
    asm2.handle(rfq([a], rid="t3"), store)
    row = log.read("t3")[-1]
    assert row["at_ms"] == 5 and json.loads(row["config_json"])["base_edge"] == 0.005 and row["latency_ms"] >= 0
    assert log.counts() == [("quoted", 1)]
    log.close()


def test_config_from_env():
    cfg = load_quote_config(None, env={"QUOTE_BASE_EDGE": "0.03", "QUOTE_TIER_SIZE_MULT": "ok=1,wide=0.2", "QUOTE_LEGS_SIZE_MULT": "1=1,2=0.5"})
    assert cfg.base_edge == 0.03 and cfg.tier_size_mult["wide"] == 0.2 and cfg.legs_mult(2) == 0.5 and cfg.legs_mult(9) == 0.25
