"""As-of readers: field-for-field with the live readers, never a row after t, heartbeat staleness, lazy == eager marginals."""
import pytest

from rfqbot.backtest.asof import CatalogRO, Clock, GameData, Heartbeat, KalshiAsOf, PolyAsOf
from rfqbot.backtest.lazy import LazyMarginals
from rfqbot.kalshi_books import kalshi_for_legs
from rfqbot.ladders import game_markets
from rfqbot.marginals import compute_game, load_config

from backtest_world import G1, G2, PID, T0, TOK, build_world


def test_poly_asof_matches_game_markets_at_several_times(tmp_path):
    w = build_world(tmp_path)
    poly = PolyAsOf(w["books_db"], w["rfq_db"])
    for t in (T0 - 700_000, T0 - 600_000, T0 - 1, T0 + 39_999, T0 + 40_000, T0 + 40_001, T0 + 99_999, T0 + 500_000):
        for prefix in (G1, G2):
            live = game_markets(prefix, t, books_db=w["books_db"], rfq_db=w["rfq_db"])
            mine = poly.read(prefix, t)
            assert mine == live, (t, prefix)


def test_no_row_after_t_is_ever_read(tmp_path):
    a = build_world(tmp_path / "a")
    b = build_world(tmp_path / "b", extra_g1_row=(T0 + 50_001, 8800, 9000))
    pa, pb = PolyAsOf(a["books_db"], a["rfq_db"]), PolyAsOf(b["books_db"], b["rfq_db"])
    t = T0 + 50_000
    assert pa.read(G1, t) == pb.read(G1, t)                                 # the row at t + 1 ms is invisible at t
    assert pb.read(G1, t + 1) != pa.read(G1, t + 1)                         # and visible one millisecond later
    # through the whole marginal path, with the clock pinned at t
    for p in (pa, pb):
        p.clock = Clock(t)
    cfg = load_config()
    kal_a, kal_b = KalshiAsOf(a["kalshi_db"], Clock(t)), KalshiAsOf(b["kalshi_db"], Clock(t))
    ma = LazyMarginals(pa, kal_a, pa.game(G1), "nba", t, cfg).get(PID["g1_war"])
    mb = LazyMarginals(pb, kal_b, pb.game(G1), "nba", t, cfg).get(PID["g1_war"])
    assert ma[2] == mb[2] and ma[0] == mb[0]
    assert pa.heartbeat().at(t) <= t and pb.heartbeat().at(t) <= t


def test_end_ms_bounds_what_is_loaded(tmp_path):
    w = build_world(tmp_path, extra_g1_row=(T0 + 500_000, 8800, 9000))
    capped = PolyAsOf(w["books_db"], w["rfq_db"], end_ms=T0 + 100_000)
    full = PolyAsOf(w["books_db"], w["rfq_db"])
    assert capped.read(G1, T0 + 900_000) != full.read(G1, T0 + 900_000)     # past the cap the capped reader still shows the cap's last row
    assert capped.game(G1).n_rows < full.game(G1).n_rows
    assert all(q.at_ms <= T0 + 100_000 for q in capped.read(G1, T0 + 900_000))


def test_game_data_edge_cases(tmp_path):
    w = build_world(tmp_path)
    poly = PolyAsOf(w["books_db"], w["rfq_db"])
    g = poly.game(G1)
    assert isinstance(g, GameData) and g.pids == [PID["g1_war"], PID["g1_lak"], PID["g1_sp_lak"], PID["g1_sp_war"]]
    assert (g.positions(T0 - 10_000_000) == -1).all()                         # before every row
    assert g.first_row_ms == T0 - 600_000 and poly.first_row_ms(G1) == T0 - 600_000
    assert poly.read("nba-xxx-yyy-2026-01-01", T0) == []                      # unknown game
    mids = g.mids(T0)
    assert mids[PID["g1_war"]] == pytest.approx(0.49) and PID["g1_lak"] in mids
    wide = PolyAsOf(w["books_db"], w["rfq_db"])
    assert all(abs(v - round(v, 4)) < 1e-12 for v in wide.game(G1).mids(T0).values())


def test_heartbeat_and_confirmations(tmp_path):
    w = build_world(tmp_path)
    poly = PolyAsOf(w["books_db"], w["rfq_db"])
    hb = poly.heartbeat()
    assert hb.at(T0 + 5_000) == T0 and hb.at(T0 - 1) == T0 - 10_000 and hb.at(T0 - 10_000_000) is None
    quotes = poly.read(G1, T0 + 5_000)
    conf = poly.confirm(quotes, T0 + 5_000)
    assert set(conf) == {q.position_id for q in quotes} and set(conf.values()) == {T0}
    assert poly.confirm(quotes, T0 - 10_000_000) == {}                          # nothing is subscribed or alive yet
    assert poly.confirmed_ms(G1, PID["g1_war"], T0 + 5_000) == T0
    kal = KalshiAsOf(w["kalshi_db"], Clock(T0 + 5_000))
    assert kal.confirm() == T0 and kal.confirm(T0 - 1) <= T0 - 1
    assert Heartbeat([5, 1, 3]).at(4) == 3


