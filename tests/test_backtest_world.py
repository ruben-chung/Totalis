"""Shared synthetic world for the backtest tests: tiny rfq.db / books.db / kalshi_books.db and a raw RFQ log.

Two NBA games (a moneyline pair each, plus a spread pair on the first), one Kalshi match for a Thunder moneyline,
books that change at known times, a trade print on one RFQ. Time zero is T0 (2026-10-20 23:46:40 UTC), tip-off
is two hours later, so everything is pre-game unless a test moves the clock.
"""
import gzip
import json
from pathlib import Path

from rfqbot.books import BookStore
from rfqbot.kalshi_books import KalshiBookStore
from rfqbot.store import Store

T0 = 1_792_540_000_000
G1, G2 = "nba-gsw-lal-2026-10-21", "nba-okc-sas-2026-10-20"
TIP = "2026-10-21 01:46:40+00"
PID = {"g1_war": "pg1w", "g1_lak": "pg1l", "g1_sp_lak": "pg1sl", "g1_sp_war": "pg1sw", "g2_thu": "pg2t", "g2_spu": "pg2s"}
TOK = {k: "tok_" + v for k, v in PID.items()}
COND = {"g1_ml": "0xg1ml", "g1_sp": "0xg1sp", "g2_ml": "0xg2ml"}
KTICKER = "KXNBAGAME-26OCT20OKCSAS-OKC"


def market(pid, slug, title, outcome, idx, cond):
    return {"position_id": pid, "market_id": "1", "condition_id": cond, "slug": slug, "title": title, "outcome": outcome,
            "outcome_index": idx, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1}


def rfq_frame(rfq_id, t, legs, direction="BUY", unit="notional", value_e6=50_000_000, requester="req_a", deadline=None, cond=None):
    payload = {"type": "RFQ_REQUEST", "rfq_id": rfq_id, "requestor_public_id": requester, "leg_position_ids": list(legs),
               "condition_id": cond or ("0xcombo_" + rfq_id), "yes_position_id": "y_" + rfq_id, "no_position_id": "n_" + rfq_id,
               "direction": direction, "side": "YES", "requested_size": {"unit": unit, "value_e6": str(value_e6)},
               "submission_deadline": deadline if deadline is not None else t + 400}
    return {"t": t, "d": "in", "c": 1, "s": 0, "p": json.dumps(payload)}


def write_raw(dir_: Path, frames, name="2026-10-20/23.jsonl.gz"):
    path = Path(dir_) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        for fr in frames:
            f.write(json.dumps(fr) + "\n")
    return Path(dir_)


