"""Scorer on the recorded history fixture (one real NFL event, resolved) with synthetic snapshots."""
import json
from pathlib import Path

import pytest

from rfqbot.history import HistoryStore
from rfqbot.history.pairs import build_pairs
from rfqbot.history.pull import ingest_event
from rfqbot.pricing.validate import Validator, family_pair, format_report, worst_family_pairs

FX = json.loads((Path(__file__).parent / "fixtures" / "history" / "nfl_event.json").read_text())


@pytest.fixture
def history_db(tmp_path):
    path = tmp_path / "history.db"
    hs = HistoryStore(path)
    prefix, n, n_res = ingest_event(hs, "nfl", FX["main"], at_ms=1)
    assert prefix and n == len(FX["main"]["markets"]) and n_res == n
    # synthetic snapshots: a winning leg priced 0.62, a losing one 0.38 (both sides of every market)
    kick = 1_760_000_000
    for r in hs.rows("SELECT token_id, won FROM hist_legs WHERE game_prefix=?", prefix):
        if r["token_id"]:
            hs.insert_prices(r["token_id"], {15: (kick - 900, 0.62 if r["won"] else 0.38)})
    built = build_pairs(hs, sport="nfl")
    assert sum(built.values()) > 0
    hs.close()
    return path, prefix


def test_validator_scores_fixture_game(history_db):
    path, prefix = history_db
    v = Validator(path, None, max_pairs_per_game=50, seed=1)
    gv = v.validate_game(prefix, 15)
    assert gv.ok, gv.error
    assert gv.n_quotes > 0 and gv.n_pairs_scored > 0 and gv.n_pairs_scored <= 50
    assert "M" in gv.fitted_fg                       # spread ladder present in the fixture
    rows = v.out.execute("SELECT * FROM hist_pair_scores").fetchall()
    assert len(rows) == gv.n_pairs_scored
    for r in rows:
        assert 0 <= r["p_joint"] <= 1 and 0 <= r["p_indep"] <= 1 and r["won"] in (0.0, 1.0)
        assert r["lo"] - 1e-9 <= r["p_joint"] <= r["hi"] + 1e-9
        assert r["family_pair"] == family_pair(*r["family_pair"].replace(" x ", "/").split("/"))
    legs = v.out.execute("SELECT COUNT(*) FROM hist_leg_scores").fetchone()[0]
    assert legs > 0
    res = v.report(15)
    assert res["games_ok"] == 1 and res["pairs"] == gv.n_pairs_scored and "overall" in res and "legs" in res
    assert set(res["overall"]) >= {"brier_engine", "brier_indep", "logloss_engine", "bias_indep"}
    assert res["legs"]["n_half"] == 0
    text = format_report(res)
    assert "by family pair" in text and "single legs" in text
    worst_family_pairs(res, min_n=1)
    # idempotent re-run
    gv2 = v.validate_game(prefix, 15)
    assert v.out.execute("SELECT COUNT(*) FROM hist_pair_scores").fetchone()[0] == gv2.n_pairs_scored


def test_validator_reports_missing_snapshots(history_db):
    path, prefix = history_db
    v = Validator(path, None)
    gv = v.validate_game(prefix, 60)      # no snapshots at 60 min in the fixture
    assert not gv.ok and "no snapshot" in gv.error
    res = v.report(60)
    assert res["games_ok"] == 0 and res["failures"] == [(prefix, gv.error)]
