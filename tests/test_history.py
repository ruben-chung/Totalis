"""History package: pull from recorded Gamma fixtures, parser round trips, price snapshots, resolve, pairs, export."""
import json
from pathlib import Path

import httpx
import pytest

from rfqbot.history import HistoryStore, game_markets_hist
from rfqbot.history.gamma import Http, is_side_event
from rfqbot.history.pairs import build_pairs
from rfqbot.history.prices import coverage_by_offset, pick_points, snapshot_prices, tokens_to_check
from rfqbot.history.pull import pull, reparse, resolution
from rfqbot.history.resolve import candidate_markets, outcome_name, resolve
from rfqbot.store import Store

FX = json.loads((Path(__file__).parent / "fixtures" / "history" / "nfl_event.json").read_text())


class FakeClient:
    """Stands in for httpx.AsyncClient: serves the fixture for /events, /markets and /prices-history."""

    def __init__(self, *, fail_first_prices: bool = False):
        self.calls = []
        self.fail_first_prices = fail_first_prices

    async def get(self, url, params=None):
        self.calls.append((url.rsplit("/", 1)[-1], params))
        req = httpx.Request("GET", url)
        if url.endswith("/events"):
            offset = int(dict(params).get("offset", 0))
            evs = [FX["main"]] + ([FX["side"]] if "side" in FX else []) + ([FX["props"]] if "props" in FX else [])
            return httpx.Response(200, json=evs if offset == 0 else [], request=req)
        if url.endswith("/markets"):
            want = {v for k, v in params if k == "condition_ids"}
            closed = ("closed", "true") in params           # Gamma: closed markets only come back with closed=true
            mks = [m for ev in (FX["main"], FX.get("side", {}), FX.get("props", {})) for m in ev.get("markets", [])
                   if m["conditionId"].lower() in want and bool(m.get("closed")) == closed]
            return httpx.Response(200, json=mks, request=req)
        if url.endswith("/prices-history"):
            if self.fail_first_prices:
                self.fail_first_prices = False
                return httpx.Response(503, text="busy", request=req)
            if params["market"] == FX["prices"]["token_id"]:
                return httpx.Response(200, json={"history": FX["prices"]["history"]}, request=req)
            return httpx.Response(200, json={"history": []}, request=req)
        return httpx.Response(404, request=req)

    async def aclose(self):
        pass


def _http(**kw) -> Http:
    return Http(pause_s=0, client=FakeClient(**kw), retries=2)


@pytest.fixture
def hist():
    s = HistoryStore(None)
    yield s
    s.close()


def test_resolution_rules():
    assert resolution({"umaResolutionStatus": "resolved", "outcomePrices": '["1", "0"]'}) == (1, [1, 0])
    assert resolution({"umaResolutionStatus": "resolved", "outcomePrices": '["0.5", "0.5"]'}) == (0, [None, None])
    assert resolution({"umaResolutionStatus": "proposed", "outcomePrices": '["1", "0"]'}) == (0, [None, None])
    assert outcome_name({"umaResolutionStatus": "resolved", "outcomePrices": '["0.5","0.5"]', "outcomes": '["A","B"]'}) == "VOID"
    assert outcome_name({"umaResolutionStatus": "resolved", "outcomePrices": '["0","1"]', "outcomes": '["A","B"]'}) == "B"
    assert is_side_event("nfl-was-sf-2026-10-20-player-props") and not is_side_event("nfl-was-sf-2026-10-20")


async def test_pull_ingests_main_and_side_events_and_parses_legs(hist):
    counts = await pull(hist, "nfl", since="2026-09-01", http=_http())
    n_fixture = sum(len(FX[k]["markets"]) for k in ("main", "side", "props") if k in FX)
    assert counts["markets"] == n_fixture and counts["games"] == 1
    assert counts["side_events"] == sum(1 for k in ("side", "props") if k in FX)
    g = hist.rows("SELECT * FROM hist_games")[0]
    assert g["n_markets"] == n_fixture and g["game_start_time"] and json.loads(g["event_slugs"])[0] == FX["main"]["slug"]
    # every main-event market parsed into LegSpec fields with the right families; both sides present with won 0/1
    legs = hist.rows("SELECT l.*, m.sports_market_type FROM hist_legs l JOIN hist_markets m USING(condition_id)"
                     " WHERE m.event_slug = ?", FX["main"]["slug"])
    assert legs and all(l["parse_error"] is None for l in legs), [l["parse_error"] for l in legs if l["parse_error"]]
    fam = {l["sports_market_type"]: l["family"] for l in legs}
    assert fam["spreads"] == "spread" and fam["totals"] == "total" and fam["first_half_spreads"] == "spread"
    assert {l["period"] for l in legs if l["sports_market_type"] == "first_half_spreads"} == {"1H"}
    by_market = {}
    for l in legs:
        by_market.setdefault(l["condition_id"], []).append(l["won"])
    assert all(sorted(v) == [0, 1] for v in by_market.values())
    assert all(l["canonical_key"].startswith("nfl|") and l["token_id"] for l in legs)
    # player props do not parse but are kept with the error
    if "props" in FX:
        p = hist.rows("SELECT parse_error FROM hist_legs l JOIN hist_markets m USING(condition_id) WHERE m.event_slug LIKE '%player-props'")
        assert p and all(r[0] for r in p)
    # idempotent
    await pull(hist, "nfl", since="2026-09-01", http=_http())
    assert hist.scalar("SELECT COUNT(*) FROM hist_markets") == n_fixture


