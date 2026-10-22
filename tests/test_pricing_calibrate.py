import json
import sqlite3

import numpy as np
import pytest

from rfqbot.pricing.calibrate import (CALIBRATION_SCHEMA, Calibration, CalibrationError, PairParams, TailMap, engine_config_hash,
                                      family_pair_key, fit, format_metrics, group_of, pair_kind, set_default_calibration)
from rfqbot.pricing.joint import build_game
from rfqbot.pricing.joint_nfl import EngineConfig
from rfqbot.pricing.synthetic import make_truth, quotes_from_truth
from rfqbot.pricing.validate import SCHEMA


def test_keys_and_identity():
    assert group_of("FG", "FG") == "within_FG" and group_of("1H", "1H") == "within_half" and group_of("FG", "2H") == "cross"
    assert family_pair_key("total", "FG", "spread", "FG") == "spread/FG x total/FG" == family_pair_key("spread", "FG", "total", "FG")
    c = Calibration()
    assert c.identity and c.apply("spread/FG x total/FG", "within_FG", 0.3, 0.2) == (0.3, "none") and c.model_error("x", "cross") == 0.0


def test_apply_levels_and_tails():
    grp = PairParams(lam=0.8, lo=None, hi=TailMap(0.8, -0.5), resid=0.02, group="cross")
    pair = PairParams(lam=0.5, lo=TailMap(0.05, -0.4), hi=TailMap(0.8, -0.25), resid=0.03, group="cross")
    c = Calibration(pairs={"spread/FG x total/2H": pair}, groups={"cross": grp}, identity=False)
    p, lvl = c.apply("spread/FG x total/2H", "cross", 0.3, 0.2)
    assert lvl == "pair" and p == pytest.approx(0.25) and c.model_error("spread/FG x total/2H", "cross") == 0.03
    p, lvl = c.apply("moneyline/1H x total/FG", "cross", 0.3, 0.2)        # unknown pair -> group
    assert lvl == "group" and p == pytest.approx(0.28) and c.model_error("moneyline/1H x total/FG", "cross") == 0.02
    assert c.apply("spread/FG x total/2H", "cross", 0.02, 0.03)[0] == pytest.approx(0.025 - 0.4 * (0.025 - 0.05))   # lo hinge
    assert c.apply("spread/FG x total/2H", "cross", 0.95, 0.9)[0] == pytest.approx(0.925 - 0.25 * 0.125)
    assert c.apply(None, "within_FG", 0.4, 0.3) == (0.4, "none")           # three legs, no group params
    # three or more legs: the group lambda only, never the group's tail map
    p3, lvl3 = c.apply(None, "cross", 0.95, 0.9)
    assert lvl3 == "group-extrapolated" and p3 == pytest.approx(0.9 + 0.8 * 0.05)
    assert c.apply("moneyline/1H x total/FG", "cross", 0.95, 0.9)[0] < p3   # the pair-level path does apply the group tail


def test_hinge_tails_are_continuous_at_the_cuts():
    pp = PairParams(lam=1.0, lo=TailMap(0.05, -0.6), hi=TailMap(0.8, -0.8), resid=0.0, group="within_FG")
    c = Calibration(pairs={"spread/FG x total/FG": pp}, identity=False)
    for cut in (0.05, 0.8):
        below = c.apply("spread/FG x total/FG", "within_FG", cut - 1e-4, cut - 1e-4)[0]
        above = c.apply("spread/FG x total/FG", "within_FG", cut + 1e-4, cut + 1e-4)[0]
        assert abs(above - below) < 1e-3, (cut, below, above)
        assert c.apply("spread/FG x total/FG", "within_FG", cut, cut)[0] == pytest.approx(cut)
    # b = -1 collapses the tail onto the cut and never crosses it; the map is monotone for b > -1
    flat = Calibration(pairs={"x/FG x y/FG": PairParams(1.0, TailMap(0.05, -1.0), None, 0.0)}, identity=False)
    assert flat.apply("x/FG x y/FG", "within_FG", 0.0, 0.0)[0] == pytest.approx(0.05)
    ps = [c.apply("spread/FG x total/FG", "within_FG", x, x)[0] for x in np.linspace(0, 1, 201)]
    assert all(b >= a - 1e-12 for a, b in zip(ps, ps[1:]))


