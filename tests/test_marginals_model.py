"""Marginal layer rules with synthetic quotes: every flag, tier, the Kalshi side mapping, in-play staleness."""
import math

import pytest

from rfqbot.marginals.config import MarginalConfig
from rfqbot.marginals.model import (KALSHI, POLY, VenueQuote, combine, estimate_venue,
                                    kalshi_quote, microprice, parse_game_start_ms)
from rfqbot.marginals.sources import KalshiQuote, MatchRow
from rfqbot.marginals import marginal_for_leg
from rfqbot.ladders import MarketQuote

CFG = MarginalConfig()
NOW = 1_000_000_000_000          # 2001-09-09 01:46:40 UTC, in ms
START_FUTURE = NOW + 3_600_000           # kickoff in an hour
START_PAST = NOW - 600_000               # kicked off ten minutes ago


def pq(bid=0.40, ask=0.42, bs=100.0, as_=100.0, bd=1000.0, ad=1000.0, at=NOW - 5000):
    return VenueQuote(POLY, bid, ask, bs, as_, bd, ad, at)


def kq(bid=0.40, ask=0.42, bs=100.0, as_=100.0, bd=1000.0, ad=1000.0, at=NOW - 5000):
    return VenueQuote(KALSHI, bid, ask, bs, as_, bd, ad, at)


def run(poly, kalshi=None, confidence="exact", sport="nfl", now=NOW, start=START_FUTURE, cfg=CFG):
    return combine("pid", poly, kalshi, confidence=confidence, sport=sport, now_ms=now, game_start_ms=start, cfg=cfg)


# ---------------------------------------------------------------- building blocks
def test_microprice_weights_bid_by_ask_size_and_falls_back_to_mid():
    assert microprice(0.40, 0.42, 100, 100) == pytest.approx(0.41)
    assert microprice(0.40, 0.42, 300, 100) == pytest.approx(0.415)      # heavy bid pushes the price up
    assert microprice(0.40, 0.42, None, 100) == pytest.approx(0.41)
    assert microprice(0.40, 0.42, 0, 0) == pytest.approx(0.41)


def test_sigma_formula():
    e = estimate_venue(pq(bid=0.40, ask=0.44, bd=500.0, ad=500.0, at=NOW - 100_000), now_ms=NOW, inplay=False, cfg=CFG)
    hs, depth, stale = 0.02, 500.0, 100.0
    expect = hs * (1 + CFG.c_thin / (1 + depth / CFG.d0)) + CFG.c_stale_pregame * stale
    assert e.sigma == pytest.approx(expect) and e.precision == pytest.approx(1 / expect ** 2)
    assert e.half_spread == pytest.approx(0.02) and e.depth == 500.0 and e.staleness_s == 100.0 and e.usable
    # depth is the thinner side, falling back to size at best when depth is missing
    assert estimate_venue(pq(bd=50.0, ad=900.0), now_ms=NOW, inplay=False, cfg=CFG).depth == 50.0
    assert estimate_venue(pq(bs=7.0, as_=9.0, bd=None, ad=None), now_ms=NOW, inplay=False, cfg=CFG).depth == 7.0


def test_kalshi_side_mapping():
    k = KalshiQuote(yes_bid=0.30, yes_ask=0.34, bid_size=10, ask_size=20, bid_depth=100, ask_depth=200, at_ms=5)
    y = kalshi_quote(k, "yes")
    assert (y.bid, y.ask, y.bid_size, y.ask_size, y.bid_depth, y.ask_depth, y.at_ms) == (0.30, 0.34, 10, 20, 100, 200, 5)
    n = kalshi_quote(k, "no")
    assert n.bid == pytest.approx(0.66) and n.ask == pytest.approx(0.70)
    assert (n.bid_size, n.ask_size, n.bid_depth, n.ask_depth) == (20, 10, 200, 100)
    # p_no == 1 - p_yes under the microprice too
    ey = estimate_venue(y, now_ms=NOW, inplay=False, cfg=CFG)
    en = estimate_venue(n, now_ms=NOW, inplay=False, cfg=CFG)
    assert en.p == pytest.approx(1 - ey.p) and en.sigma == pytest.approx(ey.sigma)
    half = KalshiQuote(yes_bid=None, yes_ask=0.34, bid_size=None, ask_size=20, bid_depth=None, ask_depth=200, at_ms=5)
    assert kalshi_quote(half, "no").ask is None and kalshi_quote(half, "no").bid == pytest.approx(0.66)
    with pytest.raises(ValueError):
        kalshi_quote(k, "maybe")


