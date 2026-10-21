import json
import os
import time

import pytest

from rfqbot.edge import MarginTerms, Segment, margin_for
from rfqbot.edge.config import EdgeConfig, load_edge_config
from rfqbot.edge.table import MarginTable, load_table, write_table


def node(seg, m_star=0.02, base=0.015, curse_mult=0.8, info=0.0, source="model", n=100):
    return {"segment": seg, "level": Segment.parse(seg).level, "n": n, "n_printed": 20, "sigma": 0.006, "delta": 0.03,
            "m_star": m_star, "f_star": 0.4, "curse_star": 0.005, "z_star": 1.0, "curse_mult": curse_mult, "base": base,
            "profit_fill": 0.015, "profit_req": 0.006, "info_premium": info, "constraint_hit": False, "source": source}


def wallet(pid, premium=0.0, blocked=False, until=None, reason=""):
    return {"public_id": pid, "n": 10, "n_wins": 2, "mean": -0.01, "sd": 0.02, "shrunk": -0.004, "se": 0.005, "upper": 0.002,
            "lower": -0.01, "blocked": blocked, "blocked_until_ms": until, "reason": reason, "premium": premium, "manual": "", "last_ms": 0}


def test_write_load_and_ladder_resolution(tmp_path):
    cfg = EdgeConfig(default_edge=0.005)
    path = tmp_path / "margin_table.json"
    nodes = [node("nba|2|BUY|disjoint|s1", m_star=0.02, base=0.015, curse_mult=0.8),
             node("nba|2|BUY|disjoint|*", m_star=0.018, base=0.014, curse_mult=0.7, info=0.003),
             node("*|*|SELL|*|*", m_star=0.01, base=0.008, curse_mult=0.3, info=0.006),
             node("*|*|*|*|*", m_star=0.012, base=0.01, curse_mult=0.4),
             node("nfl|2|BUY|disjoint|*", source="no_prints", n=40)]
    wallets = {"w_prem": wallet("w_prem", premium=0.004), "w_blk": wallet("w_blk", blocked=True, until=10 ** 15, reason="rule"),
               "w_old": wallet("w_old", blocked=True, until=1, reason="rule"), "w_zero": wallet("w_zero")}
    data = write_table(path, nodes, wallets, cfg, fitted_at_ms=123)
    assert path.exists() and data["fitted_at_ms"] == 123 and set(data["wallets"]) == {"w_prem", "w_blk", "w_old"}
    t = load_table(path, max_age_s=0)
    assert isinstance(t, MarginTable) and t.fitted_at_ms == 123
    # exact cell
    m = margin_for("nba|2|BUY|disjoint|s1", table=t)
    assert m == MarginTerms(0.015, 0.8, 0.0, 0.0, False, "nba|2|BUY|disjoint|s1", 0, 100, "model")
    assert m.margin(0.006) == pytest.approx(0.015 + 0.8 * 0.006)
    # pooled: sigma dropped
    m = margin_for(Segment.parse("nba|2|BUY|disjoint|s2"), table=t)
    assert m.segment == "nba|2|BUY|disjoint|*" and m.level == 1 and m.info_premium == 0.003
    # pooled up to the direction node, then global
    assert margin_for("nba|5+|SELL|same_game|s0", table=t).segment == "*|*|SELL|*|*"
    assert margin_for("nhl|2|BUY|disjoint|s0", table=t).segment == "*|*|*|*|*"
    # a node fitted without prints hands back the default edge, flagged
    m = margin_for("nfl|2|BUY|disjoint|s1", table=t)
    assert m.source == "no_prints" and m.base == 0.005 and m.curse_mult == 0.0 and m.segment == "nfl|2|BUY|disjoint|*"
    # wallets: premium, active block, expired block
    assert margin_for("nba|2|BUY|disjoint|s1", requester="w_prem", table=t).wallet_premium == 0.004
    b = margin_for("nba|2|BUY|disjoint|s1", requester="w_blk", table=t)
    assert b.blocked and b.block_reason == "rule" and b.wallet_premium == 0.0
    assert not margin_for("nba|2|BUY|disjoint|s1", requester="w_old", table=t, now_ms=2).blocked
    assert margin_for("nba|2|BUY|disjoint|s1", requester="unknown", table=t).wallet_premium == 0.0


def test_margin_for_without_table_and_cache_refresh(tmp_path):
    missing = tmp_path / "none.json"
    assert margin_for("nba|2|BUY|disjoint|s1", path=missing, default_edge=0.0075) == MarginTerms(0.0075, 0.0, 0.0, 0.0, False)
    path = tmp_path / "t.json"
    cfg = EdgeConfig()
    write_table(path, [node("*|*|*|*|*", base=0.01)], {}, cfg, fitted_at_ms=1)
    assert margin_for("nba|2|BUY|disjoint|s1", path=path).base == 0.01
    time.sleep(0.01)
    write_table(path, [node("*|*|*|*|*", base=0.02)], {}, cfg, fitted_at_ms=2)
    os.utime(path, (time.time() + 5, time.time() + 5))     # make sure the mtime differs
    assert load_table(path, max_age_s=0).fitted_at_ms == 2
    assert margin_for("nba|2|BUY|disjoint|s1", path=path).base == 0.02
    # a corrupt file degrades to the default
    path.write_text("{not json")
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert load_table(path, max_age_s=0) is None               # margin_for keeps a 1 s cache, so force the re-read
    assert margin_for("nba|2|BUY|disjoint|s1", path=path).source == "default"


def test_segment_key_roundtrip_and_errors():
    s = Segment.of("nfl", 4, "SELL", True, 0.02, (0.005, 0.009))
    assert s.key == "nfl|3-4|SELL|same_game|s2" and Segment.parse(s.key) == s
    with pytest.raises(ValueError):
        Segment.parse("nfl|SELL")


def test_config_env_overrides():
    cfg = load_edge_config(env_file=None, env={"EDGE_BLOCK_MIN_FILLS": "12", "EDGE_BLOCK_DAYS": "3", "EDGE_WALLET_K": "2",
                                               "EDGE_MIN_FILL_RATE": "0.2", "EDGE_SIGMA_CUTS": "0.004,0.01", "EDGE_HORIZONS_MIN": "5,30",
                                               "QUOTE_DECISIONS_DB": "x/decisions.db", "QUOTE_BASE_EDGE": "0.004", "EDGE_CURSE_SOURCE": "Measured"})
    assert cfg.block_min_fills == 12 and cfg.block_days == 3.0 and cfg.wallet_k == 2.0 and cfg.min_fill_rate == 0.2
    assert cfg.sigma_cuts == (0.004, 0.01) and cfg.horizons_min == (5, 30) and str(cfg.decisions_db) == "x/decisions.db"
    assert cfg.default_edge == 0.004 and cfg.curse_source == "measured"
    assert cfg.grid()[:3] == [0.0, 0.0025, 0.005] and cfg.grid()[-1] == 0.1
    json.loads(cfg.to_json())
