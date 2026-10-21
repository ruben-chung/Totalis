import json

import pytest

from rfqbot.quoting.decisions import DecisionLog
from rfqbot.risk.book import EXPIRED, FILLED, PENDING, SETTLED, Book, Position, PositionLeg
from rfqbot.store import Store

KEY_DAL = "nfl|2026-10-27|DAL@PHI|FG|moneyline|-|-|DAL"
KEY_TOT = "nfl|2026-10-27|DAL@PHI|FG|total|-|44.5|over"


def decision(rid, at_ms, legs, clusters, action="sell_yes", price=400_000, size=10_000_000, decision="quoted"):
    return {"rfq_id": rid, "at_ms": at_ms, "direction": "BUY" if action == "sell_yes" else "SELL", "side": "YES",
            "requested_unit": "notional", "requested_e6": 1, "n_legs": len(legs), "legs_json": json.dumps(legs),
            "clusters_json": json.dumps(clusters), "certified": 1, "p_fair": 0.3, "u_m": 0, "u_d": 0, "e_b": 0.005,
            "e_i": 0, "margin": 0.005, "maker_action": action, "price_e6": price, "size_e6": size, "decision": decision,
            "reason": "", "latency_ms": 0.1, "model_built_at": None, "config_json": "{}"}


def leg(pid, key, p, game, slug="nfl-dal-phi-2026-10-27"):
    return {"position_id": pid, "slug": slug, "outcome": "Cowboys", "game_key": game, "p": p, "err": 0.01, "tier": "ok",
            "flags": [], "kalshi_confidence": "none", "key": key}


def test_position_basics():
    p = Position("r", [PositionLeg("1", KEY_DAL, None, 0.6, "nfl-dal-phi-2026-10-27")], -1, 10.0, 0.4)
    assert p.sport == "nfl" and p.games == ["nfl-dal-phi-2026-10-27"] and p.game_dates == ["2026-10-27"]
    assert p.max_loss_per_share == pytest.approx(0.6) and p.notional == pytest.approx(6.0)
    assert p.pnl(True) == pytest.approx(-6.0) and p.pnl(False) == pytest.approx(4.0)
    with pytest.raises(ValueError):
        Position("r", [], 0, 1.0, 0.5)
    with pytest.raises(ValueError):
        Position("r", [], 1, 1.0, 1.5)


def test_from_decisions_latest_row_per_rfq_and_assume_filled(tmp_path):
    db = tmp_path / "d.db"
    log = DecisionLog(db)
    l1, l2 = leg("p1", KEY_DAL, 0.6, "nfl-dal-phi-2026-10-27"), leg("p2", KEY_TOT, 0.5, "nfl-dal-phi-2026-10-27")
    cl = [{"game_key": "nfl|2026-10-27|DAL@PHI", "position_ids": ["p1", "p2"], "pair_class": "same_game", "p": 0.33,
           "lo": 0.3, "hi": 0.36, "source": "joint", "notes": []}]
    log.write(decision("r1", 1000, [l1, l2], cl, price=300_000, size=5_000_000))
    log.write(decision("r1", 2000, [l1, l2], cl, price=320_000, size=7_000_000))          # requote: latest wins
    log.write(decision("r2", 3000, [l1], cl[:0], action="buy_yes", price=580_000, size=2_000_000))
    log.write(decision("r3", 4000, [l1], [], decision="skipped:size_below_min", price=None, size=None))
    log.close()

    book = Book.from_decisions(db, assume_filled=True)
    assert set(book.positions) == {"r1", "r2"}
    r1 = book.positions["r1"]
    assert r1.status == FILLED and r1.sign == -1 and r1.size == 7.0 and r1.price == 0.32
    assert [l.key for l in r1.legs] == [KEY_DAL, KEY_TOT]
    assert r1.legs[0].spec is not None and r1.legs[0].spec.rules.get("tie") == "50-50"     # Poly NFL moneyline rule
    assert r1.legs[1].spec.line == 44.5 and r1.legs[1].spec.side == "over"
    assert r1.clusters == [("nfl|2026-10-27|DAL@PHI", ("p1", "p2"), 0.33)]
    r2 = book.positions["r2"]
    assert r2.sign == 1 and r2.notional == pytest.approx(2 * 0.58)
    s = book.summary()
    assert s["active"] == 2 and s["by_sport"] == {"nfl": 2} and s["by_sign"] == {"long": 1, "short": 1}

    # pending only: a quote is live for 60 s after its decision; at now=62 s only r2 (decided at 3 s) is
    book2 = Book.from_decisions(db, assume_filled=False, now_ms=62_000)
    assert set(book2.positions) == {"r2"} and book2.positions["r2"].status == PENDING
    assert book2.positions["r2"].expires_ms == 63_000
    assert book2.expire(70_000) == ["r2"] and book2.positions["r2"].status == EXPIRED and book2.active() == []

    # time window
    assert set(Book.from_decisions(db, since_ms=2500).positions) == {"r2"}