def test_parse_game_start():
    assert parse_game_start_ms("2026-10-23 00:15:00+00") == 1_792_714_500_000
    assert parse_game_start_ms("2026-10-23T00:15:00Z") == 1_792_714_500_000
    assert parse_game_start_ms(None) is None and parse_game_start_ms("tbd") is None


# ---------------------------------------------------------------- combination
def test_exact_match_combines_by_precision_with_kalshi_prior():
    m = run(pq(0.40, 0.42), kq(0.42, 0.44))
    ep, ek = m.venues
    assert m.tier == "ok" and m.flags == ()
    assert ek.precision == pytest.approx(2.0 / ek.sigma ** 2)                 # nfl prior 2.0
    total = ep.precision + ek.precision
    assert m.p == pytest.approx((ep.precision * 0.41 + ek.precision * 0.43) / total)
    assert m.err == pytest.approx(max(1 / math.sqrt(total), CFG.err_floor))
    assert m.w_poly == pytest.approx(ep.precision / total) and m.w_kalshi == pytest.approx(ek.precision / total)
    assert m.w_poly + m.w_kalshi == pytest.approx(1.0) and m.w_kalshi > m.w_poly
    assert m.p_poly == pytest.approx(0.41) and m.p_kalshi == pytest.approx(0.43) and m.at_ms == NOW - 5000
    assert "+" in m.explain and "x2" in m.explain


def test_prior_is_per_sport():
    nfl = run(pq(0.40, 0.42), kq(0.42, 0.44), sport="nfl")
    other = run(pq(0.40, 0.42), kq(0.42, 0.44), sport="mlb")
    assert nfl.w_kalshi > other.w_kalshi and other.w_kalshi == pytest.approx(0.5)


def test_rules_differ_adds_a_bias_variance_to_kalshi_and_flags():
    exact = run(pq(0.40, 0.42), kq(0.42, 0.44))
    rd = run(pq(0.40, 0.42), kq(0.42, 0.44), confidence="rules_differ")
    assert "rules_differ" in rd.flags and rd.tier == "ok"
    prior, sigma = CFG.prior_for("nfl"), rd.venues[1].sigma
    assert exact.venues[1].precision == pytest.approx(prior / sigma ** 2)
    assert rd.venues[1].precision == pytest.approx(prior / (sigma ** 2 + CFG.rules_differ_bias ** 2))
    assert rd.w_kalshi < exact.w_kalshi and rd.w_kalshi > 0


def test_rules_differ_props_do_not_combine():
    mq = MarketQuote(position_id="p", token_id="t", slug="nba-dal-hou-2026-10-21-rebounds-cooper-flagg-6pt5", title="", outcome="Over",
                     outcome_index=0, best_bid=0.40, best_ask=0.42, bid_size=100.0, ask_size=100.0, bid_depth=1000.0, ask_depth=1000.0,
                     at_ms=NOW - 5000, game_start_time=None)
    match = MatchRow("KX", "yes", "rules_differ", ())
    kqt = KalshiQuote(0.50, 0.52, 100.0, 100.0, 1000.0, 1000.0, NOW - 5000)
    m = marginal_for_leg(mq, match, kqt, sport="nba", now_ms=NOW, cfg=CFG)
    assert "kalshi_unused:props_rules" in m.flags and m.w_kalshi == 0.0 and m.p == pytest.approx(0.41)


