"""Markout monitor and the end-to-end fit on synthetic decisions / prints with a fake fair source."""
import json

import pytest

from rfqbot.edge.config import EdgeConfig
from rfqbot.edge.data import DecisionRec, LegInfo, Print
from rfqbot.edge.fairs import FairSource, LegFair
from rfqbot.edge.markout import info_premium_for, node_means, refresh_markouts, summarize
from rfqbot.edge.pipeline import fit
from rfqbot.edge.store import EdgeStore
from rfqbot.edge.table import load_table

E6 = 1_000_000
T0 = 1_792_000_000_000
G1, G2 = "nba-okc-sas-2026-10-20", "nba-phi-nyk-2026-10-20"


class FakeFairs:
    """Leg fairs that drift linearly: p(t) = p0 + slope * minutes; Kalshi = p + 0.002; a leg can go in-play."""

    def __init__(self, slopes: dict[str, float], inplay_after_min: float | None = None):
        self.slopes = slopes
        self.inplay = inplay_after_min

    def __call__(self, prefix: str, at_ms: int) -> dict[str, LegFair]:
        mins = (at_ms - T0) / 60_000
        out = {}
        for pid, p0 in (("p1", 0.5), ("p2", 0.4), ("p3", 0.6)):
            if (prefix == G1 and pid != "p1") or (prefix == G2 and pid == "p1"):
                continue
            p = p0 + self.slopes.get(pid, 0.0) * mins
            tier = "skip" if (self.inplay is not None and mins >= self.inplay) else "ok"
            out[pid] = LegFair(pid, p, p - 0.001, p + 0.002, 0.005, tier, "exact" if pid != "p3" else "none")
        return out


def prt(rfq, direction, price, at, requester="w1", pids=("p1", "p2"), prefixes=(G1, G2)):
    return Print(rfq_id=rfq, direction=direction, price=price, size_e6=100 * E6, executed_at_ms=at, requester=requester,
                 n_legs=len(pids), position_ids=list(pids), prefixes=list(prefixes))


def test_refresh_markouts_signs_horizons_and_references():
    cfg = EdgeConfig(horizons_min=(1, 5, 15, 30), sigma_cuts=(0.005, 0.009))
    store = EdgeStore(None)
    # p1 drifts up 0.001 / min: a BUY print (maker sold YES) loses, a SELL print gains
    fairs = FairSource(cfg, compute=FakeFairs({"p1": 0.001}))
    prints = [prt("b", "BUY", 0.21, T0), prt("s", "SELL", 0.19, T0, requester="w2")]
    now = T0 + 16 * 60_000          # only 1, 5 and 15 min have matured
    counts = refresh_markouts(cfg, store, fairs, now=now, prints=prints)
    assert counts == {"prints": 2, "computed": 18, "missing_fair0": 0, "missing_fair_h": 0, "pending": 6}
    rows = store.markout_rows()
    by = {(r["rfq_id"], r["ref"], r["horizon_min"]): r for r in rows}
    # fair0 = 0.5 * 0.4 = 0.2; after 15 min p1 = 0.515 -> fair 0.206: BUY maker lost 0.6pt, SELL maker gained
    assert by[("b", "model", 15)]["fair0"] == pytest.approx(0.2) and by[("b", "model", 15)]["fair_h"] == pytest.approx(0.206)
    assert by[("b", "model", 15)]["markout"] == pytest.approx(-0.006)
    assert by[("s", "model", 15)]["markout"] == pytest.approx(+0.006)
    assert by[("b", "model", 1)]["markout"] == pytest.approx(-0.0004)
    assert by[("b", "kalshi", 5)]["fair0"] == pytest.approx(0.502 * 0.402)
    assert by[("b", "poly", 5)]["fair0"] == pytest.approx(0.499 * 0.399)
    # sigma of the print = 0.2 * sqrt((.005/.5)^2 + (.005/.4)^2) = 0.0032 -> s0
    assert by[("b", "model", 15)]["segment"] == "nba|2|BUY|disjoint|s0" and by[("b", "model", 15)]["requester"] == "w1"
    # the pending horizon matures later and is filled in; nothing is recomputed
    calls = fairs.calls
    counts = refresh_markouts(cfg, store, fairs, now=T0 + 31 * 60_000, prints=prints)
    assert counts["computed"] == 6 and counts["pending"] == 0
    assert len(store.markout_rows()) == 24
    stats = {s.key: s for s in summarize(store.markout_rows(), ("ref", "horizon_min", "direction"))}
    assert stats[("model", 30, "BUY")].n == 1 and stats[("model", 30, "BUY")].mean == pytest.approx(-0.012)
    means = node_means(store.markout_rows(), ref="model", horizon_min=30)
    assert means["nba|2|BUY|disjoint|s0"] == (pytest.approx(-0.012), 1) and means["*|*|*|*|*"][1] == 2
    assert info_premium_for("nba|2|BUY|disjoint|s2", means, n_min=1) == (pytest.approx(0.012), "nba|2|BUY|disjoint|*", 1)
    assert info_premium_for("nba|2|SELL|disjoint|s0", means, n_min=1)[0] == 0.0       # SELL flow gained: no premium
    assert info_premium_for("nfl|2|BUY|disjoint|s1", means, n_min=5) == (0.0, "", 0)
    assert fairs.calls > calls


