"""Replay end to end on the synthetic world: determinism, skip reasons, fills into the book, no lookahead, the feed and the pricing views."""
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rfqbot.backtest.config import BacktestConfig, WindowConfig
from rfqbot.backtest.engine import (CacheView, IndependenceModel, ModelView, Replayer, scaled_calibration, scaled_shocks)
from rfqbot.backtest.feed import SportIndex, file_window_ms, iter_requests, raw_files
from rfqbot.backtest.fills import FillResult, decision_rec, evaluate_fill
from rfqbot.backtest.store import BacktestStore
from rfqbot.edge.data import Print
from rfqbot.pricing.calibrate import Calibration, PairParams
from rfqbot.risk.book import FILLED, PENDING

from backtest_world import G1, G2, PID, T0, add_print, build_world, rfq_frame, write_raw

E6 = 1_000_000
LEGS = [PID["g1_war"], PID["g2_thu"]]           # Warriors ML x Thunder ML: two games, disjoint
FIX = Path(__file__).parent / "fixtures"


def frames():
    return [
        rfq_frame("r0", T0 - 700_000, LEGS),                                    # before any book row of either game: no book yet
        rfq_frame("r1", T0 + 1_000, LEGS),                                      # r0 tracked the games; the first tick after it refreshes them
        rfq_frame("r1", T0 + 1_001, LEGS),                                      # exact duplicate frame: one RFQ only
        rfq_frame("r2", T0 + 20_000, LEGS),                                     # BUY: priced, quoted
        rfq_frame("r3", T0 + 30_000, [PID["g1_war"]], direction="SELL", unit="shares", value_e6=200 * E6),   # SELL single leg
        rfq_frame("r4", T0 + 60_000, ["pos_unknown", PID["g2_thu"]]),           # unknown market: pre-filtered
        rfq_frame("r5", T0 + 70_000, ["pos_soccer"]),                           # another sport: pre-filtered
        rfq_frame("r6", T0 + 200_000, LEGS),                                    # later, same combo
    ]


def world(tmp_path, **kw):
    w = build_world(tmp_path, **kw)
    s = __import__("rfqbot.store", fromlist=["Store"]).Store(w["rfq_db"])
    s.upsert_markets([{"position_id": "pos_soccer", "market_id": "9", "condition_id": "0xs", "slug": "epl-ars-che-2026-10-21", "title": "x",
                       "outcome": "Yes", "outcome_index": 0, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1}])
    s.close()
    raw = write_raw(tmp_path / "raw", frames())
    return w, raw


def run(tmp_path, cfgs=None, raw=None, w=None, window=None, name="x", **kw):
    w2, raw2 = (w, raw) if w is not None else world(tmp_path)
    window = window or WindowConfig(start_ms=T0 - 800_000, end_ms=T0 + 400_000)
    rp = Replayer(raw2, window, cfgs or [BacktestConfig(name="t", table="none")], w2["rfq_db"], w2["books_db"], w2["kalshi_db"],
                  tmp_path / f"bt_{name}.db", progress=lambda s: None, **kw)
    return rp, rp.run()


def decisions(path, run_id="t"):
    s = BacktestStore(path)
    rows = s.decisions(run_id)
    s.close()
    return rows


def test_replay_counts_skip_reasons_and_dedup(tmp_path):
    rp, res = run(tmp_path)
    s = res[0]
    assert s.prefilter["requests"] == 7                                         # r1 duplicate dropped
    assert s.prefilter["pre:unknown_market"] == 1 and s.prefilter["pre:other_sport"] == 1
    rows = {r["rfq_id"]: r for r in decisions(tmp_path / "bt_x.db")}
    assert set(rows) == {"r0", "r1", "r2", "r3", "r6"}
    assert rows["r0"]["decision"] == "skipped:replay:no_book_yet"               # the live reason was cold_game / no_quote; replay says why
    assert rows["r1"]["decision"] == "quoted"                                    # served from the refresh the cold RFQ r0 triggered
    assert rows["r2"]["decision"] == "quoted" and rows["r6"]["decision"] == "quoted"
    q = rows["r2"]
    assert 0 < q["p_fair"] < 1
    assert q["price_e6"] > q["p_fair"] * E6                                      # BUY: we sell YES above fair
    assert q["sport"] == "nba" and json.loads(q["games_json"]) == sorted([G1, G2]) and q["pair_class"] == "disjoint"
    assert q["expires_ms"] == q["deadline_ms"] + 31_000                          # the listener's expiry rule
    # SELL single leg: we buy YES below fair
    assert rows["r3"]["decision"] == "quoted" and rows["r3"]["price_e6"] < rows["r3"]["p_fair"] * E6