def build_world(tmp_path, *, extra_g1_row=None, book_rows=None, kalshi_rows=None):
    """-> dict of paths. ``extra_g1_row`` = (at_ms, bid_int, ask_int) appended for the Warriors token (lookahead tests)."""
    rfq = Store(tmp_path / "rfq.db")
    rfq.upsert_markets([
        market(PID["g1_war"], G1, "Warriors vs. Lakers", "Warriors", 0, COND["g1_ml"]),
        market(PID["g1_lak"], G1, "Warriors vs. Lakers", "Lakers", 1, COND["g1_ml"]),
        market(PID["g1_sp_lak"], G1 + "-spread-home-6pt5", "Spread: Lakers (-6.5)", "Lakers", 0, COND["g1_sp"]),
        market(PID["g1_sp_war"], G1 + "-spread-home-6pt5", "Spread: Lakers (-6.5)", "Warriors", 1, COND["g1_sp"]),
        market(PID["g2_thu"], G2, "Thunder vs. Spurs", "Thunder", 0, COND["g2_ml"]),
        market(PID["g2_spu"], G2, "Thunder vs. Spurs", "Spurs", 1, COND["g2_ml"]),
    ])
    rfq.close()
    books = BookStore(tmp_path / "books.db")
    rows = []
    for key, pid in PID.items():
        slug = G1 + ("-spread-home-6pt5" if "sp" in key else "") if key.startswith("g1") else G2
        outcome = {"g1_war": "Warriors", "g1_lak": "Lakers", "g1_sp_lak": "Lakers", "g1_sp_war": "Warriors", "g2_thu": "Thunder", "g2_spu": "Spurs"}[key]
        rows.append((pid, "0x", 0, outcome, TOK[key], slug, "1", TIP, None, 1))
    books.upsert_tokens(rows)
    books.subscribed({tok: pid for tok, pid in ((TOK[k], PID[k]) for k in PID)}, T0 - 3_600_000)
    books.subscribed({"tok_hb": "hb"}, T0 - 3_600_000)
    books.begin()
    base = {"g1_war": (4800, 5000), "g1_lak": (4900, 5100), "g1_sp_lak": (4700, 4900), "g1_sp_war": (5000, 5200),
            "g2_thu": (5900, 6100), "g2_spu": (3900, 4100)}
    for k, (b, a) in base.items():
        books.insert_top(TOK[k], T0 - 600_000, None, (b, a, 2000.0, 2000.0, 8000.0, 8000.0), "book")
    # a change on the Warriors moneyline 40 s in, and one on the Thunder 100 s in
    books.insert_top(TOK["g1_war"], T0 + 40_000, None, (5000, 5200, 2000.0, 2000.0, 8000.0, 8000.0), "delta")
    books.insert_top(TOK["g2_thu"], T0 + 100_000, None, (6100, 6300, 2000.0, 2000.0, 8000.0, 8000.0), "delta")
    for t in range(T0 - 600_000, T0 + 1_200_000, 10_000):          # heartbeat: another asset ticks every 10 s
        books.insert_top("tok_hb", t, None, (5000 + (t // 10_000) % 7, 5100, 10.0, 10.0, 10.0, 10.0), "delta")
    for r in (book_rows or []):
        books.insert_top(*r)
    if extra_g1_row:
        at, b, a = extra_g1_row
        books.insert_top(TOK["g1_war"], at, None, (b, a, 2000.0, 2000.0, 8000.0, 8000.0), "delta")
    books.close()
    kb = KalshiBookStore(tmp_path / "kalshi_books.db")
    kb.upsert_matches([{"position_id": PID["g2_thu"], "sport": "nba", "canonical_key": "k_thu", "game_key": "nba|2026-10-20|OKC@SAS",
                        "kalshi_ticker": KTICKER, "kalshi_side": "yes", "confidence": "exact", "notes_json": "[]",
                        "line_candidates_json": None, "matched_at_ms": 1},
                       {"position_id": PID["g2_spu"], "sport": "nba", "canonical_key": "k_spu", "game_key": "nba|2026-10-20|OKC@SAS",
                        "kalshi_ticker": KTICKER, "kalshi_side": "no", "confidence": "exact", "notes_json": "[]",
                        "line_candidates_json": None, "matched_at_ms": 1}])
    kb.insert_top(KTICKER, T0 - 300_000, None, (0.58, 0.60, 4000.0, 4000.0, 9000.0, 9000.0), "orderbook")
    kb.insert_top(KTICKER, T0 + 100_000, None, (0.60, 0.62, 4000.0, 4000.0, 9000.0, 9000.0), "orderbook")
    for t in range(T0 - 600_000, T0 + 1_200_000, 20_000):
        kb.insert_top("KXHB", t, None, (0.1 + (t // 20_000) % 5 / 100, 0.2, 1.0, 1.0, None, None), "ws")
    for r in (kalshi_rows or []):
        kb.insert_top(*r)
    kb.close()
    return {"rfq_db": tmp_path / "rfq.db", "books_db": tmp_path / "books.db", "kalshi_db": tmp_path / "kalshi_books.db", "root": tmp_path}


def add_print(rfq_db, rfq_id, *, direction, price_e6, size_e6, at_ms, legs, requester="req_a"):
    """A trade print on ``rfq_id`` (needs a combo with its legs and a requester, as the live store has)."""
    s = Store(rfq_db)
    cid = s.combo_id("0xcombo_" + rfq_id, legs, yes="y_" + rfq_id, no="n_" + rfq_id, at_ms=at_ms)
    rid = s.requester_id(requester)
    s.insert_trade({"rfq_id": rfq_id, "requester_id": rid, "combo_id": cid, "direction": direction, "side": "YES", "price_e6": price_e6,
                    "size_e6": size_e6, "executed_at_ms": at_ms, "received_at_ms": at_ms, "is_ours": 0, "raw_message_id": None})
    s.close()


def test_world_builds(tmp_path):
    w = build_world(tmp_path)
    assert w["books_db"].exists() and w["kalshi_db"].exists()
