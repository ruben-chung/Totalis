import pytest

from rfqbot.probe.disguise import ExtraLeg, LegMeta, back_out, disguised_price, parse_ts, pick_extra_leg

T0 = 1_792_000_000_000
H = 3_600_000


def meta(pid, slug, cond=None, start=T0 + 2 * H):
    return LegMeta(pid, cond or f"c-{pid}", slug, "Yes", 0, start, None)


def extra(pid, slug, p=0.995, spread=0.01, start=T0 + 3 * H, cond=None):
    return ExtraLeg(pid, cond or f"c-{pid}", slug, "Yes", p, p - spread / 2, p + spread / 2, start, T0)


COMBO = [meta("p1", "nba-okc-sas-2026-10-20"), meta("p2", "nba-phi-nyk-2026-10-20")]


def test_parse_ts_formats():
    assert parse_ts("2026-10-24 14:00:00+00") == parse_ts("2026-10-24T14:00:00Z") == 1_792_850_400_000
    assert parse_ts(None) is None and parse_ts("garbage") is None


def test_pick_only_disjoint_future_near_certain_legs():
    pool = [
        extra("x1", "nba-okc-sas-2026-10-20-total-220pt5"),            # same game as p1 -> excluded
        extra("x2", "nba-okc-bos-2026-10-22"),                          # shares a team (OKC) -> shared_team, excluded
        extra("x3", "nfl-dal-phi-2026-10-26"),                          # different sport, later start -> ok
        extra("x4", "epl-liv-mun-2026-10-25", p=0.999, spread=0.002),   # ok and better (higher p, tighter)
        extra("x5", "epl-ars-che-2026-10-25", p=0.999, spread=0.002, start=T0 - H),   # in-play -> excluded
        extra("x6", "ucl-bar-psg-2026-10-21", p=0.999, spread=0.002, start=T0 + H),   # resolves before the combo -> excluded
        extra("x7", "nfl-sea-la-2026-10-26", start=None),                               # unknown start -> excluded
    ]
    best = pick_extra_leg(COMBO, T0, pool)
    assert best.position_id == "x4"
    assert best.game_key is not None and best.spread == pytest.approx(0.002)
    # with the best one gone, the next disjoint candidate is chosen
    assert pick_extra_leg(COMBO, T0, [c for c in pool if c.position_id != "x4"]).position_id == "x3"
    # nothing disjoint -> None
    assert pick_extra_leg(COMBO, T0, pool[:2]) is None
    # salt rotates among exact ties
    ties = [extra("t1", "epl-liv-mun-2026-10-25", p=0.999, spread=0.002), extra("t2", "epl-tot-eve-2026-10-25", p=0.999, spread=0.002)]
    assert {pick_extra_leg(COMBO, T0, ties, salt=s).position_id for s in (0, 1)} == {"t1", "t2"}


def test_extra_leg_must_not_be_in_the_combo_market_or_group():
    # same condition id as a combo leg (the other outcome of the same market) is same_market, excluded
    pool = [extra("x9", "nba-okc-sas-2026-10-20", cond="c-p1")]
    assert pick_extra_leg(COMBO, T0, pool) is None


def test_back_out_and_error_propagation():
    p, err = back_out(0.2985, 0.995, 0.01)
    assert p == pytest.approx(0.3) and err == pytest.approx(0.3 * 0.005 / 0.995)
    assert disguised_price(0.3, 0.995) == pytest.approx(0.2985)
    # a wider book on the extra leg means a proportionally larger error
    _, err2 = back_out(0.2985, 0.995, 0.02)
    assert err2 == pytest.approx(2 * err)
    with pytest.raises(ValueError):
        back_out(0.3, 0.0, 0.01)
    d = extra("x", "nfl-dal-phi-2026-10-26").as_dict()
    assert d["game_key"] and d["p_L"] == 0.995