async def test_pull_since_stops_at_older_events(hist):
    counts = await pull(hist, "nfl", since="2099-01-01", http=_http())
    assert counts["events"] == 0 and counts["skipped_old"] == 1


def test_pick_points_is_point_in_time():
    kick = 10_000
    hist = [{"t": kick - 3600, "p": 0.5}, {"t": kick - 900, "p": 0.6}, {"t": kick - 300, "p": 0.7}, {"t": kick - 10, "p": 0.9}]
    pts = pick_points(hist, kick, (60, 15, 5))
    assert pts == {60: (kick - 3600, 0.5), 15: (kick - 900, 0.6), 5: (kick - 300, 0.7)}   # never a later point
    assert pick_points(hist[-1:], kick, (60,)) == {}                                       # nothing early enough


async def test_prices_snapshot_incremental_and_backoff(hist):
    await pull(hist, "nfl", since="2026-09-01", http=_http())
    http = _http(fail_first_prices=True)
    c = await snapshot_prices(hist, sport="nfl", offsets=(60, 15, 5), http=http)
    n_tokens = len(hist.rows("SELECT token_id FROM hist_legs l JOIN hist_markets m USING(condition_id)"
                             " WHERE m.resolved=1 AND l.parse_error IS NULL AND l.token_id IS NOT NULL"))
    assert c["tokens"] == n_tokens and c["with_history"] == 1 and c["no_history"] == n_tokens - 1 and c["errors"] == 0
    tok = FX["prices"]["token_id"]
    rows = {r["offset_min"]: (r["t"], r["p"]) for r in hist.rows("SELECT * FROM hist_prices WHERE token_id=?", tok)}
    expected = pick_points(FX["prices"]["history"], FX["prices"]["kickoff_s"], (60, 15, 5))
    assert rows == expected and rows
    # rerun: everything is cached (misses included), no CLOB calls
    calls_before = len([c for c in http.client.calls if c[0] == "prices-history"])
    c2 = await snapshot_prices(hist, sport="nfl", offsets=(60, 15, 5), http=http)
    assert c2["tokens"] == 0 and len([c for c in http.client.calls if c[0] == "prices-history"]) == calls_before
    assert tokens_to_check(hist, sport="nfl", offsets=(30,))                 # a new offset set is checked again
    cov = dict((o, (h, n)) for o, h, n in coverage_by_offset(hist, sport="nfl"))
    assert cov[5] == (1, n_tokens)


async def test_game_markets_hist_file(tmp_path):
    st = HistoryStore(tmp_path / "h.db")
    await pull(st, "nfl", since="2026-09-01", http=_http())
    await snapshot_prices(st, sport="nfl", offsets=(60, 15, 5), http=_http())
    prefix = st.scalar("SELECT game_prefix FROM hist_games")
    qs = game_markets_hist(prefix, 5, history_db=st.path)
    assert len(qs) == st.scalar("SELECT COUNT(*) FROM hist_legs")
    priced = [q for q in qs if q.best_bid is not None]
    assert len(priced) == 1 and priced[0].best_bid == priced[0].best_ask and priced[0].bid_size is None and priced[0].at_ms > 0
    assert priced[0].token_id == FX["prices"]["token_id"] and priced[0].game_start_time
    assert all(q.at_ms == 0 and q.best_ask is None for q in qs if q.best_bid is None)
    assert len(game_markets_hist(prefix, 5, history_db=st.path, include_unpriced=False)) == 1
    # the engine's fair rule accepts it
    from rfqbot.pricing.quotes import fair_from_quote
    f = fair_from_quote(priced[0])
    assert f is not None and f.p == priced[0].best_bid
    st.close()