def test_kalshi_asof_matches_kalshi_for_legs(tmp_path):
    w = build_world(tmp_path)
    kal = KalshiAsOf(w["kalshi_db"])
    pids = [PID["g2_thu"], PID["g2_spu"], PID["g1_war"]]
    for t in (T0 - 400_000, T0 - 300_000, T0 + 99_999, T0 + 100_000, T0 + 800_000):
        live = kalshi_for_legs(pids, t, db=w["kalshi_db"])
        mine = kal.read(pids, t)
        assert set(mine) == set(live) == {PID["g2_thu"], PID["g2_spu"]}        # a leg with no leg_matches row is absent
        for p in mine:
            assert mine[p][0] == live[p][0]
            assert mine[p][1] == live[p][1]
    assert kal.read([PID["g2_thu"]], T0 - 400_000)[PID["g2_thu"]][1] is None   # no Kalshi row yet at that time


@pytest.mark.parametrize("t", [T0 - 100_000, T0 + 10_000, T0 + 120_000])
def test_lazy_marginals_equal_compute_game(tmp_path, t):
    w = build_world(tmp_path)
    cfg = load_config()
    ref = compute_game(G2, t, cfg=cfg, books_db=w["books_db"], rfq_db=w["rfq_db"], kalshi_db=w["kalshi_db"])
    poly, kal = PolyAsOf(w["books_db"], w["rfq_db"], Clock(t)), KalshiAsOf(w["kalshi_db"], Clock(t))
    lm = LazyMarginals(poly, kal, poly.game(G2), "nba", t, cfg)
    assert len(ref) == len(lm) == 2 and lm
    for leg in ref:
        mq, row, m = lm.get(leg.quote.position_id)
        assert leg.quote == mq and leg.match == row and leg.marginal == m
    # the Kalshi match is used (weight > 0) once the Kalshi row exists
    thu = lm.get(PID["g2_thu"])[2]
    assert (thu.w_kalshi > 0) == (t >= T0 - 300_000)
    # the NO-side Spurs leg gets 1 - yes
    spu = lm.get(PID["g2_spu"])[2]
    assert spu.p_kalshi is None or abs(spu.p_kalshi - (1 - 0.59)) < 0.05 or abs(spu.p_kalshi - (1 - 0.61)) < 0.05
    assert lm.get("nope") is None and "nope" not in lm and PID["g2_thu"] in lm


def test_catalog_ro(tmp_path):
    w = build_world(tmp_path)
    cat = CatalogRO(w["rfq_db"])
    assert cat.get_market(PID["g1_war"])["slug"] == G1 and cat.get_market("missing") is None
    assert cat.get_market(PID["g1_war"]) is cat.get_market(PID["g1_war"])        # cached
    cat.close()


def test_clock_is_monotone():
    c = Clock(10)
    c.set(10); c.set(20)
    assert c() == 20
    with pytest.raises(ValueError):
        c.set(19)


def test_preload_gives_the_same_answers_as_per_game_loading(tmp_path):
    w = build_world(tmp_path)
    cold, pre = PolyAsOf(w["books_db"], w["rfq_db"], end_ms=T0 + 300_000), PolyAsOf(w["books_db"], w["rfq_db"], end_ms=T0 + 300_000)
    n = pre.preload(("nba",))
    assert n > 0 and pre._pre is not None and TOK["g1_war"] in pre._pre
    kc, kp = KalshiAsOf(w["kalshi_db"], end_ms=T0 + 300_000), KalshiAsOf(w["kalshi_db"], end_ms=T0 + 300_000)
    assert kp.preload() > 0 and kp._pre_done
    pids = [PID["g2_thu"], PID["g2_spu"]]
    for t in (T0 - 700_000, T0 - 300_000, T0 + 40_000, T0 + 100_000, T0 + 299_999):
        for prefix in (G1, G2):
            assert pre.read(prefix, t) == cold.read(prefix, t)
        assert kp.read(pids, t) == kc.read(pids, t)
    assert pre.heartbeat().at(T0 + 5000) == cold.heartbeat().at(T0 + 5000)
    # rows after end_ms are not preloaded either
    late = build_world(tmp_path / "late", extra_g1_row=(T0 + 400_000, 9000, 9200))
    p2 = PolyAsOf(late["books_db"], late["rfq_db"], end_ms=T0 + 300_000)
    p2.preload(("nba",))
    assert all(q.at_ms <= T0 + 300_000 for q in p2.read(G1, T0 + 900_000))
