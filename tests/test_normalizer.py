from pathlib import Path

import pytest

from rfqbot import messages as m
from rfqbot import normalizer as n
from rfqbot.normalizer import Normalizer, PaperQuote
from rfqbot.replay import replay
from rfqbot.store import Store

FIX = Path(__file__).parent / "fixtures" / "messages.jsonl"


def _req(rfq_id, t, legs=("p1", "p2"), direction="BUY"):
    return m.RfqRequest(
        type="RFQ_REQUEST", rfq_id=rfq_id, requestor_public_id="r", leg_position_ids=tuple(legs),
        condition_id="0xc", yes_position_id="y", no_position_id="n", direction=direction, side="YES",
        requested_size=m.RequestedSize("notional", 1_000_000), submission_deadline_ms=t + 400,
    )


def test_full_lifecycle_from_fixture(store):
    counts = replay(FIX, store, expiry_grace_ms=5_000)
    assert counts["RFQ_REQUEST"] == 6
    status = {r["rfq_id"]: r["status"] for r in store.conn.execute("SELECT rfq_id, status FROM rfqs")}
    assert status == {
        "rfq_a": n.SETTLED,
        "rfq_b": n.FILLED_BY_OTHER,
        "rfq_c": n.EXPIRED,
        "rfq_d": n.CANCELLED,
        "rfq_e": n.EXPIRED,
        "rfq_f": n.EXPIRED,   # closed by the final sweep
    }
    # a: settled with tx hash, trade marked ours, quote filled
    assert store.scalar("SELECT tx_hash FROM settlements WHERE rfq_id='rfq_a'") == "0xtx_a"
    assert store.scalar("SELECT is_ours FROM trades WHERE rfq_id='rfq_a'") == 1
    assert store.latest_quote("rfq_a")["state"] == n.Q_FILLED
    assert store.latest_quote("rfq_a")["server_quote_id"] == "quote_a1"
    evs = [e["event_type"] for e in store.events_for("rfq_a")]
    assert evs == [n.EV_QUOTE_DRAFTED, n.EV_QUOTE_ACKED, n.EV_CONFIRMATION_REQUESTED,
                   n.EV_CONFIRMATION_SENT, n.EV_EXECUTION_UPDATE, n.EV_EXECUTION_UPDATE,
                   n.EV_EXECUTION_UPDATE, n.EV_SETTLED, n.EV_TRADE_BROADCAST]
    # b: two revisions, first superseded; replayed trade deduped; lost quote expired
    qs = store.conn.execute("SELECT revision, state FROM quotes WHERE rfq_id='rfq_b' ORDER BY revision").fetchall()
    assert [(q[0], q[1]) for q in qs] == [(1, n.Q_SUPERSEDED), (2, n.Q_EXPIRED)]
    assert store.scalar("SELECT COUNT(*) FROM trades WHERE rfq_id='rfq_b'") == 1
    assert store.scalar("SELECT is_ours FROM trades WHERE rfq_id='rfq_b'") == 0
    assert store.scalar("SELECT COUNT(*) FROM rfq_events WHERE rfq_id='rfq_b' AND event_type='TRADE_BROADCAST'") == 1
    # c: rejected with code
    q = store.latest_quote("rfq_c")
    assert q["state"] == n.Q_REJECTED and q["reason"] == "QUOTED_PRICE_ABOVE_SAFETY_THRESHOLD"
    # d: cancelled
    assert store.latest_quote("rfq_d")["state"] == n.Q_CANCELLED
    # legs captured (via combos; rfq_a and rfq_d share combo_a)
    assert store.scalar("SELECT COUNT(*) FROM rfq_legs") == 2 + 3 + 2 + 2 + 2 + 2
    assert store.scalar("SELECT COUNT(*) FROM combos") == 5
    assert store.scalar("SELECT n_legs FROM rfqs_v WHERE rfq_id='rfq_b'") == 3
    assert store.scalar("SELECT requestor_public_id FROM rfqs_v WHERE rfq_id='rfq_b'") == "req_2"
    # raw copied: unknown type kept, RFQ_REQUEST left out of the db by default (it lives in the raw log)
    assert store.scalar("SELECT COUNT(*) FROM raw_messages WHERE msg_type='SOME_FUTURE_TYPE'") == 1
    assert store.scalar("SELECT COUNT(*) FROM raw_messages WHERE msg_type='RFQ_REQUEST'") == 0


