"""Marginal layer over real stores: point-in-time (no future quotes), sources, CLI table, --log, config."""
import sqlite3

from rfqbot.books import BookStore
from rfqbot.kalshi_books import KalshiBookStore
from rfqbot.marginals import LegMarginal, compute_game
from rfqbot.marginals.__main__ import LOG_COLUMNS, main
from rfqbot.marginals.config import MarginalConfig, load_config
from rfqbot.marginals.sources import (KalshiQuote, MatchRow, kalshi_confirmation, kalshi_source, no_kalshi,
                                      poly_confirmations, poly_source, sport_of, synthetic_kalshi)
from rfqbot.store import Store

GAME = "nfl-pit-no-2026-10-25"
START_MS = 1_792_935_000_000          # 2026-10-25 13:30:00+00
T0 = START_MS - 3_600_000             # an hour before kickoff


def _market(pid, slug, title, outcome, idx, cond="0xc"):
    return {"position_id": pid, "market_id": "1", "condition_id": cond, "slug": slug, "title": title, "outcome": outcome,
            "outcome_index": idx, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1}


def _dbs(tmp_path):
    rfq = Store(tmp_path / "rfq.db")
    rfq.upsert_markets([
        _market("p_a", GAME, "Steelers vs. Saints", "Steelers", 0),
        _market("p_b", GAME, "Steelers vs. Saints", "Saints", 1),
        _market("p_c", GAME + "-spread-home-3pt5", "Spread", "Steelers", 0, "0xs"),
    ])
    rfq.close()
    books = BookStore(tmp_path / "books.db")
    start = "2026-10-25 13:30:00+00"
    books.upsert_tokens([
        ("p_a", "0xc", 0, "Steelers", "tok_a", GAME, "1", start, None, 1),
        ("p_b", "0xc", 1, "Saints", "tok_b", GAME, "1", start, None, 1),
        ("p_c", "0xs", 0, "Steelers", "tok_c", GAME + "-spread-home-3pt5", "1", start, None, 1),
    ])
    books.begin()
    books.subscribed({"tok_a": "p_a", "tok_b": "p_b"}, T0 + 900)
    books.subscribed({"tok_c": "p_c"}, T0 + 900)
    books.unsubscribed(["tok_c"], T0 + 2500)
    books.insert_top("tok_a", T0 + 1000, None, (4000, 4200, 10.0, 10.0, 1000.0, 1000.0), "book")
    books.insert_top("tok_a", T0 + 2000, None, (4100, 4300, 10.0, 10.0, 1000.0, 1000.0), "delta")
    books.insert_top("tok_a", START_MS + 5000, None, (6000, 6200, 10.0, 10.0, 1000.0, 1000.0), "delta")   # in play
    books.insert_top("tok_b", T0 + 1500, None, (5600, None, 5.0, None, 5.0, None), "book")              # one-sided
    books.close()
    return str(tmp_path / "books.db"), str(tmp_path / "rfq.db")


def _by(rows):
    assert all(isinstance(r, LegMarginal) for r in rows)
    return {r.quote.position_id: r.marginal for r in rows}


