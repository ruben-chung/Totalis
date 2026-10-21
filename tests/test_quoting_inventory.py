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
from rfqbot.quoting.inventory import Inventory, kelly_size
from rfqbot.risk.caps import Caps
from rfqbot.sports import registry
from rfqbot.store import Store

GAME = "nfl-dal-phi-2026-10-27"
NOW = 1_000_000


def marginal(pid, p, err=0.01, tier="ok"):
    return Marginal(position_id=pid, p=p, err=err, p_poly=p, p_kalshi=None, w_poly=1.0, w_kalshi=0.0,
                    kalshi_confidence="none", flags=(), tier=tier, at_ms=1, explain="")


@pytest.fixture(scope="module")
def nfl_entry():
    quotes = quotes_from_truth(make_truth(), game=GAME)
    model = build_game(quotes, config=EngineConfig(tie_prob=None, n_scenarios=20_000))
    sp = registry.get("nfl")
    e = GameEntry(prefix=GAME, sport="nfl", quotes=quotes, model=model, model_built_at=1.0, refreshed_at_ms=1)
    for q in quotes:
        e.marginals[q.position_id] = (q, None, marginal(q.position_id, (q.best_bid + q.best_ask) / 2))
        e.specs[q.position_id] = sp.parse_poly(q.slug, q.title, q.outcome, q.outcome_index, position_id=q.position_id)
    return e


class FakeStore:
    def __init__(self, entries):
        self.rows = {}
        for e in entries:
            for q in e.quotes:
                self.rows[q.position_id] = {"position_id": q.position_id, "condition_id": "c" + q.slug, "slug": q.slug, "outcome": q.outcome}

    def get_market(self, pid):
        return self.rows.get(pid)


def rfq(legs, direction="BUY", unit="notional", value=100_000_000, rid="r1", req="req_a", cond="cond1", deadline=NOW + 400):
    return RfqRequest(type="RFQ_REQUEST", rfq_id=rid, requestor_public_id=req, leg_position_ids=tuple(legs), condition_id=cond,
                      yes_position_id="y", no_position_id="n", direction=direction, side="YES",
                      requested_size=RequestedSize(unit=unit, value_e6=value), submission_deadline_ms=deadline)


def leg(entry, slug_suffix, outcome):
    return next(q.position_id for q in entry.quotes if q.slug == f"{entry.prefix}{slug_suffix}" and q.outcome == outcome)


def make_world(nfl_entry, caps=None, cfg=None, decisions=None, store_path=None):
    cfg = cfg or QuoteConfig(max_size_usd=1e9)
    cache = GameCache(cfg, build_models=False)
    cache.entries[GAME] = nfl_entry
    caps = caps or Caps(capital_usd=50_000.0)
    inv = Inventory(cache, cfg, caps, store_path=store_path, now_ms=lambda: NOW, horizon_ms=31_000)
    cache.on_refresh.append(inv.refresh)
    asm = Assembler(cache, cfg, decisions=decisions or DecisionLog(None), now_ms=lambda: NOW, inventory=inv, horizon_ms=31_000)
    return cache, FakeStore([nfl_entry]), asm, inv


def test_kelly_size():
    assert kelly_size(0.015, 0.375, 50_000, 0.25) == pytest.approx(0.25 * 50_000 * 0.015 / (0.375 * 0.625))
    assert kelly_size(0.0, 0.5, 50_000, 0.25) == 0.0
    assert kelly_size(0.01, 0.0, 50_000, 0.25) == 0.0


