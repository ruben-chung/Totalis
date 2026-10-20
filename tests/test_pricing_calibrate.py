import json
import sqlite3

import numpy as np
import pytest

from rfqbot.pricing.calibrate import Calibration, PairParams, TailMap, family_pair_key, fit, format_metrics, group_of, set_default_calibration
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
    grp = PairParams(lam=0.8, lo=None, hi=None, resid=0.02, group="cross")
    pair = PairParams(lam=0.5, lo=TailMap(0.05, 0.01, 1.0), hi=TailMap(0.8, -0.1, 1.1), resid=0.03, group="cross")
    c = Calibration(pairs={"spread/FG x total/2H": pair}, groups={"cross": grp}, identity=False)
    p, lvl = c.apply("spread/FG x total/2H", "cross", 0.3, 0.2)
    assert lvl == "pair" and p == pytest.approx(0.25) and c.model_error("spread/FG x total/2H", "cross") == 0.03
    p, lvl = c.apply("moneyline/1H x total/FG", "cross", 0.3, 0.2)        # unknown pair -> group
    assert lvl == "group" and p == pytest.approx(0.28) and c.model_error("moneyline/1H x total/FG", "cross") == 0.02
    assert c.apply("spread/FG x total/2H", "cross", 0.02, 0.03)[0] == pytest.approx(0.035)   # lo tail: 0.025 -> 0.035
    assert c.apply("spread/FG x total/2H", "cross", 0.95, 0.9)[0] == pytest.approx(-0.1 + 1.1 * 0.925)
    assert c.apply(None, "within_FG", 0.4, 0.3) == (0.4, "none")           # three legs, no group params


def test_load_save_roundtrip(tmp_path):
    c = Calibration(pairs={"a/FG x b/FG": PairParams(0.7, TailMap(0.05, 0.0, 1.0), None, 0.02, 500, 40, 0, 0.6, 0.9, "within_FG")},
                    groups={"within_FG": PairParams(0.9, None, TailMap(0.8, -0.1, 1.1), 0.01, 10000, 0, 300, None, 0.9, "within_FG")},
                    n0=300, n0_tail=100, meta={"x": 1}, identity=False)
    c.save(tmp_path / "c.json")
    d = Calibration.load(tmp_path / "c.json")
    assert not d.identity and d.n0 == 300 and d.pairs["a/FG x b/FG"].lam == 0.7 and d.pairs["a/FG x b/FG"].lo.b == 1.0
    assert d.groups["within_FG"].hi.a == -0.1 and d.pairs["a/FG x b/FG"].lam_pair == 0.6
    assert Calibration.load(tmp_path / "missing.json").identity
    json.loads(d.to_json())


def _scores_db(path, rng, n_games=25, per_game=600):
    """Synthetic scores: two cross pairs with different true lambdas (0.4 and 0.9), one rare pair, one FG pair."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    rows = []
    pairs = [("spread/FG x total/2H", 0.4, 300), ("spread/1H x spread/FG", 0.9, 280), ("moneyline/2H x total/FG", 0.4, 5), ("spread/FG x total/FG", 1.0, 15)]
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


def test_fit_is_hierarchical(tmp_path):
    rng = np.random.default_rng(5)
    db = tmp_path / "scores.db"
    _scores_db(db, rng)
    cal, m = fit(db, 15, folds=4, n0_grid=(100, 1000, 10000), n0_tail_grid=(100, 1000))
    big_a, big_b, rare = cal.pairs["spread/FG x total/2H"], cal.pairs["spread/1H x spread/FG"], cal.pairs["moneyline/2H x total/FG"]
    assert abs(big_a.lam - 0.4) < 0.15 and abs(big_b.lam - 0.9) < 0.15          # well-sampled pairs keep their own lambda
    assert abs(rare.lam - cal.groups["cross"].lam) < abs((rare.lam_pair or rare.lam) - cal.groups["cross"].lam) + 1e-9  # rare pair shrinks to the group
    assert big_a.group == "cross" and rare.n == 125 and cal.n0 in (100, 1000, 10000)
    o = m["overall"]
    assert o["logloss_pair_oos"] < o["logloss_group_oos"] < o["logloss_engine"]     # per-pair beats pooled beats raw
    assert "lam_used" in format_metrics(m) and m["pairs"]["spread/FG x total/2H"]["lambda_pair"] is not None
    assert cal.pairs["spread/FG x total/2H"].resid >= 0


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