def test_refresh_markouts_records_missing_later_fair_when_a_leg_goes_in_play():
    cfg = EdgeConfig(horizons_min=(5, 30))
    store = EdgeStore(None)
    fairs = FairSource(cfg, compute=FakeFairs({}, inplay_after_min=10))
    counts = refresh_markouts(cfg, store, fairs, now=T0 + 40 * 60_000, prints=[prt("b", "BUY", 0.21, T0)])
    assert counts["computed"] == 3 and counts["missing_fair_h"] == 3
    rows = {(r["ref"], r["horizon_min"]): r for r in store.markout_rows()}
    assert rows[("model", 30)]["fair_h"] is None and rows[("model", 30)]["markout"] is None and rows[("model", 5)]["markout"] == pytest.approx(0.0)
    # a missing later fair is final (books never backfill): the row is not retried
    assert ("model", 30) in store.markout_done().get("b", set())


def dec(i, direction, p_fair, price, at, rfq, requester="w1", u_m=0.0032):
    legs = [LegInfo("p1", G1, 0.5, 0.005, "exact"), LegInfo("p2", G2, 0.4, 0.005, "exact")]
    return DecisionRec(id=i, rfq_id=rfq, at_ms=at, direction=direction, requested_unit="notional", requested_e6=20 * E6, n_legs=2,
                       p_fair=p_fair, u_m=u_m, u_d=0.0, margin=abs(price - p_fair), price=price, size_e6=100 * E6, legs=legs,
                       requester=requester)