def test_replay_is_deterministic(tmp_path):
    w, raw = world(tmp_path)
    cols = ("rfq_id", "at_ms", "decision", "p_fair", "u_m", "u_d", "margin", "price_e6", "size_e6", "binding_cap", "e_i")
    out = []
    for i in range(2):
        run(tmp_path, w=w, raw=raw, name=f"d{i}")
        out.append([tuple(r[c] for c in cols) for r in decisions(tmp_path / f"bt_d{i}.db")])
    assert out[0] == out[1] and len(out[0]) == 5


def test_a_print_fills_the_pending_quote_into_the_book(tmp_path):
    w, raw = world(tmp_path)
    # r2: BUY. Find our price first, then print just above it so we win and are strictly inside by a tick
    rp, _ = run(tmp_path, w=w, raw=raw, name="probe")
    r2 = {r["rfq_id"]: r for r in decisions(tmp_path / "bt_probe.db")}["r2"]
    ours = r2["price_e6"]
    size = r2["size_e6"]
    add_print(w["rfq_db"], "r2", direction="BUY", price_e6=ours + 2_000, size_e6=size * 2, at_ms=T0 + 25_000, legs=LEGS)
    rp, res = run(tmp_path, w=w, raw=raw, name="filled")
    row = {r["rfq_id"]: r for r in decisions(tmp_path / "bt_filled.db")}["r2"]
    assert (row["printed"], row["fill_win"], row["fill_strict"]) == (1, 1, 1)
    assert row["fill_frac"] == pytest.approx(0.5) and row["fill_size_e6"] == size       # the requester traded 2x our size: we fill all of ours
    assert row["print_price"] == pytest.approx((ours + 2_000) / E6) and row["executed_at_ms"] == T0 + 25_000
    assert row["delta"] == pytest.approx((ours + 2_000) / E6 - row["p_fair"])
    pos = rp.strategies[0].inv.book.positions["r2"]
    assert pos.status == FILLED and pos.sign == -1 and pos.size == pytest.approx(size / E6, abs=1e-6)
    assert rp.strategies[0].inv.book.positions["r6"].status in (PENDING, "expired")
    assert res[0].filled == 1 and res[0].win == 1 and res[0].strict == 1


def test_tie_wins_but_is_not_strict_and_a_lost_print_never_fills(tmp_path):
    w, raw = world(tmp_path)
    run(tmp_path, w=w, raw=raw, name="probe")
    r2 = {r["rfq_id"]: r for r in decisions(tmp_path / "bt_probe.db")}["r2"]
    add_print(w["rfq_db"], "r2", direction="BUY", price_e6=r2["price_e6"], size_e6=r2["size_e6"], at_ms=T0 + 25_000, legs=LEGS)
    add_print(w["rfq_db"], "r6", direction="BUY", price_e6=1_000, size_e6=r2["size_e6"], at_ms=T0 + 210_000, legs=LEGS, requester="req_b")
    rp, res = run(tmp_path, w=w, raw=raw, name="tie")
    rows = {r["rfq_id"]: r for r in decisions(tmp_path / "bt_tie.db")}
    assert (rows["r2"]["fill_win"], rows["r2"]["fill_strict"], rows["r2"]["fill_size_e6"]) == (1, 0, r2["size_e6"])
    assert (rows["r6"]["printed"], rows["r6"]["fill_win"], rows["r6"]["fill_size_e6"]) == (1, 0, 0)
    assert rp.strategies[0].inv.book.positions["r6"].status != FILLED
    assert (res[0].win, res[0].strict, res[0].filled) == (1, 0, 1)
    # the strict rule drives the book when asked: the tie is not a strict win, so nothing fills
    rp2, res2 = run(tmp_path, [BacktestConfig(name="t", table="none", fill_rule="strict")], w=w, raw=raw, name="tie2")
    assert (res2[0].win, res2[0].strict, res2[0].filled) == (1, 0, 0)


def test_replay_never_uses_a_book_row_after_the_request(tmp_path):
    fr = [rfq_frame("a0", T0 + 1_000, LEGS), rfq_frame("a1", T0 + 10_000, LEGS), rfq_frame("b", T0 + 200_000, LEGS), rfq_frame("c", T0 + 260_000, LEGS)]
    wa = build_world(tmp_path / "a")
    wb = build_world(tmp_path / "b", extra_g1_row=(T0 + 200_001, 9000, 9200))      # one millisecond after request "b"
    ra_raw, rb_raw = write_raw(tmp_path / "a" / "raw", fr), write_raw(tmp_path / "b" / "raw", fr)
    run(tmp_path / "a", w=wa, raw=ra_raw, name="a"); run(tmp_path / "b", w=wb, raw=rb_raw, name="b")
    ra = {r["rfq_id"]: r for r in decisions(tmp_path / "a" / "bt_a.db")}
    rb = {r["rfq_id"]: r for r in decisions(tmp_path / "b" / "bt_b.db")}
    for rid in ("a0", "a1", "b"):                                                # b is at T0+200000, the new row at T0+200001
        assert (ra[rid]["decision"], ra[rid]["p_fair"], ra[rid]["price_e6"]) == (rb[rid]["decision"], rb[rid]["p_fair"], rb[rid]["price_e6"]), rid
    assert ra["b"]["decision"] == "quoted"
    assert rb["c"]["p_fair"] != ra["c"]["p_fair"]                                # the row is seen once a refresh happens after it