def test_load_save_roundtrip(tmp_path):
    c = Calibration(pairs={"a/FG x b/FG": PairParams(0.7, TailMap(0.05, -0.3), None, 0.02, 500, 40, 30, 0, 0.6, 0.1, 0.9, "within_FG", "market_pinned", "informative", "pair", None)},
                    groups={"within_FG": PairParams(0.9, None, TailMap(0.8, -0.1), 0.01, 10000, 90, 0, 300, None, None, 0.9, "within_FG", "group", "group")},
                    meta={"x": 1}, identity=False)
    c.save(tmp_path / "c.json")
    d = Calibration.load(tmp_path / "c.json")
    pp = d.pairs["a/FG x b/FG"]
    assert not d.identity and pp.lam == 0.7 and pp.lo.b == -0.3 and pp.kind == "market_pinned" and pp.lam_status == "informative"
    assert pp.n_games == 40 and pp.lam_ci == pytest.approx((0.6 - 1.96 * 0.1, 0.6 + 1.96 * 0.1)) and pp.lo_source == "pair"
    assert d.groups["within_FG"].hi.b == -0.1 and pp.lam_pair == 0.6
    assert d.meta["schema"] == CALIBRATION_SCHEMA and d.meta["engine_config_hash"] == engine_config_hash() and d.meta["x"] == 1
    assert Calibration.load(tmp_path / "missing.json").identity
    json.loads(d.to_json())


def test_load_refuses_schema_or_engine_config_mismatch(tmp_path):
    c = Calibration(groups={"cross": PairParams(0.8, None, None, 0.0, group="cross")}, identity=False)
    d = json.loads(c.to_json())
    d["meta"]["schema"] = CALIBRATION_SCHEMA - 1
    (tmp_path / "old.json").write_text(json.dumps(d))
    with pytest.raises(CalibrationError, match="schema"):
        Calibration.load(tmp_path / "old.json")
    d = json.loads(c.to_json())
    d["meta"]["engine_config_hash"] = "deadbeef"
    (tmp_path / "cfg.json").write_text(json.dumps(d))
    with pytest.raises(CalibrationError, match="EngineConfig"):
        Calibration.load(tmp_path / "cfg.json")
    # a file without meta at all (the pre-schema format) is refused too, not silently applied
    (tmp_path / "bare.json").write_text(json.dumps({"pairs": {}, "groups": {}}))
    with pytest.raises(CalibrationError):
        Calibration.load(tmp_path / "bare.json")
    assert engine_config_hash(EngineConfig(tie_prob=None)) != engine_config_hash()