def test_fit_end_to_end_writes_table_and_scores_wallets(tmp_path):
    cfg = EdgeConfig(edge_db=tmp_path / "edge.db", table_path=tmp_path / "margin_table.json", wallet_list=tmp_path / "w.json",
                     n_min=10, min_printed=3, horizons_min=(15, 30), sigma_cuts=(0.005, 0.009), block_min_fills=8)
    store = EdgeStore(cfg.edge_db)
    fairs = FairSource(cfg, compute=FakeFairs({"p1": 0.0005}))   # fair drifts up 0.02 pt/min on p1 -> BUY flow is informed
    decisions, prints = [], []
    for i in range(40):
        at = T0 + i * 60_000
        rfq = f"rfq-{i}"
        # we quote fair + 1pt; the print sits 0.5 .. 4pt over fair, so we win when the print is >= 1pt
        gap = 0.005 + (i % 8) * 0.005
        pf = 0.20 + 0.0002 * i                          # our fair tracks the drift (0.4 * 0.0005 per minute)
        decisions.append(dec(i, "BUY", pf, pf + 0.01, at, rfq, requester="toxic" if i % 2 else "benign"))
        if i % 5 != 4:                                  # some RFQs never print
            prints.append(prt(rfq, "BUY", pf + gap, at + 10_000, requester="toxic" if i % 2 else "benign"))
    rep = fit(cfg, store, now=T0 + 80 * 60_000, fairs=fairs, decisions=decisions, prints=prints)
    assert len(rep.fills) == 40 and sum(f.printed for f in rep.fills) == 32
    wins = [f for f in rep.fills if f.win_actual]
    assert all(f.delta >= 0.01 - 1e-12 for f in wins) and len(wins) == sum(1 for f in rep.fills if f.printed and f.delta >= 0.01 - 1e-12)
    # curves: the full cell has 40 decisions, so it resolves to itself
    assert rep.curves.cells["nba|2|BUY|disjoint|s0"] == ("nba|2|BUY|disjoint|s0", 0)
    c = rep.curves.nodes["nba|2|BUY|disjoint|s0"]
    assert c.n == 40 and c.n_printed == 32 and c.f[0] == pytest.approx(0.8) and all(a >= b for a, b in zip(c.f, c.f[1:]))
    # markouts: 32 prints x 2 horizons x 3 refs, all matured; BUY flow loses 0.6 / 1.2 pt (30 min) on the model reference
    assert rep.markout_counts["computed"] == 32 * 6 and rep.markout_counts["pending"] == 0
    st = {s.key: s for s in summarize(rep.markout_rows, ("ref", "horizon_min", "direction"))}
    assert st[("model", 30, "BUY")].mean == pytest.approx(-0.4 * 0.0005 * 30, abs=1e-9)
    # the node's info premium is that loss; the optimiser chose a markup on the grid
    n = rep.nodes["nba|2|BUY|disjoint|s0"]
    assert n.info_premium == pytest.approx(0.006, abs=1e-9) and n.source == "model" and 0 < n.m_star <= 0.04
    # curse estimates exist for the node at both horizons and all references
    keys = {(e.segment, e.horizon_min, e.ref) for e in rep.curse}
    assert ("nba|2|BUY|disjoint|s0", 30, "model") in keys and ("nba|2|BUY|disjoint|s0", 15, "kalshi") in keys
    est = next(e for e in rep.curse if (e.segment, e.horizon_min, e.ref) == ("nba|2|BUY|disjoint|s0", 30, "model"))
    assert est.n_with_later == 32 and est.measured[0] == pytest.approx(0.0002 * (30 + 10 / 60), abs=1e-6)   # later fair - p_fair: +0.6pt
    # wallets: both wallets get 16 observations (profit = markup + markout = 1pt - 0.6pt > 0): nobody blocked
    assert rep.wallet_obs == 32
    assert {w.public_id: w.n for w in rep.wallets.values()} == {"toxic": 16, "benign": 16}
    assert not any(w.blocked for w in rep.wallets.values())
    # table on disk, loadable, with the node and the cells
    t = load_table(cfg.table_path, max_age_s=0)
    assert t is not None and "nba|2|BUY|disjoint|s0" in t.nodes and t.data["cells"]["nba|2|BUY|disjoint|s0"]["level"] == 0
    assert t.terms(c.segment).base == pytest.approx(n.base, abs=1e-6)   # the table rounds to 6 decimals
    # second fit reuses stored markouts (no new compute_game calls beyond cache) and keeps counts
    calls = fairs.calls
    rep2 = fit(cfg, store, now=T0 + 80 * 60_000, fairs=fairs, decisions=decisions, prints=prints)
    assert rep2.markout_counts["computed"] == 0 and fairs.calls == calls
    meta = json.loads(store.conn.execute("SELECT value FROM meta WHERE key='last_fit'").fetchone()[0])
    assert meta["decisions"] == 40 and meta["fills_printed"] == 32
    store.close()
