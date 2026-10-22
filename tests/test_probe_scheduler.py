import pytest

from rfqbot.edge.data import DecisionRec, LegInfo, Print
from rfqbot.probe.config import ProbeConfig
from rfqbot.probe.gateway import DryRunGateway, E6
from rfqbot.probe.scheduler import Trigger, build_request, execute, plan, triggers
from rfqbot.probe.store import ProbeStore

T0 = 1_792_000_000_000
G1, G2 = "nba-okc-sas-2026-10-20", "nba-phi-nyk-2026-10-20"
CFG = ProbeConfig(mode="dry", delay_sec=5, horizons_sec=(60, 300), max_per_hour=4, max_per_combo=3, min_spacing_sec=30,
                  both_directions=True, disguise=False, size_usd=20)


def dec(i, direction="BUY", p_fair=0.2, price=0.21):
    legs = [LegInfo("p1", G1, 0.5, 0.005), LegInfo("p2", G2, 0.4, 0.005)]
    return DecisionRec(id=i, rfq_id=f"rfq-{i}", at_ms=T0 + i * 1000, direction=direction, requested_unit="notional", requested_e6=20 * E6,
                       n_legs=2, p_fair=p_fair, u_m=0.004, u_d=0.0, margin=abs(price - p_fair), price=price, size_e6=100 * E6, legs=legs)


def prt(i, direction="BUY", price=0.22, size=100 * E6, cond=None, requester="w1"):
    return Print(rfq_id=f"rfq-{i}", direction=direction, price=price, size_e6=size, executed_at_ms=T0 + i * 1000 + 10_000, requester=requester,
                 n_legs=2, position_ids=["p1", "p2"], prefixes=[G1, G2], condition_id=cond or f"0xc{i}")


def trig(i, direction="BUY", size=100 * E6, cond=None, at=None, seg="nba|2|BUY|disjoint|s0"):
    return Trigger(f"rfq-{i}", "print", cond or f"0xc{i}", ["p1", "p2"], direction, 0.22, size, at if at is not None else T0, "w1", dec(i, direction), seg)


def test_triggers_join_prints_to_quoted_decisions():
    ts = triggers(CFG, T0, decisions=[dec(1), dec(2, "SELL")], prints=[prt(1), prt(2, "SELL"), prt(3)])
    assert [t.rfq_id for t in ts] == ["rfq-1", "rfq-2"]                       # rfq-3 was printed but never quoted
    assert ts[0].kind == "print" and ts[0].condition_id == "0xc1" and ts[0].segment == "nba|2|BUY|disjoint|s0" and ts[0].our_price == 0.21


def test_plan_due_horizons_directions_and_hourly_budget():
    store = ProbeStore(None)
    t = trig(1, at=T0)
    # 3 s after the print: nothing due yet
    plans, st = plan([t], store, CFG, T0 + 3_000)
    assert plans == [] and st.due == 0 and st.candidates == 6
    # 7 s: the 5 s probe is due in both directions, the trigger's direction first
    plans, st = plan([t], store, CFG, T0 + 7_000)
    assert [(p.horizon_sec, p.direction) for p in plans] == [(5, "BUY"), (5, "SELL")] and st.selected == 2
    # 10 min later all horizons are due: 6 candidates, budget 4 per hour, same-combo spacing 30 s -> one two-sided pair now
    plans, st = plan([t], store, CFG, T0 + 600_000)
    assert [(p.horizon_sec, p.direction) for p in plans] == [(5, "BUY"), (5, "SELL")] and st.dropped_spacing == 4 and st.budget_remaining == 4
    # record them and see the budget shrink, the pair marked done and spacing still binding
    execute(plans, DryRunGateway(CFG), store, CFG, now=T0 + 600_000, pool=[], metas={})
    plans, st = plan([t], store, CFG, T0 + 600_010)
    assert plans == [] and st.already_done == 2 and st.dropped_spacing == 4 and st.budget_remaining == 2
    # 31 s later spacing has cleared, but the per-combo daily cap (3) leaves room for one more probe only
    plans, st = plan([t], store, CFG, T0 + 631_000)
    assert [(p.horizon_sec, p.direction) for p in plans] == [(60, "BUY")] and st.dropped_combo_cap == 3 and st.dropped_spacing == 0