def test_locked_or_crossed_book_is_unusable_not_a_zero_width_one():
    # the reviewer's repro: Poly 0.40/0.40 beside Kalshi 0.45/0.46 used to give p = 0.40 with w_poly = 1
    m = run(pq(0.40, 0.40), kq(0.45, 0.46))
    assert "crossed:poly" in m.flags and m.w_poly == 0.0 and m.p == pytest.approx(0.455, abs=0.002)
    assert run(pq(0.43, 0.41), None, confidence="none").tier == "skip"
    alone = run(pq(0.40, 0.40), None, confidence="none")
    assert alone.p is None and alone.tier == "skip" and "crossed:poly" in alone.flags


def test_half_spread_is_floored_at_half_a_tick():
    e = estimate_venue(pq(0.4995, 0.5005, bd=1e6, ad=1e6), now_ms=NOW, inplay=False, cfg=CFG)
    assert e.half_spread == pytest.approx(0.0005) and e.sigma >= 0.0005
    ek = estimate_venue(kq(0.499, 0.501, bd=1e6, ad=1e6), now_ms=NOW, inplay=False, cfg=CFG)
    assert ek.half_spread == pytest.approx(0.005)


def test_config_rejects_non_positive_kalshi_prior():
    with pytest.raises(ValueError):
        MarginalConfig(kalshi_prior={"nba": 0.0})
    with pytest.raises(ValueError):
        MarginalConfig(kalshi_prior_default=-1.0)


def test_microprice_shift_is_shrunk_when_the_book_is_thin():
    thick = estimate_venue(pq(0.40, 0.42, bs=300.0, as_=100.0, bd=5000.0, ad=5000.0), now_ms=NOW, inplay=False, cfg=CFG)
    thin = estimate_venue(pq(0.40, 0.42, bs=300.0, as_=100.0, bd=20.0, ad=20.0), now_ms=NOW, inplay=False, cfg=CFG)
    assert 0.41 < thin.p < thick.p < 0.415 + 1e-9


def test_err_is_floored_at_rho_times_the_best_sigma():
    cfg = MarginalConfig(kalshi_prior={"nfl": 100.0})                            # a huge prior would drive 1/sqrt(sum prec) far below sigma
    m = run(pq(0.40, 0.42), kq(0.40, 0.42), cfg=cfg)
    best = min(v.sigma for v in m.venues)
    assert m.err >= cfg.err_rho * best - 1e-12 and 1 / math.sqrt(sum(v.precision for v in m.venues)) < cfg.err_rho * best


@pytest.mark.parametrize("conf", ["shifted_line", "none"])
def test_shifted_line_and_none_are_poly_only(conf):
    m = run(pq(0.40, 0.42), kq(0.50, 0.52), confidence=conf)
    assert m.p == pytest.approx(0.41) and m.w_poly == 1.0 and m.w_kalshi == 0.0 and m.p_kalshi is None
    assert f"kalshi_unused:{conf}" in m.flags and m.tier == "ok" and len(m.venues) == 1


def test_exact_match_without_a_kalshi_quote():
    m = run(pq(0.40, 0.42), None, confidence="exact")
    assert m.p == pytest.approx(0.41) and "kalshi_unused:no_quote" in m.flags and m.w_kalshi == 0.0
    m = run(pq(0.40, 0.42), kq(None, None, at=NOW), confidence="exact")
    assert "no_quote:kalshi" in m.flags and m.w_kalshi == 0.0 and m.tier == "ok"


def test_unknown_confidence_rejected():
    with pytest.raises(ValueError):
        run(pq(), kq(), confidence="fuzzy")


# ---------------------------------------------------------------- flags and tiers
def test_wide_is_a_note_only():
    narrow = run(pq(0.40, 0.42), None, confidence="none")
    wide = run(pq(0.30, 0.52), None, confidence="none")
    assert "wide:poly" in wide.flags and wide.tier == "wide" and "wide:poly" not in narrow.flags
    assert wide.err == pytest.approx(min(wide.venues[0].sigma, CFG.err_cap))  # nothing added on top of sigma
    # threshold: spread > k_wide * p(1-p) + floor; at p=0.5 that is 0.07
    just_under = run(pq(0.465, 0.535), None, confidence="none")
    just_over = run(pq(0.46, 0.54), None, confidence="none")
    assert "wide:poly" not in just_under.flags and "wide:poly" in just_over.flags


