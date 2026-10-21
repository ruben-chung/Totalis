"""Analysis signs and a fake-gateway end-to-end run producing a report."""
import random

import pytest

from rfqbot.edge.data import DecisionRec, LegInfo, Print
from rfqbot.edge.fairs import FairSource, LegFair
from rfqbot.probe.analysis import combo_markout, gap, leg_markout, probe_gap, report_lines
from rfqbot.probe.config import ProbeConfig
from rfqbot.probe.disguise import ExtraLeg, LegMeta
from rfqbot.probe.gateway import E6, FakeGateway
from rfqbot.probe.scheduler import execute, plan, triggers
from rfqbot.probe.store import ProbeStore

T0 = 1_792_000_000_000
G1, G2 = "nba-okc-sas-2026-10-20", "nba-phi-nyk-2026-10-20"


def test_markout_signs():
    # we sold YES at 0.21 (BUY trigger); the probe says the combo is worth 0.23 now: we lost 2 pt
    assert combo_markout("BUY", 0.21, 0.23) == pytest.approx(-0.02)
    assert combo_markout("BUY", 0.21, 0.19) == pytest.approx(+0.02)
    # we bought YES at 0.19 (SELL trigger); the probe says 0.17: we lost 2 pt
    assert combo_markout("SELL", 0.19, 0.17) == pytest.approx(-0.02)
    assert leg_markout("SELL", 0.19, 0.21) == pytest.approx(+0.02)
    # competitor gap in our win direction: BUY -> probe above our fair is room, SELL -> below
    assert gap("BUY", 0.20, 0.23) == pytest.approx(0.03) and gap("SELL", 0.20, 0.17) == pytest.approx(0.03)


class Fairs:
    """Leg fairs drifting up on p1 so BUY flow (we sold) is informed."""

    def __call__(self, prefix, at_ms):
        mins = (at_ms - T0) / 60_000
        legs = {G1: ("p1", 0.5 + 0.0005 * mins), G2: ("p2", 0.4)}
        pid, p = legs[prefix]
        return {pid: LegFair(pid, p, p - 0.001, p + 0.002, 0.005, "ok", "exact")}


def test_fake_gateway_end_to_end_report(tmp_path):
    cfg = ProbeConfig(mode="dry", db=tmp_path / "probe.db", delay_sec=5, horizons_sec=(60, 300), max_per_hour=100, max_per_combo=6,
                      min_spacing_sec=0, disguise=True, size_usd=20)
    rng = random.Random(1)
    decisions, prints = [], []
    for i in range(10):
        at = T0 + i * 60_000
        pf = 0.20 + 0.0002 * i
        d = "BUY" if i % 3 else "SELL"
        price = pf + 0.01 if d == "BUY" else pf - 0.01
        legs = [LegInfo("p1", G1, 0.5, 0.005, "exact"), LegInfo("p2", G2, 0.4, 0.005, "exact")]
        decisions.append(DecisionRec(id=i, rfq_id=f"rfq-{i}", at_ms=at, direction=d, requested_unit="notional", requested_e6=20 * E6, n_legs=2,
                                     p_fair=pf, u_m=0.004, u_d=0.0, margin=0.01, price=price, size_e6=100 * E6, legs=legs))
        prints.append(Print(rfq_id=f"rfq-{i}", direction=d, price=price + (0.005 if d == "BUY" else -0.005), size_e6=100 * E6,
                            executed_at_ms=at + 10_000, requester="w_tox" if i % 2 else "w_ok", n_legs=2, position_ids=["p1", "p2"],
                            prefixes=[G1, G2], condition_id=f"0xc{i}"))
    fairs = FairSource(None, compute=Fairs())
    pool = [ExtraLeg("x1", "c-x1", "nfl-dal-phi-2026-10-26", "Yes", 0.995, 0.99, 1.0, T0 + 86_400_000, T0)]
    metas = {"p1": LegMeta("p1", "c-p1", G1, "Yes", 0, T0 + 7_200_000, None), "p2": LegMeta("p2", "c-p2", G2, "Yes", 0, T0 + 7_200_000, None)}

    def price_fn(req):
        # makers quote the (disguised) combo at the drifted leg fair + 1pt in the requester's disfavour, plus noise
        now = price_fn.now
        fair = fairs.combo(["p1", "p2"], [G1, G2], now).model
        p = fair * (0.995 if len(req.leg_position_ids) == 3 else 1.0)
        return p + (0.01 if req.direction == "BUY" else -0.01) + rng.gauss(0, 0.002)

    store = ProbeStore(cfg.db)
    gw = FakeGateway(price_fn, requestor_public_id="req_probe")
    trigs = triggers(cfg, T0, decisions=decisions, prints=prints)
    assert len(trigs) == 10
    sent = 0
    for now in (T0 + 20 * 60_000, T0 + 40 * 60_000):          # two passes so the 5 s / 60 s / 300 s horizons are all due
        price_fn.now = now
        plans, st = plan(trigs, store, ProbeConfig(**{**cfg.__dict__, "max_late_sec": 1e9}), now)
        rows = execute(plans, gw, store, cfg, now=now, fairs=fairs, pool=pool, metas=metas, mode="fake")
        sent += len(rows)
    assert sent == 60 and gw.accepted == 0                        # 10 triggers x 3 horizons x 2 directions, nothing accepted
    rows = store.rows()
    assert all(r["extra_leg_json"] for r in rows) and all(r["backed_out_price"] is not None for r in rows)
    # back-out recovers the undisguised price within the extra leg's error
    r0 = rows[0]
    assert abs(r0["backed_out_price"] * r0["p_L"] - r0["blended_price"]) < 1e-9 and r0["backed_out_err"] > 0
    assert r0["leg_fair_send"] is not None and r0["requestor_public_id"] == "req_probe" and store.meta("requester_id") == "req_probe"
    # per-RFQ gap: the probe price sits ~1pt beyond our fair in our win direction
    g = probe_gap("rfq-1", db=store)
    assert g is not None and 0.0 < g < 0.05
    assert probe_gap("rfq-nope", db=store) is None
    lines = report_lines(store, cfg)
    text = "\n".join(lines)
    assert "probes: 60" in text and "disguised 60" in text and "nba|2|BUY|disjoint|s0" in text and "w_tox" in text
    # the combo markout for BUY triggers is negative (the leg fair drifted against us) and the leg-book one agrees in sign
    seg_lines = [l for l in text.split("\n") if l.startswith("nba|2|BUY|disjoint|s0") and " same " in l]
    assert seg_lines and all(l.split()[5].startswith("-") for l in seg_lines)
    store.close()
