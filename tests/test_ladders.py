"""game_markets: latest and point-in-time semantics over a small books.db + rfq.db."""
from rfqbot.books import BookStore
from rfqbot.ladders import MarketQuote, game_coverage, game_markets
from rfqbot.store import Store


def _market(pid, slug, title, outcome, idx, cond="0xc"):
    return {"position_id": pid, "market_id": "1", "condition_id": cond, "slug": slug, "title": title, "outcome": outcome,
            "outcome_index": idx, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1}


def _dbs(tmp_path):
    rfq = Store(tmp_path / "rfq.db")
    rfq.upsert_markets([
        _market("p_ml_a", "nfl-pit-no-2026-10-25", "Steelers vs. Saints", "Steelers", 0),
        _market("p_ml_b", "nfl-pit-no-2026-10-25", "Steelers vs. Saints", "Saints", 1),
        _market("p_sp_a", "nfl-pit-no-2026-10-25-spread-home-3pt5", "Spread", "Steelers", 0, "0xs"),
        _market("p_sp_b", "nfl-pit-no-2026-10-25-spread-home-3pt5", "Spread", "Saints", 1, "0xs"),
        _market("p_other", "nfl-pit-no-2026-10-26-spread-home-1pt5", "Other game", "X", 0, "0xo"),
    ])
    rfq.close()
    books = BookStore(tmp_path / "books.db")
    books.upsert_tokens([
        ("p_ml_a", "0xc", 0, "Steelers", "tok_ml_a", "nfl-pit-no-2026-10-25", "1", "2026-10-25 13:30:00+00", None, 1),
        ("p_ml_b", "0xc", 1, "Saints", "tok_ml_b", "nfl-pit-no-2026-10-25", "1", "2026-10-25 13:30:00+00", None, 1),
        ("p_sp_a", "0xs", 0, "Steelers", "tok_sp_a", "nfl-pit-no-2026-10-25-spread-home-3pt5", "1", "2026-10-25 13:30:00+00", None, 1),
        # p_sp_b has no token yet -> left out
    ])
    books.begin()
    books.insert_top("tok_ml_a", 1000, 999, (4000, 4200, 10.0, 20.0, 15.0, 25.0), "book")
    books.insert_top("tok_ml_a", 2000, 1999, (4100, 4300, 11.0, 21.0, 16.0, 26.0), "delta")
    books.insert_top("tok_ml_a", 3000, 2999, (4200, 4400, 12.0, 22.0, 17.0, 27.0), "delta")
    books.insert_top("tok_ml_b", 1500, None, (5600, None, 5.0, None, 5.0, None), "book")   # one-sided book
    books.close()
    return tmp_path / "books.db", tmp_path / "rfq.db"


def test_latest_rows_and_missing_books(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    qs = game_markets("nfl-pit-no-2026-10-25", books_db=books_db, rfq_db=rfq_db)
    assert [q.position_id for q in qs] == ["p_ml_a", "p_ml_b", "p_sp_a"]      # other game and untokenized leg excluded
    a = qs[0]
    assert isinstance(a, MarketQuote) and a.token_id == "tok_ml_a" and a.at_ms == 3000
    assert (a.best_bid, a.best_ask, a.bid_size, a.ask_size, a.bid_depth, a.ask_depth) == (0.42, 0.44, 12.0, 22.0, 17.0, 27.0)
    assert a.mid == 0.43 and a.game_start_time == "2026-10-25 13:30:00+00" and a.title == "Steelers vs. Saints"
    b = qs[1]
    assert b.best_bid == 0.56 and b.best_ask is None and b.mid is None
    sp = qs[2]
    assert sp.at_ms == 0 and sp.best_bid is None and sp.best_ask is None and sp.slug.endswith("spread-home-3pt5")


def test_point_in_time_never_uses_future_rows(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    by = {q.position_id: q for q in game_markets("nfl-pit-no-2026-10-25", at_ms=2500, books_db=books_db, rfq_db=rfq_db)}
    assert by["p_ml_a"].at_ms == 2000 and by["p_ml_a"].best_bid == 0.41
    by = {q.position_id: q for q in game_markets("nfl-pit-no-2026-10-25", at_ms=2000, books_db=books_db, rfq_db=rfq_db)}
    assert by["p_ml_a"].at_ms == 2000                                        # inclusive
    by = {q.position_id: q for q in game_markets("nfl-pit-no-2026-10-25", at_ms=500, books_db=books_db, rfq_db=rfq_db)}
    assert by["p_ml_a"].at_ms == 0 and by["p_ml_a"].best_bid is None        # nothing known yet at that time
    assert by["p_ml_b"].at_ms == 0


def test_ro_alias_is_shared_connector():
    """`ladders._ro` stays importable for one release (rfqbot.marginals.sources) and is rfqbot.db.ro_connect."""
    from rfqbot.db import ro_connect
    from rfqbot.ladders import _ro
    assert _ro is ro_connect


def test_unknown_game_and_coverage(tmp_path):
    books_db, rfq_db = _dbs(tmp_path)
    assert game_markets("nba-nope-nope-2026-01-01", books_db=books_db, rfq_db=rfq_db) == []
    cov = game_coverage("nfl-pit-no-2026-10-25", books_db=books_db, rfq_db=rfq_db)
    assert cov == {"catalog_positions": 4, "with_token": 3, "with_book": 2}