def test_thin_is_a_note_only():
    m = run(pq(bd=10.0, ad=10.0), None, confidence="none")
    assert "thin:poly" in m.flags and m.tier == "thin"
    assert m.err == pytest.approx(max(m.venues[0].sigma, CFG.err_floor))
    assert m.venues[0].sigma > run(pq(), None, confidence="none").venues[0].sigma   # thinness inflates sigma


def test_stale_flags_then_drops_the_venue():
    fresh = run(pq(0.40, 0.42), kq(0.42, 0.44, at=NOW - 5000))
    warned = run(pq(0.40, 0.42), kq(0.42, 0.44, at=NOW - 700_000))              # > 600 s warn
    assert "stale:kalshi" in warned.flags and warned.tier == "stale" and warned.w_kalshi > 0
    assert warned.w_kalshi < fresh.w_kalshi                                       # staleness flows through sigma
    dropped = run(pq(0.40, 0.42), kq(0.42, 0.44, at=NOW - 4_000_000))            # > 3600 s drop
    assert "stale:kalshi" in dropped.flags and dropped.w_kalshi == 0.0 and dropped.p == pytest.approx(0.41)
    assert dropped.tier == "ok" and dropped.venues[1].reason == "stale_drop"      # unused venue does not set the tier
    assert dropped.at_ms == NOW - 5000


def test_confirmation_measures_staleness_from_the_feed_heartbeat():
    old = pq(0.40, 0.42, at=NOW - 4_000_000)                                      # row > drop threshold
    assert run(old, None, confidence="none").p is None
    confirmed = VenueQuote(POLY, 0.40, 0.42, 100.0, 100.0, 1000.0, 1000.0, NOW - 4_000_000, confirmed_ms=NOW - 2000)
    m = run(confirmed, None, confidence="none")
    assert m.p == pytest.approx(0.41) and m.tier == "ok" and "stale:poly" not in m.flags
    assert m.venues[0].staleness_s == 2.0 and m.venues[0].age_s == 4000.0 and m.at_ms == NOW - 4_000_000
    # a confirmation older than the row, or in the future, changes nothing
    e = estimate_venue(VenueQuote(POLY, 0.40, 0.42, 1, 1, 1, 1, NOW - 5000, confirmed_ms=NOW - 9000), now_ms=NOW, inplay=False, cfg=CFG)
    assert e.staleness_s == 5.0
    e = estimate_venue(VenueQuote(POLY, 0.40, 0.42, 1, 1, 1, 1, NOW - 5000, confirmed_ms=NOW + 9000), now_ms=NOW, inplay=False, cfg=CFG)
    assert e.staleness_s == 0.0
    # in play: a confirmed quiet quote is fresh
    live = run(VenueQuote(POLY, 0.40, 0.42, 1, 1, 1000, 1000, NOW - 300_000, confirmed_ms=NOW - 1000), None, confidence="none", start=START_PAST)
    assert live.tier == "ok" and "inplay" in live.flags


def test_tier_follows_the_venues_with_weight():
    # Poly is wide (hs 0.16) and nearly weightless next to a tight Kalshi book: tier ok, flag kept as a note
    m = run(pq(0.10, 0.42), kq(0.25, 0.26, bd=10000.0, ad=10000.0))
    assert "wide:poly" in m.flags and m.w_poly < 0.05 and m.tier == "ok"
    low = MarginalConfig(tier_w_min=0.0)
    assert run(pq(0.10, 0.42), kq(0.25, 0.26, bd=10000.0, ad=10000.0), cfg=low).tier == "wide"


def test_everything_stale_is_no_quote():
    m = run(pq(at=NOW - 4_000_000), kq(at=NOW - 4_000_000))
    assert m.p is None and m.err is None and m.tier == "skip" and "no_quote" in m.flags
    assert m.w_poly == 0.0 and m.w_kalshi == 0.0 and m.at_ms == NOW - 4_000_000