def test_plan_combo_cap_and_too_late_and_priority():
    store = ProbeStore(None)
    cfg = ProbeConfig(**{**CFG.__dict__, "max_per_hour": 100, "min_spacing_sec": 0, "max_late_sec": 120})
    a = trig(1, size=50 * E6, cond="0xA")
    b = trig(2, size=500 * E6, cond="0xB", seg="nfl|2|BUY|disjoint|s0")
    plans, st = plan([a, b], store, cfg, T0 + 70_000)        # 5 s and 60 s due, 300 s not
    assert st.due == 8 and st.too_late == 0
    # per combo per day cap 3: of the 4 due per combo only 3 are selected; the fewest-probed segment wins ties, then size
    assert len(plans) == 6 and st.dropped_combo_cap == 2
    assert plans[0].trigger.condition_id == "0xB" and plans[0].direction == "BUY"       # larger size first (equal segment counts)
    # a probe that is already 10 min overdue is dropped, not sent late
    plans, st = plan([trig(3, at=T0 - 1_000_000)], ProbeStore(None), cfg, T0)
    assert plans == [] and st.too_late == 6


def test_build_request_sizes_both_directions():
    t = trig(1)
    buy = build_request(type("P", (), {"trigger": t, "direction": "BUY", "horizon_sec": 5})(), CFG, None)
    assert buy.unit == "notional" and buy.value_e6 == 20 * E6 and buy.leg_position_ids == ("p1", "p2")
    sell = build_request(type("P", (), {"trigger": t, "direction": "SELL", "horizon_sec": 5})(), CFG, None)
    assert sell.unit == "shares" and abs(sell.value_e6 / E6 - 20 / 0.21) < 1e-6          # shares worth $20 at our price


def test_per_minute_budget_pacing_and_http_refusal():
    from rfqbot.probe.gateway import FakeGateway
    store = ProbeStore(None)
    cfg = ProbeConfig(**{**CFG.__dict__, "max_per_hour": 100, "max_per_minute": 3, "min_spacing_sec": 0, "max_per_combo": 100})
    trigs = [trig(i, cond=f"0xc{i}") for i in range(4)]
    plans, st = plan(trigs, store, cfg, T0 + 7_000)
    assert len(plans) == 3 and st.budget_remaining == 3 and st.dropped_hourly == 5       # 8 due, 3 per minute
    # live sends are paced 60 / max_per_minute seconds apart through `pace`; dry / fake gateways are not

    class Http(FakeGateway):
        name = "http"
    live = ProbeConfig(**{**cfg.__dict__, "mode": "live"})
    sleeps = []
    rows = execute(plans, Http(lambda req: 0.23), store, live, now=T0 + 7_000, pool=[], metas={}, pace=sleeps.append)
    assert sleeps == [20.0, 20.0] and {r["mode"] for r in rows} == {"live"}       # http rows are 'live' rows for the budget
    sleeps.clear()
    execute(plans, FakeGateway(lambda req: 0.23), ProbeStore(None), cfg, now=T0 + 7_000, pool=[], metas={}, pace=sleeps.append)
    assert sleeps == []
    # the minute's budget is spent
    plans2, st2 = plan(trigs, store, cfg, T0 + 8_000)
    assert plans2 == [] and st2.budget_remaining == 0
    assert plan(trigs, store, cfg, T0 + 70_000)[1].budget_remaining == 3
    # the http gateway is refused outside live mode, even before any send
    with pytest.raises(RuntimeError, match="PROBE_MODE"):
        execute(plans, Http(lambda req: 0.23), ProbeStore(None), cfg, now=T0 + 7_000, pool=[], metas={})