def _scores_db(path, rng, n_games=40, per_game=600):
    """Synthetic scores: two prior pairs with different true lambdas (0.4, 0.9), one rare prior pair,
    one market-pinned pair whose true lambda is 1.0, one structural pair."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    rows = []
    pairs = [("spread/FG x total/2H", 0.4, 220), ("spread/1H x spread/FG", 0.9, 220), ("moneyline/2H x total/FG", 0.4, 4),
             ("spread/FG x total/FG", 1.0, 120), ("spread/FG x spread/FG", 0.7, 36)]
    for g in range(n_games):
        i = 0
        for fp, lam, k in pairs:
            for _ in range(k):
                pi = rng.uniform(0.05, 0.6)
                pj = float(np.clip(pi + rng.normal(0, 0.12), 0.01, 0.95))
                won = float(rng.random() < pi + lam * (pj - pi))
                rows.append((f"nfl-g{g}", 15, f"a{i}", f"b{i}", fp, pj, pi, np.sqrt(pi), np.sqrt(pi), 0.5, 0.5, pj - 0.01, pj + 0.01, won, 1.0, won))
                i += 1
    c.executemany("INSERT INTO hist_pair_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()


def test_pair_kinds():
    assert pair_kind("spread/FG x total/FG") == "market_pinned" and pair_kind("spread/FG x team_total/FG") == "market_pinned"
    assert pair_kind("spread/FG x spread/FG") == "structural" and pair_kind("exact_margin/FG x spread/FG") == "structural"
    assert pair_kind("total/FG x total/FG") == "structural" and pair_kind("moneyline/FG x spread/FG") == "structural"
    assert pair_kind("spread/1H x spread/FG") == "prior" and pair_kind("spread/1H x spread/1H") == "prior"


def test_fit_is_hierarchical_and_rule_based(tmp_path):
    rng = np.random.default_rng(5)
    db = tmp_path / "scores.db"
    _scores_db(db, rng)
    cal, m = fit(db, 15, folds=4)
    a, b, rare = cal.pairs["spread/FG x total/2H"], cal.pairs["spread/1H x spread/FG"], cal.pairs["moneyline/2H x total/FG"]
    pinned, struct = cal.pairs["spread/FG x total/FG"], cal.pairs["spread/FG x spread/FG"]
    # precision shrink: the used value lies between the pair's own estimate and the group's
    assert a.kind == "prior" and a.lam_status == "shrunk" and min(a.lam_pair, a.lam_group) - 1e-9 <= a.lam <= max(a.lam_pair, a.lam_group) + 1e-9
    assert abs(a.lam_pair - 0.4) < 0.12 and abs(b.lam_pair - 0.9) < 0.12 and a.lam_se is not None and a.n_games == 40
    assert a.lam < b.lam                                                 # the ordering of the pairs survives the shrink
    assert rare.lam_status == "shrunk" and abs(rare.lam - cal.groups["cross"].lam) < abs(rare.lam_pair - cal.groups["cross"].lam)
    assert pinned.kind == "market_pinned" and pinned.lam_status == "uninformative" and pinned.lam == 1.0    # CI contains 1
    assert struct.kind == "structural" and struct.lam_status == "fixed" and struct.lam == 1.0                # never moved
    lo, hi = pinned.lam_ci
    assert lo <= 1.0 <= hi
    o = m["overall"]
    assert o["logloss_pair_oos"] < o["logloss_engine"]
    assert "lam_used" in format_metrics(m) and m["pairs"]["spread/FG x total/2H"]["lambda_ci"] is not None
    json.loads(cal.to_json())
    # the prior is leave-one-pair-out: the two big prior pairs see different group values
    assert a.lam_group != pytest.approx(b.lam_group, abs=1e-6)
    # every fitted tail map is a hinge anchored at its cut: no jump anywhere
    for fp, pp in cal.pairs.items():
        for x in (0.0499, 0.0501, 0.7999, 0.8001):
            lo_v = cal.apply(fp, pp.group, x - 1e-4, x - 1e-4)[0]
            hi_v = cal.apply(fp, pp.group, x + 1e-4, x + 1e-4)[0]
            assert abs(hi_v - lo_v) < 1e-3, (fp, x)
    assert cal.meta["schema"] == CALIBRATION_SCHEMA and cal.meta["engine_config_hash"] == engine_config_hash()


def test_degenerate_tail_falls_back_to_the_group(tmp_path):
    """A pair whose tail rows all realised 0 (few games) must not win the precision blend with a
    near-zero variance; it takes the group's hinge instead. The shipped file mapped 5% -> 0.03%."""
    rng = np.random.default_rng(3)
    db = tmp_path / "scores.db"
    c = sqlite3.connect(db); c.executescript(SCHEMA)
    rows = []
    for g in range(40):
        for i in range(150):                                   # a healthy cross pair: realised at p
            pi = rng.uniform(0.01, 0.6); pj = float(np.clip(pi + rng.normal(0, 0.1), 0.005, 0.9))
            won = float(rng.random() < pj)
            rows.append((f"nfl-g{g}", 15, f"a{i}", f"b{i}", "spread/1H x spread/FG", pj, pi, np.sqrt(pi), np.sqrt(pi), 0.5, 0.5, pj, pj, won, 1.0, won))
    for g in range(12):                                        # the degenerate pair: 8 tail rows per game, all lost
        for i in range(30):
            pi = rng.uniform(0.01, 0.4); pj = float(np.clip(pi + rng.normal(0, 0.1), 0.005, 0.5))
            won = 0.0 if pj < 0.05 else float(rng.random() < pj)
            rows.append((f"nfl-g{g}", 15, f"c{i}", f"d{i}", "moneyline/1H x spread/FG", pj, pi, np.sqrt(pi), np.sqrt(pi), 0.5, 0.5, pj, pj, won, 1.0, won))
    c.executemany("INSERT INTO hist_pair_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows); c.commit(); c.close()
    cal, _ = fit(db, 15, folds=3)
    bad, good = cal.pairs["moneyline/1H x spread/FG"], cal.pairs["spread/1H x spread/FG"]
    assert bad.n_lo > 20 and bad.lo_source == "group" and good.lo_source == "pair"
    assert bad.lo is not None and bad.lam_status == "shrunk"
    p = cal.apply("moneyline/1H x spread/FG", "cross", 0.03, 0.03)[0]
    assert p > 0.015, p                                        # not collapsed onto zero
    # a healthy tail keeps a Bernoulli-bounded variance: its blended slope stays inside B_CLIP and is finite
    assert good.lo is not None and -1.0 <= good.lo.b <= 1.0


def test_market_pinned_moves_only_when_ci_excludes_one(tmp_path):
    rng = np.random.default_rng(9)
    db = tmp_path / "scores.db"
    c = sqlite3.connect(db); c.executescript(SCHEMA)
    rows = []
    for g in range(60):
        for i in range(150):            # a pinned pair whose realized coupling is clearly absent (true lambda 0)
            pi = rng.uniform(0.1, 0.5); pj = float(np.clip(pi + rng.normal(0, 0.15), 0.01, 0.9))
            won = float(rng.random() < pi)
            rows.append((f"nfl-g{g}", 15, f"a{i}", f"b{i}", "spread/FG x team_total/FG", pj, pi, np.sqrt(pi), np.sqrt(pi), 0.5, 0.5, pj, pj, won, 1.0, won))
    c.executemany("INSERT INTO hist_pair_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows); c.commit(); c.close()
    cal, _ = fit(db, 15, folds=3)
    pp = cal.pairs["spread/FG x team_total/FG"]
    assert pp.lam_status == "informative" and pp.lam < 0.5 and pp.lam_ci[1] < 1.0


def test_joint_quote_carries_pair_calibration():
    pair = PairParams(lam=0.5, lo=None, hi=None, resid=0.03, group="cross")
    grp = PairParams(lam=0.8, lo=None, hi=None, resid=0.02, group="cross")
    cal = Calibration(pairs={"spread/FG x total/1H": pair}, groups={"cross": grp, "within_FG": PairParams(1.0, None, None, 0.01, group="within_FG")}, identity=False)
    m = build_game(quotes_from_truth(make_truth()), config=EngineConfig(tie_prob=None, n_scenarios=20_000))
    legs = {l.spec.venue_ids["slug"] + "|" + l.spec.side: l.spec for l in m.legs}
    fg = legs["nfl-dal-phi-2026-10-27-spread-home-3pt5|PHI"]; h1 = legs["nfl-dal-phi-2026-10-27-1h-total-22pt5|over"]
    q = m.joint([fg, h1], calibration=cal)
    assert q.group == "cross" and q.family_pair == "spread/FG x total/1H" and q.calibration_level == "pair" and q.model_error == 0.03
    eng_ind = q.p_legs[0] * q.p_legs[1]
    assert q.p_cal == pytest.approx(eng_ind + 0.5 * (q.p_joint - eng_ind), abs=1e-9)
    assert q.lo <= q.p_cal - 0.03 + 1e-9 or q.lo == q.frechet_lo
    tt = legs["nfl-dal-phi-2026-10-27-1h-team-total-phi-10pt5|over"]
    q2 = m.joint([fg, tt], calibration=cal)                         # pair unknown -> group fallback
    assert q2.calibration_level == "group" and q2.model_error == 0.02
    assert q2.p_cal == pytest.approx(q2.p_legs[0] * q2.p_legs[1] + 0.8 * (q2.p_joint - q2.p_legs[0] * q2.p_legs[1]), abs=1e-9)
    q0 = m.joint([fg, h1], calibration=Calibration())
    assert q0.p_cal == pytest.approx(q0.p_joint) and not q0.calibrated and q0.calibration_level == "none"
    assert m.joint([fg], calibration=cal).calibration_level == "none"
    set_default_calibration(None)