def test_from_decisions_uses_catalog_for_specs(tmp_path):
    rfq_db = tmp_path / "rfq.db"
    st = Store(rfq_db)
    st.upsert_markets([{"position_id": "p1", "market_id": "m", "condition_id": "cond1", "slug": "nfl-dal-phi-2026-10-27",
                        "title": "Cowboys vs. Eagles", "outcome": "Cowboys", "outcome_index": 0, "outcome_price": 0.6,
                        "volume": 0, "tags": "", "synced_at_ms": 1}])
    st.close()
    db = tmp_path / "d.db"
    log = DecisionLog(db)
    log.write(decision("r1", 1000, [leg("p1", None, 0.6, "nfl-dal-phi-2026-10-27")], []))
    log.close()
    book = Book.from_decisions(db, rfq_db=rfq_db)
    l = book.positions["r1"].legs[0]
    assert l.key == KEY_DAL and l.condition_id == "cond1" and l.outcome == "Cowboys" and l.spec.side == "DAL"
    # settle: Cowboys won -> we sold YES at 0.4 on 10 shares: -6
    assert book.settle({"cond1": "Cowboys"}) == ["r1"]
    assert book.positions["r1"].status == SETTLED and book.positions["r1"].realized == pytest.approx(-6.0)
    assert book.active() == []
    missing = Book.from_decisions(db, rfq_db=tmp_path / "nope.db")
    assert missing.flags and missing.positions["r1"].legs[0].spec is None


def test_from_store_pending_and_filled(tmp_path):
    st = Store(tmp_path / "rfq.db")
    st.upsert_markets([{"position_id": "p1", "market_id": "m", "condition_id": "cond1", "slug": "nfl-dal-phi-2026-10-27",
                        "title": "Cowboys vs. Eagles", "outcome": "Cowboys", "outcome_index": 0, "outcome_price": 0.6,
                        "volume": 0, "tags": "", "synced_at_ms": 1},
                       {"position_id": "p2", "market_id": "m2", "condition_id": "cond2", "slug": "nba-phi-nyk-2026-10-20",
                        "title": "76ers vs. Knicks", "outcome": "Knicks", "outcome_index": 1, "outcome_price": 0.55,
                        "volume": 0, "tags": "", "synced_at_ms": 1}])
    cid = st.combo_id("combo1", ["p1", "p2"])
    for rid, deadline, direction in (("r1", 10_000, "BUY"), ("r2", 1_000, "BUY"), ("r3", 10_000, "SELL")):
        st.insert_rfq({"rfq_id": rid, "requester_id": None, "combo_id": cid, "direction": direction, "side": "YES",
                       "size_unit": "notional", "size_value_e6": 1, "submission_deadline_ms": deadline, "first_seen_ms": 1,
                       "status": "QUOTED", "status_at_ms": 1, "stub": 0})
        st.insert_quote(rfq_id=rid, revision=1, price_e6=300_000, size_e6=4_000_000, source="collateral", is_paper=True,
                        state="SUBMITTED", at_ms=1)
    st.insert_execution("r3", None, "SUCCESS", None, 5)
    book = Book.from_store(st, now_ms=5_000)
    assert set(book.positions) == {"r1", "r3"}                     # r2's deadline passed without a fill
    r1, r3 = book.positions["r1"], book.positions["r3"]
    assert r1.status == PENDING and r1.expires_ms == 10_000 and r1.sign == -1 and r1.size == 4.0
    assert r3.status == FILLED and r3.sign == 1
    assert [l.key for l in r1.legs][0] == KEY_DAL and r1.legs[1].key.startswith("nba|")
    assert r1.legs[0].p == 0.6 and any("from catalog" in f for f in book.flags)
    book2 = Book.from_store(st, now_ms=5_000, p_of=lambda pid: 0.7)
    assert book2.positions["r1"].legs[0].p == 0.7 and not book2.flags
    st.close()