def test_combo_cap_ignores_error_failed_and_skipped_rows():
    from rfqbot.probe.gateway import FakeGateway
    store = ProbeStore(None)
    cfg = ProbeConfig(**{**CFG.__dict__, "max_per_hour": 100, "min_spacing_sec": 0, "max_per_combo": 2})
    plans, _ = plan([trig(1)], store, cfg, T0 + 7_000)
    rows = execute(plans, FakeGateway(lambda req: None), store, cfg, now=T0 + 7_000, pool=[], metas={})   # NO_QUOTES -> FAILED
    assert [r["status"] for r in rows] == ["FAILED", "FAILED"] and store.combo_count_since("0xc1", T0) == 0
    store.put({**rows[0], "status": "ERROR", "trigger_rfq_id": "rfq-other", "created_ms": T0 + 8_000})
    assert store.combo_count_since("0xc1", T0) == 0 and store.count_since(T0) == 3      # the hourly count still sees them
    plans, st = plan([trig(1)], store, cfg, T0 + 70_000)                 # 60 s horizon due; the cap of 2 is untouched
    assert st.dropped_combo_cap == 0 and [(p.horizon_sec, p.direction) for p in plans] == [(60, "BUY"), (60, "SELL")]
    rows = execute(plans, FakeGateway(lambda req: 0.2), store, cfg, now=T0 + 70_000, pool=[], metas={})
    assert store.combo_count_since("0xc1", T0) == 2


def test_buy_safety_max_rejects_an_expensive_disguised_buy():
    from rfqbot.probe.disguise import ExtraLeg
    from rfqbot.probe.gateway import FakeGateway
    cfg = ProbeConfig(**{**CFG.__dict__, "disguise": True, "buy_safety_max": 0.90909})
    hot = trig(1)
    hot.decision.price = 0.95                                           # our quote on a heavy favourite combo
    extra = ExtraLeg("x1", "c-x1", "nfl-dal-phi-2026-10-26", "Yes", 0.995, 0.99, 1.0, T0 + 86_400_000, T0)
    type("P", (), {})
    with pytest.raises(ValueError, match="BUY_SAFETY_MAX"):
        build_request(type("P", (), {"trigger": hot, "direction": "BUY", "horizon_sec": 5})(), cfg, extra)
    sell = build_request(type("P", (), {"trigger": hot, "direction": "SELL", "horizon_sec": 5})(), cfg, extra)
    assert sell.unit == "shares"                                        # SELL is not capped
    assert build_request(type("P", (), {"trigger": trig(2), "direction": "BUY", "horizon_sec": 5})(), cfg, extra).unit == "notional"
    # through execute: the BUY is recorded as SKIPPED (so it is not re-planned) and does not count against the combo
    store = ProbeStore(None)
    plans, _ = plan([hot], store, cfg, T0 + 7_000)
    rows = execute(plans, FakeGateway(lambda req: 0.9), store, cfg, now=T0 + 7_000, pool=[extra], metas={})
    by = {r["direction"]: r for r in rows}
    assert by["BUY"]["status"] == "SKIPPED" and "BUY_SAFETY_MAX" in by["BUY"]["error"] and by["SELL"]["status"] == "AWAITING_REQUESTER_ACCEPTANCE"
    assert store.done("rfq-1", 5, "BUY") and store.combo_count_since("0xc1", T0) == 1


def test_execute_records_rows_and_learns_requester_id():
    from rfqbot.probe.gateway import FakeGateway
    store = ProbeStore(None)
    gw = FakeGateway(lambda req: 0.23, requestor_public_id="req_us", now_ms=lambda: T0 + 7_000)
    plans, _ = plan([trig(1)], store, CFG, T0 + 7_000)
    rows = execute(plans, gw, store, CFG, now=T0 + 7_000, pool=[], metas={})
    assert len(rows) == 2 and rows[0]["status"] == "AWAITING_REQUESTER_ACCEPTANCE" and rows[0]["backed_out_price"] == 0.23
    assert rows[0]["backed_out_err"] == 0.0 and rows[0]["extra_leg_json"] is None and rows[0]["our_price"] == 0.21
    assert store.meta("requester_id") == "req_us"
    assert store.count_since(T0) == 2 and store.combo_count_since("0xc1", T0) == 2 and store.done("rfq-1", 5, "SELL")