async def test_resolve_writes_rfq_db_rows_and_is_timer_safe(tmp_path):
    rfq = Store(tmp_path / "rfq.db")
    mk = FX["main"]["markets"]
    # two markets seen in RFQ flow: one from the fixture (resolved), one unknown to Gamma; plus one with a future endDate
    rfq.upsert_markets([{"position_id": "pA", "market_id": "1", "condition_id": mk[0]["conditionId"], "slug": mk[0]["slug"], "title": "t",
                         "outcome": "Yes", "outcome_index": 0, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1},
                        {"position_id": "pB", "market_id": "2", "condition_id": "0xunknown", "slug": "x", "title": "t",
                         "outcome": "Yes", "outcome_index": 0, "outcome_price": 0.5, "volume": 0, "tags": "[]", "synced_at_ms": 1}])
    with rfq.tx():
        cid = rfq.combo_id("0xcombo", ["pA", "pB"], at_ms=1)
        rfq.insert_rfq({"rfq_id": "r1", "requester_id": None, "combo_id": cid, "direction": "BUY", "side": "YES", "size_unit": "notional",
                        "size_value_e6": 1, "submission_deadline_ms": None, "first_seen_ms": 1, "status": "RECEIVED", "status_at_ms": 1, "stub": 0})
    rfq.close()
    st = HistoryStore(tmp_path / "h.db")
    st.record_resolve_check("0xfuture", "2099-01-01T00:00:00Z", None, 1)
    todo, skipped = candidate_markets(st, tmp_path / "rfq.db", now=10)
    assert set(todo) == {mk[0]["conditionId"].lower(), "0xunknown"} and skipped["end_date_not_passed"] == 0
    c = await resolve(st, tmp_path / "rfq.db", http=_http())
    assert c["written"] == 1 and c["not_in_gamma"] == 1 and c["written_local"] == 0
    rfq = Store(tmp_path / "rfq.db")
    rows = rfq.conn.execute("SELECT condition_id, resolved_outcome FROM market_resolutions").fetchall()
    assert len(rows) == 1 and rows[0][0] == mk[0]["conditionId"].lower()
    assert rows[0][1] in json.loads(mk[0]["outcomes"])
    rfq.close()
    # second run within the recheck window: nothing to do
    c2 = await resolve(st, tmp_path / "rfq.db", http=_http())
    assert c2["candidates"] == 0 and c2["already_resolved"] == 1 and c2["checked_recently"] == 1
    st.close()


async def test_pairs_cover_every_cross_market_pair_in_rank_order(hist):
    await pull(hist, "nfl", since="2026-09-01", http=_http())
    c = build_pairs(hist, sport="nfl")
    legs = hist.rows("SELECT condition_id, won FROM hist_legs WHERE won IS NOT NULL AND parse_error IS NULL")
    n = len(legs)
    same = sum(1 for _ in legs) // 2          # one same-market pair per market is excluded
    assert c["pairs"] == n * (n - 1) // 2 - same and c["games"] == 1
    assert hist.scalar("SELECT COUNT(*) FROM hist_pairs WHERE family_a='moneyline' AND period_a='FG' AND family_b='spread' AND period_b='FG'") > 0
    assert hist.scalar("SELECT COUNT(*) FROM hist_pairs WHERE cond_a = cond_b") == 0
    assert hist.scalar("SELECT COUNT(*) FROM hist_pairs WHERE won_a NOT IN (0,1) OR won_b NOT IN (0,1)") == 0
    # cap and idempotence
    assert build_pairs(hist, sport="nfl")["pairs"] == 0
    c3 = build_pairs(hist, sport="nfl", max_pairs_per_game=10, rebuild=True)
    assert c3["pairs"] == 10 and c3["capped_games"] == 1
    # the cap keeps the highest-ranked pairs: FG x FG, moneyline first
    assert hist.scalar("SELECT COUNT(*) FROM hist_pairs WHERE period_a='FG' AND period_b='FG'") == 10
    assert hist.scalar("SELECT COUNT(*) FROM hist_pairs WHERE family_a='moneyline'") == 10


async def test_resolve_uses_pulled_outcomes_without_gamma(tmp_path):
    rfq = Store(tmp_path / "rfq.db"); rfq.close()
    st = HistoryStore(tmp_path / "h.db")
    await pull(st, "nfl", since="2026-09-01", http=_http())
    http = _http()
    c = await resolve(st, tmp_path / "rfq.db", http=http)
    n_res = st.scalar("SELECT COUNT(*) FROM hist_markets WHERE resolved=1")
    assert c["written_local"] == n_res and c["candidates"] == 0 and c["local"] == 0
    assert not any(k == "markets" for k, _ in http.client.calls)
    rfq = Store(tmp_path / "rfq.db")
    assert rfq.scalar("SELECT COUNT(*) FROM market_resolutions") == n_res
    rfq.close(); st.close()


async def test_reparse_rewrites_legs_locally(hist, monkeypatch):
    await pull(hist, "nfl", since="2026-09-01", http=_http())
    n_fail = hist.scalar("SELECT COUNT(*) FROM hist_legs WHERE parse_error IS NOT NULL")
    # simulate a parser that now accepts everything
    import rfqbot.history.pull as pl
    monkeypatch.setattr(pl, "parse_leg", lambda *a, **k: ({"family": "total", "period": "FG", "side": "over", "team": None,
                                                           "line": 1.5, "bucket": None, "includes_ot": None, "game_key": "g",
                                                           "canonical_key": "c", "contract_key": "k"}, None))
    c = reparse(hist, "nfl", only_failed=True)
    assert c["newly_parsed"] == n_fail and c["failed"] == 0
    assert hist.scalar("SELECT COUNT(*) FROM hist_legs WHERE parse_error IS NOT NULL") == 0
    assert hist.scalar("SELECT COUNT(*) FROM hist_legs WHERE won IS NULL") == 0     # won recomputed from stored prices


async def test_client_error_404_is_not_retried():
    http = _http()
    with pytest.raises(httpx.HTTPStatusError):
        await http.get_json("https://gamma.example/nope")                      # FakeClient answers 404
    assert http.client.calls == [("nope", None)] and http.errors == 1          # one attempt, no backoff
