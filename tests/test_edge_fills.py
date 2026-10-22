from rfqbot.edge.data import DecisionRec, LegInfo, Print
from rfqbot.edge.fills import actual_markup, adverse_round_trips, competitor_gap, counterfactual, fill_fraction, would_win

E6 = 1_000_000


def dec(i, direction, p_fair, price, size=100 * E6, rfq=None, n_legs=2, u_m=0.005, requester="w1"):
    legs = [LegInfo("p1", "nba-okc-sas-2026-10-20", 0.5, 0.005), LegInfo("p2", "nba-phi-nyk-2026-10-20", 0.4, 0.005)][:n_legs]
    return DecisionRec(id=i, rfq_id=rfq or f"rfq-{i}", at_ms=1_000_000 + i, direction=direction, requested_unit="notional" if direction == "BUY" else "shares",
                       requested_e6=20 * E6, n_legs=n_legs, p_fair=p_fair, u_m=u_m, u_d=0.0, margin=abs(price - p_fair), price=price,
                       size_e6=size, legs=legs, requester=requester)


def prt(rfq, direction, price, size=100 * E6, at=1_010_000, requester="w1", cond="0xc"):
    return Print(rfq_id=rfq, direction=direction, price=price, size_e6=size, executed_at_ms=at, requester=requester, n_legs=2,
                 position_ids=["p1", "p2"], prefixes=["nba-okc-sas-2026-10-20", "nba-phi-nyk-2026-10-20"], condition_id=cond)


def test_adverse_round_trip_is_a_fill_plus_an_opposite_print_on_the_same_combo_within_10s():
    d = dec(1, "BUY", 0.30, 0.31)                                    # we quoted 0.31, print at 0.33: a win
    p = prt("rfq-1", "BUY", 0.33, at=1_000_000)
    fills = counterfactual([d], [p])
    assert fills[0].win_actual
    # the same wallet sells the same combo 8 s later -> adverse round trip on rfq-1
    unwind = prt("rfq-9", "SELL", 0.29, at=1_008_000)
    assert adverse_round_trips(fills, [p, unwind]) == {"rfq-1"}
    # not: a different wallet, a different combo, the same direction, or outside the window
    assert adverse_round_trips(fills, [p, prt("rfq-9", "SELL", 0.29, at=1_008_000, requester="w2")]) == set()
    assert adverse_round_trips(fills, [p, prt("rfq-9", "SELL", 0.29, at=1_008_000, cond="0xother")]) == set()
    assert adverse_round_trips(fills, [p, prt("rfq-9", "BUY", 0.33, at=1_008_000)]) == set()
    assert adverse_round_trips(fills, [p, prt("rfq-9", "SELL", 0.29, at=1_020_001)]) == set()
    # a losing quote (we did not fill) is never a round trip, whatever the wallet did next
    lost = counterfactual([dec(2, "BUY", 0.30, 0.35)], [prt("rfq-2", "BUY", 0.33, at=1_000_000)])
    assert adverse_round_trips(lost, [prt("rfq-2", "BUY", 0.33, at=1_000_000), unwind]) == set()


def test_would_win_buy_lower_ask_wins():
    # requester buys YES: the print is the best ask; ours must be at or below it
    assert would_win("BUY", 0.30, 0.01, 0.31)
    assert would_win("BUY", 0.30, 0.01, 0.32)
    assert not would_win("BUY", 0.30, 0.03, 0.31)


def test_would_win_sell_higher_bid_wins():
    # requester sells YES: the print is the best bid; ours must be at or above it
    assert would_win("SELL", 0.60, 0.01, 0.59)
    assert would_win("SELL", 0.60, 0.02, 0.58)
    assert not would_win("SELL", 0.60, 0.03, 0.59)


def test_competitor_gap_sign_is_in_win_direction():
    assert abs(competitor_gap("BUY", 0.30, 0.32) - 0.02) < 1e-9
    assert abs(competitor_gap("SELL", 0.60, 0.58) - 0.02) < 1e-9
    assert competitor_gap("BUY", 0.30, 0.29) < 0      # print inside our fair: never winnable
    assert competitor_gap("SELL", 0.60, 0.62) < 0


def test_fill_fraction_is_size_permitting():
    assert fill_fraction(100 * E6, 50 * E6) == 1.0
    assert fill_fraction(25 * E6, 50 * E6) == 0.5
    assert fill_fraction(0, 50 * E6) == 0.0
    assert fill_fraction(10 * E6, 0) == 1.0


def test_counterfactual_join_and_grid():
    d_win = dec(1, "BUY", 0.30, 0.31)            # markup 1pt, print at 0.33 -> delta 3pt, win
    d_lose = dec(2, "BUY", 0.30, 0.35)           # markup 5pt, print at 0.33 -> lose
    d_sell = dec(3, "SELL", 0.60, 0.585)         # markup 1.5pt, print at 0.58 -> delta 2pt, win
    d_none = dec(4, "BUY", 0.30, 0.31)           # no print
    d_small = dec(5, "BUY", 0.30, 0.31, size=20 * E6)
    fills = counterfactual([d_win, d_lose, d_sell, d_none, d_small],
                           [prt("rfq-1", "BUY", 0.33), prt("rfq-2", "BUY", 0.33), prt("rfq-3", "SELL", 0.58), prt("rfq-5", "BUY", 0.33, size=80 * E6)])
    by = {f.decision_id: f for f in fills}
    assert by[1].printed and by[1].win_actual and abs(by[1].delta - 0.03) < 1e-9 and by[1].fill_frac == 1.0
    assert abs(by[1].undercut - 0.02) < 1e-9
    assert by[2].printed and not by[2].win_actual and by[2].fill_frac == 0.0
    assert by[3].win_actual and abs(by[3].delta - 0.02) < 1e-9
    assert not by[4].printed and by[4].delta is None and not by[4].win_actual
    assert by[5].win_actual and by[5].fill_frac == 0.25
    grid = [0.0, 0.01, 0.02, 0.03, 0.04]
    assert by[1].wins_on_grid(grid) == [True, True, True, True, False]
    assert by[4].wins_on_grid(grid) == [False] * 5
    # the markup the logged price carries
    assert abs(actual_markup(d_win) - 0.01) < 1e-9 and abs(actual_markup(d_sell) - 0.015) < 1e-9


def test_counterfactual_ignores_direction_mismatch():
    fills = counterfactual([dec(1, "BUY", 0.30, 0.31)], [prt("rfq-1", "SELL", 0.33)])
    assert not fills[0].printed