def test_repeated_combo_is_cut_then_skipped_by_the_outcome_cap(nfl_entry):
    cache, store, asm, inv = make_world(nfl_entry)
    legs = [leg(nfl_entry, "-spread-home-3pt5", "Cowboys"), leg(nfl_entry, "-total-44pt5", "Over")]
    sizes, decisions = [], []
    for i in range(12):
        d = asm.handle(rfq(legs, value=5_000_000_000, rid=f"r{i}", req="req_a" if i % 2 else "req_b"), store)
        dec = asm.decisions.read(f"r{i}")[0]
        decisions.append(dec["decision"])
        sizes.append(dec["size_e6"] or 0)
    assert decisions[0] == "quoted" and sizes[0] > 0
    first = asm.decisions.read("r0")[0]
    assert first["allowed_size"] is not None and first["binding_cap"] in ("outcome", "game", "es1", "kelly", None)
    assert first["expires_ms"] == NOW + 400 + 31_000 and first["deadline_ms"] == NOW + 400
    assert first["pending_same_combo"] == 0 and json.loads(first["headroom_json"])["outcome"] is not None
    assert json.loads(first["book_json"])["positions"] == 0
    # the same combo keeps coming: pending exposure is counted immediately, the cap bites, then kills it
    assert any(d.startswith("skipped:cap:") for d in decisions), decisions
    k = decisions.index(next(d for d in decisions if d.startswith("skipped:cap:")))
    assert all(d == "quoted" for d in decisions[:k])
    assert all(d.startswith("skipped:cap:") for d in decisions[k:])
    last_q = asm.decisions.read(f"r{k - 1}")[0]
    n_req = len({"req_a" if i % 2 else "req_b" for i in range(k - 1)})
    assert last_q["binding_cap"] is not None and last_q["pending_same_combo"] == k - 1 and last_q["pending_requesters"] == n_req
    killed = asm.decisions.read(f"r{k}")[0]
    assert killed["decision"] == f"skipped:cap:{killed['binding_cap']}" and "headroom" in killed["reason"]
    # total short exposure to the combo's outcomes stays inside the outcome cap ($2,500 at 5%)
    bk = inv.book_summary()
    assert bk["positions"] == k and bk["pending"] == k
    deltas = {d.label: d.delta for d in inv.state.canonical_deltas(GAME)}
    assert all(abs(v) <= 2_500 + 1 for v in deltas.values())
    s = inv.summary()
    assert s["stats"]["quoted"] == k and s["assess_p50_ms"] < 10


def test_skew_widens_adders_and_tightens_hedges_never_below_fair(nfl_entry):
    cache, store, asm, inv = make_world(nfl_entry)
    ml = [leg(nfl_entry, "", "Cowboys")]
    # build a short book in DAL moneyline (requesters BUY -> we sell)
    for i in range(3):
        asm.handle(rfq(ml, value=300_000_000, rid=f"s{i}"), store)
    base = asm.decisions.read("s0")[0]
    later = asm.decisions.read("s2")[0]
    assert base["e_i"] == 0.0                      # empty book: no skew
    assert later["e_i"] > 0 and later["d_es1_per_share"] > 0       # adds to the tail: wider
    assert later["price_e6"] >= base["price_e6"]
    # the hedge (requester SELLs -> we buy DAL) tightens, but the bid never exceeds fair
    d = asm.evaluate(rfq(ml, direction="SELL", unit="shares", value=100_000_000, rid="h"), store)
    assert d.quoted and d.e_i < 0 and d.d_es1_per_share < 0
    assert d.margin >= 0 and d.price_e6 / 1e6 <= d.p_fair + 1e-9
    assert "inv=-" in d.reason


def test_refresh_expires_fills_and_rebuilds(nfl_entry, tmp_path):
    store_db = tmp_path / "rfq.db"
    st = Store(store_db)
    cache, fake, asm, inv = make_world(nfl_entry, store_path=store_db)
    ml = [leg(nfl_entry, "", "Cowboys")]
    for rid, deadline in (("a", NOW + 400), ("b", NOW + 400), ("c", NOW + 400)):
        st.insert_rfq({"rfq_id": rid, "requester_id": None, "combo_id": None, "direction": "BUY", "side": "YES",
                       "size_unit": "notional", "size_value_e6": 1, "submission_deadline_ms": deadline, "first_seen_ms": NOW,
                       "status": "QUOTED", "status_at_ms": NOW, "stub": 0})
        asm.handle(rfq(ml, value=200_000_000, rid=rid, deadline=deadline), fake)
    assert inv.book_summary()["pending"] == 3
    st.set_rfq_status("a", "EXPIRED", NOW + 40_000)
    st.insert_execution("b", None, "CONFIRMED", "0x1", NOW + 5_000)
    st.insert_trade({"rfq_id": "b", "requester_id": None, "combo_id": None, "direction": "BUY", "side": "YES",
                     "price_e6": 400_000, "size_e6": 150_000_000, "executed_at_ms": NOW + 5_000, "received_at_ms": NOW + 5_000,
                     "is_ours": 1, "raw_message_id": None})
    assert inv.refresh(NOW + 10_000, force=True)
    bk = inv.book
    assert bk.positions["a"].status == "expired" and bk.positions["b"].status == "filled"
    assert bk.positions["b"].size == pytest.approx(150.0) and bk.positions["b"].price == pytest.approx(0.4)
    assert bk.positions["c"].status == "pending"
    assert inv.refresh(NOW + 11_000) is False                # not due yet (risk_refresh_sec)
    # c expires by the listener's rule: deadline + horizon
    inv.refresh(NOW + 400 + 31_000 + 1, force=True)
    assert bk.positions["c"].status == "expired" and inv.stats["expired"] == 2 and inv.stats["filled"] == 1
    assert inv.book_summary()["positions"] == 1
    # rebuild after a restart from the decisions log + store: only the live quote comes back
    log_path = tmp_path / "dec.db"
    asm2_dec = DecisionLog(log_path)
    for rid in ("a", "b", "c"):
        row = asm.decisions.read(rid)[0]
        asm2_dec.write(dict(row))
    asm2_dec.close()
    cache2, _, _, inv2 = make_world(nfl_entry, store_path=store_db)
    n = inv2.load(log_path, now_ms=NOW + 10_000)
    assert n == 3 and inv2.book.positions["a"].status == "expired" and inv2.book.positions["b"].status == "filled"
    assert inv2.book.positions["c"].status == "pending" and inv2.book_summary()["positions"] == 2
    st.close()