def test_inplay_staleness_scale_and_tier():
    pre = run(pq(at=NOW - 30_000), None, confidence="none", start=START_FUTURE)
    live = run(pq(at=NOW - 30_000), None, confidence="none", start=START_PAST)
    assert "inplay" not in pre.flags and "inplay" in live.flags
    assert live.venues[0].sigma > pre.venues[0].sigma                            # c_stale_inplay >> c_stale_pregame
    assert live.venues[0].sigma == pytest.approx(pre.venues[0].sigma - CFG.c_stale_pregame * 30 + CFG.c_stale_inplay * 30)
    assert "stale:poly" in live.flags and live.tier == "stale"                    # 30 s > 15 s in-play warn
    fresh_live = run(pq(at=NOW - 5_000), None, confidence="none", start=START_PAST)
    assert fresh_live.tier == "ok" and "inplay" in fresh_live.flags and "stale:poly" not in fresh_live.flags
    # in play, an unflagged quote older than inplay_fresh_s still makes the tier stale
    cfg = MarginalConfig(stale_warn_inplay_s=60.0, inplay_fresh_s=15.0)
    m = run(pq(at=NOW - 30_000), None, confidence="none", start=START_PAST, cfg=cfg)
    assert "stale:poly" not in m.flags and m.tier == "stale"
    gone = run(pq(at=NOW - 90_000), None, confidence="none", start=START_PAST)    # > 60 s in-play drop
    assert gone.p is None and gone.tier == "skip"
    kickoff = run(pq(at=NOW - 1000), None, confidence="none", start=NOW)         # game_start_time <= now is in play
    assert "inplay" in kickoff.flags


def test_disagree_keeps_the_weighted_p_and_err_at_least_gap():
    m = run(pq(0.40, 0.42), kq(0.46, 0.48))                                     # gap 0.06 > 2 * hypot(sigma_p, sigma_k) ~ 0.038
    assert "disagree" in m.flags and "disagree_hard" not in m.flags and m.tier == "disagree"
    w_k = m.w_kalshi
    assert 0 < w_k < 1 and m.p == pytest.approx(0.41 * (1 - w_k) + 0.47 * w_k) and m.err >= 0.06 - 1e-12
    assert m.p_kalshi == pytest.approx(0.47) and "venues disagree" in m.explain
    agree = run(pq(0.40, 0.42), kq(0.415, 0.435))                               # gap 0.015 inside the sigma band
    assert "disagree" not in agree.flags and agree.w_kalshi > 0
    wide_gap = run(pq(0.40, 0.42), kq(0.42, 0.44))
    assert "disagree" not in wide_gap.flags and wide_gap.w_kalshi > 0
    # the test scales with the venues' own sigma: the same 0.06 gap between thin books is not a disagreement
    noisy = run(pq(0.34, 0.48, bd=10.0, ad=10.0), kq(0.40, 0.54, bd=10.0, ad=10.0))
    assert "disagree" not in noisy.flags


def test_disagree_hard_skips_but_reports_p():
    m = run(pq(0.40, 0.42), kq(0.60, 0.62))                                     # gap 0.20 > gap_skip 0.10
    assert {"disagree", "disagree_hard"} <= set(m.flags) and m.tier == "skip"
    assert m.err == pytest.approx(0.20) and 0 < m.w_kalshi < 1 and 0.41 < m.p < 0.61


def test_degenerate_raises_err_floor():
    m = run(pq(0.99, 1.0), None, confidence="none")
    assert "degenerate" in m.flags and m.err >= CFG.degenerate_err_floor
    lo = run(pq(0.0, 0.01), None, confidence="none")
    assert "degenerate" in lo.flags and lo.p < 0.015
    assert "degenerate" not in run(pq(0.40, 0.42), None, confidence="none").flags