def test_feed_window_files_and_order(tmp_path):
    raw = write_raw(tmp_path / "raw", frames())
    write_raw(raw, [rfq_frame("late", T0 + 7_200_000, LEGS)], name="2026-10-21/01.jsonl.gz")
    assert file_window_ms(Path("data/raw/2026-10-20/23.jsonl.gz")) == (1_792_537_200_000, 1_792_540_800_000)
    assert [p.name for p in raw_files(raw, T0, T0 + 1000)] == ["23.jsonl.gz"]       # the 01h file is outside the window
    got = list(iter_requests(raw, T0 - 800_000, T0 + 400_000))
    assert [m.rfq_id for _, m in got] == ["r0", "r1", "r2", "r3", "r4", "r5", "r6"]
    assert [t for t, _ in got] == sorted(t for t, _ in got)
    c = {}
    list(iter_requests(raw, T0 - 800_000, T0 + 400_000, counts=c))
    assert c == {"frames": 7, "duplicate": 1}
    assert [m.rfq_id for _, m in iter_requests(raw, T0 + 25_000, T0 + 65_000)] == ["r3", "r4"]


def test_sample_fixture_through_the_feed():
    sample = FIX / "rfq_sample_75s.jsonl.gz"
    c = {}
    got = list(iter_requests(sample, 0, 1 << 60, counts=c))
    assert len(got) == c["frames"] == 11232 and len({m.rfq_id for _, m in got}) == 11232
    ts = [t for t, _ in got]
    assert ts == sorted(ts) and ts[0] >= 1_792_529_740_261 and ts[-1] <= 1_792_529_814_813
    assert all(m.leg_position_ids and m.direction in ("BUY", "SELL") for _, m in got[:200])


def test_sport_index_classification(tmp_path):
    w, _ = world(tmp_path)
    si = SportIndex(w["rfq_db"])
    assert si.classify(LEGS) == "quoted" and si.classify([PID["g1_war"]]) == "quoted"
    assert si.classify(["pos_soccer"]) == "other_sport" and si.classify([PID["g1_war"], "pos_soccer"]) == "other_sport"
    assert si.classify(["nope"]) == "unknown_market" and si.classify(["pos_soccer", "nope"]) == "unknown_market"


# ------------------------------------------------------------------ fills
def fake_decision(direction="BUY", p_fair=0.30, price=0.31, size=100 * E6, quoted=True):
    leg = SimpleNamespace(position_id="p1", slug="nba-gsw-lal-2026-10-21", p=0.3, err=0.01, kalshi_confidence="none")
    return SimpleNamespace(rfq_id="rq", at_ms=T0, direction=direction, requested_unit="notional", requested_e6=20 * E6, n_legs=1, legs=[leg],
                           clusters=[], p_fair=p_fair, u_m=0.01, u_d=0.0, margin=abs(price - p_fair), price_e6=int(round(price * E6)),
                           size_e6=size, requester="w1", reason="", quoted=quoted)


def prt(direction, price, size=100 * E6, at=T0 + 5000):
    return Print("rq", direction, price, size, at, "w1", 1, ["p1"], ["nba-gsw-lal-2026-10-21"])


