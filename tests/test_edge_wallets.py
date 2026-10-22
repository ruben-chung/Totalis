import json
import random

from rfqbot.edge.config import EdgeConfig
from rfqbot.edge.wallets import WalletObs, load_manual, save_manual, score_wallets, z_for

CFG = EdgeConfig(block_min_fills=8, block_days=7, wallet_k=1.0, shrink_n=8.0, block_conf=0.90)
DAY = 86_400_000


def wallet(pid, profits, rt=False):
    return [WalletObs(pid, p, 1_000_000 + i, round_trip=(rt and i == 0)) for i, p in enumerate(profits)]


def sequential_blocked(profits, pop, cfg=CFG):
    """Evaluate the rule after every fill from block_min_fills on (how the monitor applies it)."""
    prior = {}
    for n in range(cfg.block_min_fills, len(profits) + 1):
        obs = wallet("x", profits[:n]) + pop
        scores = score_wallets(obs, cfg, now_ms=10_000_000, prior=prior)
        prior = scores
        if scores["x"].blocked:
            return n
    return None


def test_z_for():
    assert abs(z_for(0.90) - 1.2816) < 1e-4 and abs(z_for(0.95) - 1.6449) < 1e-4
    assert abs(z_for(0.85) - 1.0364) < 1e-3


def fpr(conf, rng, trials=120, fills=40):
    cfg = EdgeConfig(block_min_fills=8, block_days=7, wallet_k=1.0, shrink_n=8.0, block_conf=conf)
    pop = [o for w in range(20) for o in wallet(f"p{w}", [rng.gauss(0.0, 0.02) for _ in range(30)])]
    fp = sum(1 for _ in range(trials) if sequential_blocked([rng.gauss(0.0, 0.02) for _ in range(fills)], pop, cfg) is not None)
    return fp / trials


def test_neutral_wallets_false_positive_rate():
    """The brief's rule (one-sided 90% bound, checked after every fill from the 8th) is a sequential
    test, so its cumulative false-positive rate over 40 fills is ~20-25%. Accepted by Ruben 2026-10-21:
    a wrongly blocked neutral wallet costs little flow, an unblocked sharp one costs EV on every fill.
    0.95 would bring it under 10% (claude-sessions/margin-optimizer-notes.md)."""
    rng = random.Random(3)
    r90 = fpr(0.90, rng)
    r975 = fpr(0.975, rng)
    assert 0.10 < r90 < 0.35, r90
    assert r975 < 0.10, r975
    assert r975 < r90


def test_toxic_wallet_is_detected_quickly():
    rng = random.Random(5)
    pop = [o for w in range(20) for o in wallet(f"p{w}", [rng.gauss(0.0, 0.02) for _ in range(30)])]
    hits = []
    for t in range(60):
        n = sequential_blocked([rng.gauss(-0.02, 0.02) for _ in range(40)], pop)
        hits.append(n)
    detected = [n for n in hits if n is not None]
    assert len(detected) >= 58, hits
    assert sorted(detected)[len(detected) // 2] <= 16     # median detection within 16 fills


def test_block_rule_needs_min_fills_and_shrinks_toward_population():
    pop = [o for w in range(10) for o in wallet(f"p{w}", [0.01, -0.005, 0.004, 0.002, -0.001, 0.006] * 5)]
    s = score_wallets(wallet("x", [-0.05] * 7) + pop, CFG, now_ms=0)
    assert not s["x"].blocked and s["x"].n == 7 and s["x"].upper < 0        # would trigger, but too few fills
    assert s["x"].premium > 0 and s["x"].shrunk > s["x"].mean               # premium below the threshold; shrunk toward ~0
    s = score_wallets(wallet("x", [-0.05] * 8) + pop, CFG, now_ms=0)
    assert s["x"].blocked and s["x"].reason == "rule" and s["x"].blocked_until_ms == 7 * DAY and s["x"].premium == 0.0


def test_block_expires_unless_retriggered_and_carries_until_then():
    pop = [o for w in range(10) for o in wallet(f"p{w}", [0.01, -0.005, 0.004, 0.002, -0.001, 0.006] * 5)]
    s0 = score_wallets(wallet("x", [-0.05] * 8) + pop, CFG, now_ms=0)
    assert s0["x"].blocked
    # later the wallet's recent record is neutral: the block is carried until expiry, then released
    neutral = wallet("x", [0.0] * 8)
    s1 = score_wallets(neutral + pop, CFG, now_ms=3 * DAY, prior=s0)
    assert s1["x"].blocked and s1["x"].reason == "carried" and s1["x"].blocked_until_ms == 7 * DAY
    s2 = score_wallets(neutral + pop, CFG, now_ms=8 * DAY, prior=s1)
    assert not s2["x"].blocked
    # re-triggered: a fresh 7-day window
    s3 = score_wallets(wallet("x", [-0.05] * 8) + pop, CFG, now_ms=3 * DAY, prior=s0)
    assert s3["x"].reason == "rule" and s3["x"].blocked_until_ms == 10 * DAY


def test_round_trip_blocks_immediately_and_manual_list_wins(tmp_path):
    pop = [o for w in range(10) for o in wallet(f"p{w}", [0.01, -0.005, 0.004] * 5)]
    s = score_wallets(wallet("rt", [0.02], rt=True) + pop, CFG, now_ms=0)
    assert s["rt"].blocked and s["rt"].reason == "round_trip"
    manual = {"rt": "allow", "p0": "deny"}
    s = score_wallets(wallet("rt", [0.02], rt=True) + pop, CFG, now_ms=0, manual=manual)
    assert not s["rt"].blocked and s["rt"].premium == 0.0 and s["rt"].manual == "allow"
    assert s["p0"].blocked and s["p0"].reason == "manual"
    p = tmp_path / "w.json"
    save_manual(p, manual)
    assert json.loads(p.read_text()) == {"allow": ["rt"], "deny": ["p0"]}
    assert load_manual(p) == manual and load_manual(tmp_path / "missing.json") == {}
