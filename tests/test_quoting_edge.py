import json
import os
import time

import pytest

from rfqbot.marginals import Marginal
from rfqbot.messages import RequestedSize, RfqRequest
from rfqbot.pricing.quotes import Quote
from rfqbot.quoting import Assembler, QuoteConfig
from rfqbot.quoting.config import load_quote_config
from rfqbot.quoting.cache import GameCache, GameEntry
from rfqbot.quoting.decisions import DecisionLog

NBA = "nba-gsw-lal-2026-10-21"
NBA2 = "nba-bos-mia-2026-10-21"
NOW = 1_000_000


def marginal(pid, p, err=0.01):
    return Marginal(position_id=pid, p=p, err=err, p_poly=p, p_kalshi=None, w_poly=1.0, w_kalshi=0.0,
                    kalshi_confidence="none", flags=(), tier="ok", at_ms=1, explain="")


def nba_entry(prefix, pid):
    q = Quote(pid, "t" + pid, f"{prefix}-spread-home-6pt5", "Spread: Home (-6.5)", "Home", 0, 0.49, 0.51, 500, 500, at_ms=1)
    e = GameEntry(prefix=prefix, sport="nba", quotes=[q], refreshed_at_ms=1)
    e.marginals[pid] = (q, None, marginal(pid, 0.5, 0.01))
    return e


class FakeStore:
    def __init__(self, entries):
        self.rows = {q.position_id: {"position_id": q.position_id, "condition_id": "c" + q.position_id, "slug": q.slug, "outcome": q.outcome}
                     for e in entries for q in e.quotes}

    def get_market(self, pid):
        return self.rows.get(pid)


def table(path, base, curse, *, wallets=None, fitted_at=1):
    data = {"version": 1, "fitted_at_ms": fitted_at, "default_edge": 0.005, "wallets": wallets or {},
            "nodes": {"*|*|*|*|*": {"segment": "*|*|*|*|*", "level": 5, "n": 100, "source": "model", "base": base,
                                    "curse_mult": curse, "info_premium": 0.0}}}
    path.write_text(json.dumps(data))
    os.utime(path, (time.time() + fitted_at, time.time() + fitted_at))   # distinct mtimes between rewrites
    return path


def world(cfg):
    cache = GameCache(cfg, build_models=False)
    cache.entries[NBA] = nba_entry(NBA, "a1")
    cache.entries[NBA2] = nba_entry(NBA2, "b1")
    asm = Assembler(cache, cfg, decisions=DecisionLog(None), now_ms=lambda: NOW)
    return cache, FakeStore(cache.entries.values()), asm


def rfq(rid="r1", req="req_a"):
    return RfqRequest(type="RFQ_REQUEST", rfq_id=rid, requestor_public_id=req, leg_position_ids=("a1", "b1"), condition_id="cond",
                      yes_position_id="y", no_position_id="n", direction="BUY", side="YES",
                      requested_size=RequestedSize(unit="notional", value_e6=100_000_000), submission_deadline_ms=NOW + 400)


def test_no_table_falls_back_to_the_stage_two_margin(tmp_path):
    cfg = QuoteConfig(risk=False, edge_table=tmp_path / "missing.json")
    _, store, asm = world(cfg)
    d = asm.evaluate(rfq(), store)
    assert d.quoted and d.edge_source == "default" and d.edge_level == -1 and d.edge_segment.startswith("nba|2|BUY|disjoint|s")
    assert d.margin == pytest.approx(cfg.k_m * d.u_m + cfg.k_d * d.u_d + cfg.base_edge)
    asm_off = Assembler(world(QuoteConfig(risk=False, edge=False))[0], QuoteConfig(risk=False, edge=False), decisions=DecisionLog(None), now_ms=lambda: NOW)
    d2 = asm_off.evaluate(rfq(), store)
    assert d2.margin == pytest.approx(d.margin) and d2.edge_source is None


def test_fitted_node_prices_the_margin_and_blocked_requester_is_skipped(tmp_path):
    path = table(tmp_path / "t.json", base=0.012, curse=0.5, wallets={"req_bad": {"blocked": True, "reason": "rule", "premium": 0.0},
                                                                     "req_prem": {"blocked": False, "premium": 0.004}})
    cfg = QuoteConfig(risk=False, edge_table=path)
    _, store, asm = world(cfg)
    d = asm.evaluate(rfq(), store)
    assert d.quoted and d.edge_source == "model" and d.edge_level == 5 and d.curse_mult == 0.5 and d.e_b == 0.012
    assert d.margin == pytest.approx(0.012 + 0.5 * (d.u_m + d.u_d))
    asm.handle(rfq("r1"), store)
    row = asm.decisions.read("r1")[0]
    assert row["edge_segment"] == "*|*|*|*|*" and row["edge_source"] == "model" and row["curse_mult"] == 0.5
    b = asm.evaluate(rfq("r2", req="req_bad"), store)
    assert b.decision == "skipped:wallet_blocked" and "rule" in b.reason
    p = asm.evaluate(rfq("r3", req="req_prem"), store)
    assert p.quoted and p.wallet_premium == 0.004 and p.margin == pytest.approx(d.margin + 0.004)


def test_refit_is_picked_up_by_the_refresh_hook(tmp_path):
    path = table(tmp_path / "t.json", base=0.010, curse=1.0, fitted_at=1)
    cfg = QuoteConfig(risk=False, edge_table=path)
    cache, store, asm = world(cfg)
    cache.on_refresh.append(asm.reload_edge_table)
    m1 = asm.evaluate(rfq(), store).margin
    table(path, base=0.020, curse=1.0, fitted_at=2)
    assert asm.evaluate(rfq(), store).margin == pytest.approx(m1)     # hot path never re-reads the file
    for hook in cache.on_refresh:                                      # the refresh thread does (refresh_due runs these)
        hook()
    assert asm.edge_table_fitted_at == 2
    assert asm.evaluate(rfq(), store).margin == pytest.approx(m1 + 0.010)
    path.unlink()
    assert asm.reload_edge_table() and asm.edge_table_fitted_at is None
    assert asm.evaluate(rfq(), store).edge_source == "default"


def test_own_probe_requester_is_skipped_before_pricing():
    cfg = QuoteConfig(risk=False, edge=False, probe_requester_id="probe_me")
    _, store, asm = world(cfg)
    assert asm.evaluate(rfq(req="probe_me"), store).decision == "skipped:own_probe"
    assert asm.evaluate(rfq(req="req_a"), store).quoted
    assert load_quote_config(env={"PROBE_REQUESTER_ID": " probe_me "}).probe_requester_id == "probe_me"
    off = Assembler(world(QuoteConfig(risk=False, edge=False))[0], QuoteConfig(risk=False, edge=False), decisions=DecisionLog(None), now_ms=lambda: NOW)
    assert off.evaluate(rfq(req="probe_me"), store).quoted