def test_one_sided_venue_is_skipped_when_the_other_is_two_sided():
    m = run(pq(0.40, 0.42), kq(0.43, None))
    assert "one_sided:kalshi" in m.flags and m.w_kalshi == 0.0 and m.p == pytest.approx(0.41) and m.tier == "ok"
    m = run(pq(None, 0.42), kq(0.42, 0.44))
    assert "one_sided:poly" in m.flags and m.w_poly == 0.0 and m.w_kalshi == 1.0 and m.p == pytest.approx(0.43)


def test_lone_one_sided_venue_uses_present_side_with_large_sigma():
    m = run(pq(0.56, None), None, confidence="none")
    assert m.p == pytest.approx(0.56) and m.err == pytest.approx(CFG.one_sided_sigma, abs=1e-3) and m.tier == "wide"
    assert "one_sided:poly" in m.flags and m.w_poly == 1.0
    m = run(pq(None, 0.995), None, confidence="none")
    assert "degenerate" in m.flags and m.p == pytest.approx(0.995)
    both = run(pq(0.56, None), kq(None, 0.60))                                  # both one-sided: poly first
    assert both.p == pytest.approx(0.56) and both.w_poly == 1.0 and both.w_kalshi == 0.0


def test_no_quote_at_all():
    m = run(None, None, confidence="none")
    assert m.p is None and m.err is None and m.tier == "skip" and {"no_quote", "no_quote:poly"} <= set(m.flags)
    m = run(pq(None, None, at=NOW), None, confidence="exact")
    assert m.p is None and "no_quote:poly" in m.flags


def test_err_floor_and_cap():
    tight = run(pq(0.499, 0.501, bd=1e6, ad=1e6), kq(0.499, 0.501, bd=1e6, ad=1e6))   # one-tick books
    assert tight.err == pytest.approx(CFG.err_floor)
    huge = run(pq(0.01, 0.99), None, confidence="none")
    assert huge.err == pytest.approx(CFG.err_cap)


def test_tier_priority_and_flags_unique():
    m = run(pq(0.40, 0.42, bd=10.0, ad=10.0), kq(0.60, 0.62, at=NOW - 700_000))   # thin, stale kalshi, hard disagree
    assert m.tier == "skip"
    assert len(m.flags) == len(set(m.flags))
    m = run(pq(0.30, 0.52, bd=10.0, ad=10.0), kq(0.42, 0.44, at=NOW - 700_000))   # stale beats wide beats thin
    assert m.tier == "stale"


# ---------------------------------------------------------------- producer objects
def _mq(bid, ask, at=NOW - 5000, start="2001-09-09 01:46:40+00", pid="p1"):
    return MarketQuote(pid, "tok", "nfl-x-y-2026-10-25", "t", "X", 0, bid, ask, 10.0, 10.0, 100.0, 100.0, at, start)


def test_marginal_for_leg_with_match_row_and_kalshi_quote():
    k = KalshiQuote(0.56, 0.58, 10, 10, 100, 100, NOW - 1000)                   # YES book; the leg is the NO side
    row = MatchRow("KXNFLGAME-X", "no", "exact", ())
    m = marginal_for_leg(_mq(0.41, 0.43), row, k, sport="nfl", now_ms=NOW, cfg=CFG)
    assert m.position_id == "p1" and m.p_kalshi == pytest.approx(0.43) and m.kalshi_confidence == "exact"
    assert m.w_kalshi > 0 and m.tier == "ok"
    m = marginal_for_leg(_mq(0.41, 0.43), None, None, sport="nfl", now_ms=NOW, cfg=CFG)
    assert m.kalshi_confidence == "none" and "kalshi_unused:none" in m.flags
    # game_start_time is parsed for the in-play decision: 2001-09-09 01:46:40 UTC == 1e9 s
    live = marginal_for_leg(_mq(0.41, 0.43, start="2001-09-09 01:46:40+00"), None, None, sport="nfl", now_ms=NOW, cfg=CFG)
    assert "inplay" in live.flags
    pre = marginal_for_leg(_mq(0.41, 0.43, start="2001-09-09 01:46:41+00"), None, None, sport="nfl", now_ms=NOW, cfg=CFG)
    assert "inplay" not in pre.flags