def test_evaluate_fill_win_strict_fraction_for_both_directions():
    d = fake_decision("BUY", 0.30, 0.310)
    assert evaluate_fill(d, {"rq": prt("BUY", 0.312)}) == FillResult(True, True, True, 1.0, 0.312, 100 * E6, T0 + 5000, pytest.approx(0.012), 100 * E6)
    f = evaluate_fill(d, {"rq": prt("BUY", 0.3105)})                                # inside by half a tick: wins, not strict
    assert (f.win, f.strict) == (True, False)
    assert evaluate_fill(d, {"rq": prt("BUY", 0.310)}).win and not evaluate_fill(d, {"rq": prt("BUY", 0.310)}).strict
    f = evaluate_fill(d, {"rq": prt("BUY", 0.309)})                                 # print below our ask: lost
    assert (f.printed, f.win, f.fill_size_e6) == (True, False, 0)
    f = evaluate_fill(d, {"rq": prt("BUY", 0.33, size=400 * E6)})
    assert f.frac == pytest.approx(0.25) and f.fill_size_e6 == 100 * E6      # a bigger print: our whole quote fills
    f = evaluate_fill(d, {"rq": prt("BUY", 0.33, size=40 * E6)})
    assert f.fill_size_e6 == 40 * E6                                            # a smaller print caps the fill at the print
    s = fake_decision("SELL", 0.60, 0.590)
    assert evaluate_fill(s, {"rq": prt("SELL", 0.588)}).strict and evaluate_fill(s, {"rq": prt("SELL", 0.590)}).win
    assert not evaluate_fill(s, {"rq": prt("SELL", 0.591)}).win
    assert evaluate_fill(d, {}) == FillResult() and evaluate_fill(fake_decision(quoted=False), {"rq": prt("BUY", 0.5)}) == FillResult()
    assert evaluate_fill(d, {"rq": prt("BUY", 0.33)}, rule="strict").fill_size_e6 == 100 * E6
    assert evaluate_fill(d, {"rq": prt("BUY", 0.3105)}, rule="strict").fill_size_e6 == 0 and evaluate_fill(d, {"rq": prt("BUY", 0.3105)}).fill_size_e6 == 100 * E6
    rec = decision_rec(d)
    assert rec.p_fair == 0.30 and rec.price == pytest.approx(0.31) and rec.legs[0].prefix == "nba-gsw-lal-2026-10-21"


# ------------------------------------------------------------------ pricing views
class DummyModel:
    def __init__(self):
        self.calls = []

    def joint(self, specs, shocks=None, calibration=None):
        self.calls.append((list(specs), shocks, calibration))
        return SimpleNamespace(p_cal=0.4)


def test_model_view_passes_calibration_and_scaled_shocks(monkeypatch):
    from rfqbot.backtest import engine
    from rfqbot.pricing.joint import Shock
    # the engine's shock set is bootstrap replicas only now; scaling must still handle the correlation kinds
    monkeypatch.setattr(engine, "DEFAULT_SHOCKS", (Shock("halves_corr", 0.15), Shock("halves_corr", -0.15), Shock("prior_rho", -0.2),
                                                   Shock("prior_rho", 0.2), Shock("bootstrap", 1), Shock("bootstrap", 2)))
    assert [s.value for s in scaled_shocks(0.5)][:4] == [0.075, -0.075, -0.1, 0.1]
    assert [s.value for s in scaled_shocks(2.0)][4:] == [1, 2]                      # bootstrap seeds are not scaled
    assert all(s.kind == "bootstrap" for s in __import__("rfqbot.pricing.joint", fromlist=["x"]).DEFAULT_SHOCKS)
    cal = Calibration(pairs={"a": PairParams(lam=0.8, lo=None, hi=None, resid=0.01, n=5, n_games=3, n_lo=0, n_hi=0)}, identity=False)
    sc = scaled_calibration(cal, 0.75)
    assert sc.pairs["a"].lam == pytest.approx(0.6) and cal.pairs["a"].lam == 0.8 and sc.pairs["a"].resid == 0.01
    m = DummyModel()
    v = ModelView(m, sc, scaled_shocks(2.0))
    assert v.joint(["s1"]).p_cal == 0.4 and m.calls[0][1] == scaled_shocks(2.0) and m.calls[0][2] is sc


def test_cache_view_switches():
    entry = SimpleNamespace(model=DummyModel(), specs={}, marginals={})
    entry = dataclasses.make_dataclass("E", [("model", object), ("specs", dict), ("marginals", dict)])(entry.model, {}, {})
    cache = SimpleNamespace(get=lambda prefix: entry, stats={})
    assert CacheView(cache, BacktestConfig()).get("g") is entry                              # joint + fitted + x1: untouched
    v = CacheView(cache, BacktestConfig(calibration="ones")).get("g")
    assert isinstance(v.model, ModelView) and v.model.calibration is not None and v.model.calibration.identity
    v2 = CacheView(cache, BacktestConfig(pricing="independence")).get("g")
    assert isinstance(v2.model, IndependenceModel)
    assert CacheView(cache, BacktestConfig(shock_scale=0.5)).get("g").model.shocks == scaled_shocks(0.5)
    cache2 = SimpleNamespace(get=lambda prefix: None, stats={})
    assert CacheView(cache2, BacktestConfig(calibration="ones")).get("g") is None


def test_backtest_config_roundtrip_and_quote_config():
    bc = BacktestConfig(name="x", quote={"base_edge": 0.01, "risk": False}, table="none", calibration=0.75, shock_scale=2.0)
    assert BacktestConfig.from_json(bc.to_json()) == bc
    q = bc.quote_config()
    assert q.base_edge == 0.01 and q.risk is False and q.edge is False and str(q.decisions_db) == "/dev/null"
    assert BacktestConfig(table="repo").quote_config().edge is True