def test_expiry_uses_passed_clock_not_wall_clock(store):
    norm = Normalizer(store, expiry_grace_ms=5_000)
    t = 1_700_000_000_000
    norm.apply(_req("r1", t), at_ms=t)
    # deadline t+400; horizon = 10s + 1s + 5s = 16s. Just before: nothing.
    assert norm.expire_stale(t + 400 + 16_000) == []
    assert norm.expire_stale(t + 400 + 16_001) == ["r1"]
    assert store.get_rfq("r1")["status"] == n.EXPIRED
    assert norm.expire_stale(t + 10**9) == []  # idempotent


def test_executed_rfq_is_never_expired(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    norm.apply(_req("r1", t), at_ms=t)
    norm.apply(m.ExecutionUpdate(type="RFQ_EXECUTION_UPDATE", rfq_id="r1", status="MINED", tx_hash="0x1"), at_ms=t + 2000)
    assert store.get_rfq("r1")["status"] == n.EXECUTING
    assert norm.expire_stale(t + 10**9) == []


def test_late_lifecycle_message_creates_stub(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    conf = m.ConfirmationRequest(
        type="RFQ_CONFIRMATION_REQUEST", rfq_id="gap", quote_id="q", signer_address="s", maker_address="mk",
        signature_type=3, leg_position_ids=("a", "b", "c"), condition_id="0xc", yes_position_id="y",
        no_position_id="n", direction="BUY", side="YES", fill_size_e6=1, price_e6=2, confirm_by_ms=t + 1000,
    )
    res = norm.apply(conf, at_ms=t)
    assert res.new_rfq
    r = store.get_rfq_full("gap")
    assert r["status"] == n.SELECTED and r["n_legs"] == 3 and r["stub"] == 1
    assert store.scalar("SELECT COUNT(*) FROM rfq_legs WHERE rfq_id='gap'") == 3
    # the request itself arriving later fills in the stub without resetting status
    norm.apply(_req("gap", t - 500, legs=("a", "b", "c")), at_ms=t + 1)
    r = store.get_rfq_full("gap")
    assert r["status"] == n.SELECTED and r["stub"] == 0 and r["size_value_e6"] == 1_000_000


def test_duplicate_request_is_logged_not_overwritten(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    assert norm.apply(_req("r1", t), at_ms=t).new_rfq
    res = norm.apply(_req("r1", t + 5), at_ms=t + 5)
    assert res.duplicate and not res.new_rfq
    assert store.get_rfq("r1")["first_seen_ms"] == t


def test_paper_quote_revisions_and_status(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    norm.apply(_req("r1", t), at_ms=t)
    norm.record_paper_quote("r1", PaperQuote(400_000, 1_000_000, reason="base"), at_ms=t + 10)
    assert store.get_rfq("r1")["status"] == n.QUOTED
    norm.record_paper_quote("r1", PaperQuote(410_000, 1_000_000, reason="skew"), at_ms=t + 20)
    rows = store.conn.execute("SELECT revision, state, price_e6 FROM quotes WHERE rfq_id='r1' ORDER BY revision").fetchall()
    assert [tuple(r) for r in rows] == [(1, n.Q_SUPERSEDED, 400_000), (2, n.Q_SUBMITTED, 410_000)]
    evs = [e["event_type"] for e in store.events_for("r1")]
    assert evs == [n.EV_QUOTE_DRAFTED, n.EV_QUOTE_REVISED]


def test_unknown_legs_reported_and_joined(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    store.upsert_markets([{
        "position_id": "p1", "market_id": "1", "condition_id": "0xm1", "slug": "m1", "title": "M1", "outcome": "Yes",
        "outcome_index": 0, "outcome_price": 0.5, "volume": 1.0, "tags": "[]", "synced_at_ms": t,
    }])
    res = norm.apply(_req("r1", t, legs=("p1", "p9")), at_ms=t)
    assert res.unknown_positions == ["p9"]
    legs = {r["position_id"]: r["market_slug"] for r in store.conn.execute("SELECT * FROM rfq_legs")}
    assert legs == {"p1": "m1", "p9": None}
    store.upsert_markets([{
        "position_id": "p9", "market_id": "9", "condition_id": "0xm9", "slug": "m9", "title": "M9", "outcome": "Yes",
        "outcome_index": 0, "outcome_price": 0.1, "volume": 0.0, "tags": "[]", "synced_at_ms": t,
    }])
    # the view joins at query time: no backfill step needed
    assert store.scalar("SELECT market_slug FROM rfq_legs WHERE position_id='p9'") == "m9"


def test_replay_is_deterministic(tmp_path):
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    replay(FIX, a)
    replay(FIX, b)
    assert not a.in_transaction and not b.in_transaction                 # replay commits what it wrote
    for table in ("rfqs", "quotes", "trades", "settlements", "combos", "combo_legs", "unmet_demand_items"):
        ra = [tuple(r) for r in a.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        rb = [tuple(r) for r in b.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        assert ra == rb, table
    # replaying a db (recorded with all raw types) into another db gives the same normalized state
    full = Store(tmp_path / "full.db")
    replay(FIX, full, raw_db_types="all")
    c = Store(tmp_path / "c.db")
    replay(tmp_path / "full.db", c)
    assert [tuple(r) for r in a.conn.execute("SELECT rfq_id, status FROM rfqs ORDER BY 1")] == \
           [tuple(r) for r in c.conn.execute("SELECT rfq_id, status FROM rfqs ORDER BY 1")]


def test_unmet_demand_summary_recorded(store):
    norm = Normalizer(store)
    t = 1_700_000_000_000
    msg = m.parse_inbound({
        "type": "RFQ_UNMET_DEMAND_SUMMARY", "window_ms": 30000, "total_unfilled": 99,
        "rfqs": [{"reason": "NO_QUOTES", "direction": "BUY", "side": "YES", "leg_position_ids": ["x", "y"],
                  "condition_id": "0xcombo_x", "requested_size_bucket": {"unit": "notional", "min_value_e6": "1000000",
                  "max_value_e6": "5000000"}, "count": 43}],
    })
    assert isinstance(msg, m.UnmetDemandSummary) and msg.items[0].count == 43
    norm.apply(msg, at_ms=t)
    assert store.scalar("SELECT total_unfilled FROM unmet_demand") == 99
    assert store.scalar("SELECT count FROM unmet_demand_items") == 43
    assert store.scalar("SELECT n_legs FROM combos WHERE condition_id='0xcombo_x'") == 2


def test_late_trade_broadcast_overrides_derived_expiry(store):
    norm = Normalizer(store, expiry_grace_ms=5_000)
    t = 1_700_000_000_000
    norm.apply(_req("r1", t), at_ms=t)
    assert norm.expire_stale(t + 400 + 16_001) == ["r1"]
    trade = m.TradeBroadcast(type="RFQ_TRADE", rfq_id="r1", requester_id="r", condition_id="0xc",
                             leg_position_ids=("p1", "p2"), direction="BUY", side="YES", price_e6=500_000,
                             size_e6=1_000_000, executed_at_ms=t + 16_700)
    norm.apply(trade, at_ms=t + 16_780)
    assert store.get_rfq("r1")["status"] == n.FILLED_BY_OTHER
    assert store.scalar("SELECT is_ours FROM trades WHERE rfq_id='r1'") == 0


def test_nested_tx_rolls_back_only_the_failing_message(store):
    """A handler that fails half-way through a message must not leave partial rows that the batch
    commit then persists: inner tx() levels are savepoints."""
    t = 1_700_000_000_000
    store.begin()                                   # the listener's long-lived batch
    norm = Normalizer(store)
    norm.apply(_req("ok1", t), at_ms=t)
    with pytest.raises(RuntimeError):
        with store.tx():                            # a message-level unit inside the batch
            store.insert_rfq({"rfq_id": "half", "requester_id": None, "combo_id": None, "direction": "BUY", "side": "YES",
                              "size_unit": "notional", "size_value_e6": 1, "submission_deadline_ms": t, "first_seen_ms": t,
                              "status": n.RECEIVED, "status_at_ms": t, "stub": 0})
            raise RuntimeError("handler blew up after the first insert")
    norm.apply(_req("ok2", t + 1), at_ms=t + 1)
    store.commit()
    ids = sorted(r[0] for r in store.conn.execute("SELECT rfq_id FROM rfqs"))
    assert ids == ["ok1", "ok2"] and not store.in_transaction
    # two levels deep: the inner failure is contained, the outer unit still commits
    with store.tx():
        norm.apply(_req("ok3", t + 2), at_ms=t + 2)
        try:
            with store.tx():
                store.add_event("ok3", "X", t + 2)
                raise ValueError("inner")
        except ValueError:
            pass
    assert store.get_rfq("ok3") is not None and store.events_for("ok3") == []


def test_close_rolls_back_an_uncommitted_batch(tmp_path):
    path = tmp_path / "s.db"
    st = Store(path)
    st.begin()
    Normalizer(st).apply(_req("r1", 1), at_ms=1)
    st.close()                                      # no commit: nothing persists
    st2 = Store(path)
    assert st2.get_rfq("r1") is None
    st2.begin(); Normalizer(st2).apply(_req("r1", 1), at_ms=1); st2.commit(); st2.close()
    assert Store(path).get_rfq("r1") is not None


def test_store_fill_stub_combo_backfill_count_by_and_schema(store, tmp_path):
    from rfqbot.store import SCHEMA_VERSION, SchemaMismatch
    t = 1_700_000_000_000
    # fill_rfq_stub accepts only request fields
    store.insert_rfq({"rfq_id": "s", "requester_id": None, "combo_id": None, "direction": None, "side": None, "size_unit": None,
                      "size_value_e6": None, "submission_deadline_ms": None, "first_seen_ms": t, "status": n.SELECTED,
                      "status_at_ms": t, "stub": 1})
    store.fill_rfq_stub("s", direction="BUY", size_value_e6=5)
    r = store.get_rfq("s")
    assert r["stub"] == 0 and r["direction"] == "BUY" and r["size_value_e6"] == 5 and r["status"] == n.SELECTED
    with pytest.raises(ValueError):
        store.fill_rfq_stub("s", status="SETTLED")
    # a combo first seen on a trade (no yes/no ids) gets them from the later request, even when cached
    cid = store.combo_id("0xc9", ["a", "b"], at_ms=t)
    assert store.conn.execute("SELECT yes_position_id FROM combos WHERE id=?", (cid,)).fetchone()[0] is None
    assert store.combo_id("0xc9", ["a", "b"], yes="Y", no="N", at_ms=t + 1) == cid
    row = store.conn.execute("SELECT yes_position_id, no_position_id, n_legs FROM combos WHERE id=?", (cid,)).fetchone()
    assert tuple(row) == ("Y", "N", 2)
    assert store.combo_id("0xc9", yes="Z") == cid                            # never overwritten once set
    assert store.conn.execute("SELECT yes_position_id FROM combos WHERE id=?", (cid,)).fetchone()[0] == "Y"
    # count_by validates against the schema (table and column are interpolated into SQL)
    assert store.count_by("rfqs", "status")[0][1] >= 1 and store.count_by("rfqs_v", "n_legs") is not None
    for table, col in (("rfqs", "nope"), ("sqlite_master", "name"), ("rfqs; DROP TABLE rfqs; --", "status")):
        with pytest.raises(ValueError):
            store.count_by(table, col)
    # schema_version is read back
    path = tmp_path / "old.db"
    s0 = Store(path)
    s0.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
    s0.close()
    with pytest.raises(SchemaMismatch):
        Store(path)


def test_rawlog_tolerates_corrupt_gzip_tail(tmp_path):
    import gzip
    from rfqbot.rawlog import iter_rawlog_file
    good = gzip.compress(b'{"t":1,"d":"in","c":null,"s":1,"p":"{}"}\n')
    path = tmp_path / "x.jsonl.gz"
    path.write_bytes(good + b"\x1f\x8b\x08\x00garbage-not-a-member")   # a second, corrupt member (killed mid-write)
    assert [o["t"] for o in iter_rawlog_file(path)] == [1]
    path.write_bytes(good[:-6])                                           # truncated inside the block
    assert list(iter_rawlog_file(path)) in ([], [{"t": 1, "d": "in", "c": None, "s": 1, "p": "{}"}])
    (tmp_path / "bad.jsonl").write_text('[1, 2]\n{"t":2,"d":"in","c":null,"s":1,"p":"{}"}\n')
    from rfqbot.replay import iter_source
    rows = list(iter_source(tmp_path / "bad.jsonl"))
    assert [r.received_at_ms for r in rows] == [2]                      # a non-object line is skipped, not a crash