def test_point_in_time_never_sees_future_quotes(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    poly = poly_source(books_db, rfq_db)
    by = _by(compute_game(GAME, at_ms=T0 + 1500, poly=poly, kalshi=no_kalshi))
    assert by["p_a"].p_poly == 0.41 and by["p_a"].at_ms == T0 + 1000 and by["p_a"].tier == "ok"
    assert by["p_a"].venues[0].staleness_s == 0.5 and "inplay" not in by["p_a"].flags
    assert by["p_b"].tier == "wide" and "one_sided:poly" in by["p_b"].flags and by["p_b"].p == 0.56
    assert by["p_c"].p is None and by["p_c"].tier == "skip"                                  # no row yet
    by = _by(compute_game(GAME, at_ms=T0 + 500, poly=poly, kalshi=no_kalshi))
    assert all(m.p is None for m in by.values())                                              # nothing known yet
    # latest: the in-play row is the newest and the evaluation clock is the wall clock given
    by = _by(compute_game(GAME, None, poly=poly, kalshi=no_kalshi, now_ms=START_MS + 8000))
    assert by["p_a"].p_poly == 0.61 and "inplay" in by["p_a"].flags and by["p_a"].tier == "ok"
    assert by["p_b"].p is None and by["p_b"].tier == "skip"                                   # one-sided row now > 60 s old in play
    by = _by(compute_game(GAME, at_ms=START_MS + 30_000, poly=poly, kalshi=no_kalshi))
    assert "stale:poly" in by["p_a"].flags and by["p_a"].tier == "stale"                       # 25 s old, in play, unconfirmed


def test_confirmations_make_quiet_books_fresh(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    quotes = poly_source(books_db, rfq_db)(GAME, T0 + 3000)
    # heartbeat = newest book_top row at or before t (the in-play row is in the future here)
    assert poly_confirmations(quotes, T0 + 3000, books_db=books_db) == {"p_a": T0 + 2000, "p_b": T0 + 2000}   # tok_c unsubscribed at T0+2500
    assert poly_confirmations(quotes, T0 + 2400, books_db=books_db) == {"p_a": T0 + 2000, "p_b": T0 + 2000, "p_c": T0 + 2000}
    assert poly_confirmations(quotes, T0 + 950, books_db=books_db) == {}                        # no row yet -> no heartbeat
    assert poly_confirmations(quotes, None, books_db=books_db) == {"p_a": START_MS + 5000, "p_b": START_MS + 5000}
    assert poly_confirmations([], None, books_db=books_db) == {}
    # default readers (books_db/rfq_db args) wire the confirmations in: the one-sided row from an hour
    # ago is confirmed by the in-play heartbeat 3 s before the evaluation instant
    by = _by(compute_game(GAME, at_ms=START_MS + 8000, books_db=books_db, rfq_db=rfq_db, kalshi_mode="none"))
    assert by["p_b"].p == 0.56 and by["p_b"].tier == "wide" and "stale:poly" not in by["p_b"].flags
    assert by["p_b"].venues[0].staleness_s == 3.0 and by["p_b"].venues[0].age_s == (START_MS + 8000 - (T0 + 1500)) / 1000
    assert by["p_a"].venues[0].staleness_s == 3.0 and by["p_a"].venues[0].age_s == 3.0
    # a confirmation after the evaluation instant is never used
    by = _by(compute_game(GAME, at_ms=T0 + 3000, books_db=books_db, rfq_db=rfq_db, kalshi_mode="none"))
    assert by["p_a"].venues[0].staleness_s == 1.0


def test_kalshi_store_adapter_and_confirmation(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    kdb = tmp_path / "kalshi_books.db"
    ks = KalshiBookStore(kdb)
    ks.upsert_matches([
        {"position_id": "p_a", "sport": "nfl", "canonical_key": "k", "game_key": "g", "kalshi_ticker": "KXT-A", "kalshi_side": "no",
         "confidence": "exact", "notes_json": "[]", "line_candidates_json": None, "matched_at_ms": 1},
        {"position_id": "p_b", "sport": "nfl", "canonical_key": "k2", "game_key": "g", "kalshi_ticker": None, "kalshi_side": None,
         "confidence": "none", "notes_json": "[]", "line_candidates_json": None, "matched_at_ms": 1},
    ])
    ks.insert_top("KXT-A", T0 + 1200, None, (0.57, 0.59, 100.0, 100.0, None, None), "batch")    # YES book; leg is the NO side
    ks.insert_top("KXT-A", T0 + 4000, None, (0.50, 0.52, 10.0, 10.0, None, None), "batch")    # future at T0+3000
    ks.insert_top("KXT-Z", T0 + 2800, None, (0.10, 0.12, 1.0, 1.0, None, None), "batch")      # heartbeat only
    ks.close()
    assert kalshi_confirmation(T0 + 3000, db=kdb) == T0 + 2800 and kalshi_confirmation(None, db=kdb) == T0 + 4000
    assert kalshi_confirmation(T0 + 1000, db=kdb) is None and kalshi_confirmation(None, db=tmp_path / "missing.db") is None
    rows = compute_game(GAME, at_ms=T0 + 3000, books_db=books_db, rfq_db=rfq_db, kalshi_db=kdb)
    by = _by(rows)
    a = by["p_a"]
    assert a.kalshi_confidence == "exact" and abs(a.p_kalshi - 0.42) < 1e-9 and a.w_kalshi > 0 and a.tier == "ok"
    assert a.venues[1].at_ms == T0 + 1200 and a.venues[1].staleness_s == 0.2 and a.venues[1].age_s == 1.8
    assert by["p_b"].kalshi_confidence == "none" and by["p_b"].w_kalshi == 0.0
    assert by["p_c"].kalshi_confidence == "none" and "kalshi_unused:none" in by["p_c"].flags   # no leg_matches row
    rk = {r.quote.position_id: r for r in rows}
    assert rk["p_a"].match.kalshi_ticker == "KXT-A" and rk["p_a"].kalshi.yes_bid == 0.57 and rk["p_b"].kalshi is None


def test_kalshi_sources(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    quotes = poly_source(books_db, rfq_db)(GAME, T0 + 3000)
    from rfqbot.kalshi_books import kalshi_for_legs
    assert kalshi_source("auto") is kalshi_for_legs and kalshi_source("none") is no_kalshi
    syn = kalshi_source("synthetic", quotes=quotes, now_ms=T0 + 3000)
    out = syn([q.position_id for q in quotes], T0 + 3000)
    row, kq = out["p_a"]
    assert row.confidence == "exact" and row.kalshi_ticker.startswith("SYN-") and isinstance(kq, KalshiQuote)
    assert abs((kq.yes_bid + kq.yes_ask) / 2 - 0.42) <= 0.0101 and kq.at_ms == T0 + 3000 - 5000
    assert out["p_b"][1] is None and out["p_c"][1] is None                                   # no mid -> no synthetic book
    assert syn(["p_a"], T0 + 3000)["p_a"][1] == kq                                           # deterministic
    m = _by(compute_game(GAME, at_ms=T0 + 3000, poly=poly_source(books_db, rfq_db), kalshi=syn))["p_a"]
    assert m.w_kalshi > 0 and m.kalshi_confidence == "exact"
    m = _by(compute_game(GAME, at_ms=T0 + 3000, books_db=books_db, rfq_db=rfq_db, kalshi_mode="synthetic"))["p_a"]
    assert m.w_kalshi > 0
    assert sport_of(GAME) == "nfl" and sport_of("nba-bos-det-2026-10-20") == "nba" and sport_of("xyz-a-b") == "xyz"


def test_custom_lookup_objects_are_duck_typed(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)

    class Row:            # the real producer's classes only need these attributes
        kalshi_ticker, kalshi_side, confidence, notes = "KX", "no", "rules_differ", []

    class Q:
        yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth, at_ms = 0.57, 0.59, 5, 5, 50, 50, T0 + 2500

    m = _by(compute_game(GAME, at_ms=T0 + 3000, poly=poly_source(books_db, rfq_db), kalshi=lambda pids, t: {"p_a": (Row(), Q())}))["p_a"]
    assert m.kalshi_confidence == "rules_differ" and "rules_differ" in m.flags
    assert abs(m.p_kalshi - 0.42) < 1e-9 and m.w_kalshi > 0


def test_cli_table_and_log(tmp_path, capsys):
    books_db, rfq_db = _dbs(tmp_path)
    log_db = tmp_path / "marginals.db"
    rc = main(["--game", GAME, "--at-ms", str(T0 + 3000), "--books-db", books_db, "--rfq-db", rfq_db,
               "--kalshi", "synthetic", "--log", "--log-db", str(log_db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "moneyline / Steelers" in out and "spread-home-3pt5 / Steelers" in out and "3 legs" in out
    assert "kalshi=synthetic (used on 1)" in out and "logged 3 rows" in out
    conn = sqlite3.connect(log_db)
    conn.row_factory = sqlite3.Row
    recs = {r["position_id"]: r for r in conn.execute("SELECT * FROM marginals")}
    a = recs["p_a"]
    assert a["eval_ms"] == T0 + 3000 and a["game"] == GAME and a["sport"] == "nfl" and a["tier"] == "ok"
    assert a["poly_bid"] == 0.41 and a["poly_at_ms"] == T0 + 2000 and a["poly_staleness_s"] == 1.0
    assert a["kalshi_yes_bid"] is not None and a["kalshi_ticker"].startswith("SYN-") and a["w_kalshi"] > 0
    assert a["inplay"] == 0 and '"c_thin"' in a["config"] and a["flags"] == ""
    assert a["poly_age_s"] == 1.0 and a["poly_confirmed_ms"] == T0 + 2000 and a["kalshi_confirmed_ms"] is None
    assert recs["p_c"]["p"] is None and recs["p_c"]["tier"] == "skip" and "no_quote" in recs["p_c"]["flags"]
    # appends on the next run
    main(["--game", GAME, "--at-ms", str(T0 + 3000), "--books-db", books_db, "--rfq-db", rfq_db, "--log", "--log-db", str(log_db)])
    assert conn.execute("SELECT COUNT(*) FROM marginals").fetchone()[0] == 6
    conn.close()
    rc = main(["--game", "nba-nope-nope-2026-01-01", "--books-db", books_db, "--rfq-db", rfq_db])
    assert rc == 1


def test_log_schema_migrates_old_tables(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    log_db = tmp_path / "old.db"
    conn = sqlite3.connect(log_db)
    old = [c for c, _ in LOG_COLUMNS if not c.endswith("_age_s") and not c.endswith("_confirmed_ms")]
    conn.execute("CREATE TABLE marginals (" + ", ".join(f"{c} TEXT" for c in old) + ")")
    conn.execute(f"INSERT INTO marginals ({','.join(old)}) VALUES ({','.join('?' * len(old))})", ["x"] * len(old))
    conn.commit()
    assert main(["--game", GAME, "--at-ms", str(T0 + 3000), "--books-db", books_db, "--rfq-db", rfq_db, "--kalshi", "none",
                 "--log", "--log-db", str(log_db)]) == 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(marginals)")}
    assert {"poly_age_s", "kalshi_age_s", "poly_confirmed_ms", "kalshi_confirmed_ms"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM marginals").fetchone()[0] == 4
    conn.close()


def test_config_from_env():
    cfg = load_config(env={})
    assert cfg == MarginalConfig() and cfg.prior_for("nfl") == 2.0 and cfg.prior_for("mlb") == 1.0 and cfg.prior_for(None) == 1.0
    cfg = load_config(env={"MARGINAL_C_THIN": "0.5", "MARGINAL_GAP_SKIP": "0.2", "MARGINAL_KALSHI_PRIOR": "nfl=3, mlb=1.5"})
    assert cfg.c_thin == 0.5 and cfg.gap_skip == 0.2 and cfg.prior_for("nfl") == 3.0 and cfg.prior_for("nba") == 2.0
    assert cfg.prior_for("mlb") == 1.5 and '"gap_skip": 0.2' in cfg.to_json()