def test_risk_off_keeps_stage_two_behaviour(nfl_entry):
    cfg = QuoteConfig(risk=False, max_size_usd=1e9)
    cache, store, asm, inv = make_world(nfl_entry, cfg=cfg)
    ml = [leg(nfl_entry, "", "Cowboys")]
    for i in range(5):
        d = asm.handle(rfq(ml, value=5_000_000_000, rid=f"r{i}"), store)
        assert d is not None
    row = asm.decisions.read("r4")[0]
    assert row["allowed_size"] is None and row["e_i"] == 0.0 and row["binding_cap"] is None
    assert inv.book_summary()["positions"] == 0
    assert load_quote_config(env={"QUOTE_RISK": "0", "QUOTE_K_INV": "0.1", "QUOTE_KELLY_FRACTION": "0.5"}).risk is False
    c = load_quote_config(env={"QUOTE_K_INV": "0.1", "QUOTE_KELLY_FRACTION": "0.5"})
    assert c.risk and c.k_inv == 0.1 and c.kelly_fraction == 0.5


def test_kelly_cuts_size_and_is_logged(nfl_entry):
    cache, store, asm, inv = make_world(nfl_entry, caps=Caps(capital_usd=1_000.0, max_outcome_delta=10, max_game_worst_loss=10,
                                                             max_portfolio_es1=10))
    ml = [leg(nfl_entry, "", "Cowboys")]
    d = asm.evaluate(rfq(ml, value=5_000_000_000, rid="k"), store)
    assert d.quoted and d.kelly_size == pytest.approx(kelly_size(d.margin, d.p_fair, 1_000.0, 0.25))
    assert d.size_e6 / 1e6 <= d.kelly_size + 1e-3 and "cut:kelly" in d.reason


def test_decisions_log_migrates_old_schema(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, rfq_id TEXT NOT NULL, at_ms INTEGER NOT NULL, n_legs INTEGER NOT NULL,"
              " legs_json TEXT NOT NULL, clusters_json TEXT NOT NULL, decision TEXT NOT NULL, direction TEXT, side TEXT,"
              " requested_unit TEXT, requested_e6 INTEGER, certified INTEGER, p_fair REAL, u_m REAL, u_d REAL, e_b REAL, e_i REAL,"
              " margin REAL, maker_action TEXT, price_e6 INTEGER, size_e6 INTEGER, reason TEXT, latency_ms REAL, model_built_at REAL, config_json TEXT)")
    c.execute("INSERT INTO decisions(rfq_id, at_ms, n_legs, legs_json, clusters_json, decision) VALUES ('x', 1, 1, '[]', '[]', 'quoted')")
    c.commit(); c.close()
    log = DecisionLog(path)
    cols = {r[1] for r in log.conn.execute("PRAGMA table_info(decisions)")}
    assert {"expires_ms", "allowed_size", "binding_cap", "book_json"} <= cols
    assert log.read("x")[0]["expires_ms"] is None
    log.close()
