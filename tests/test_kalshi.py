import json
from pathlib import Path

import httpx

from rfqbot.kalshi import KalshiClient, KalshiSync, market_row, spec_from_row
from rfqbot.sports.coverage import check_gamma, load_kalshi_index, report
from rfqbot.sports.legspec import canonical_key
from rfqbot.sports.nfl_poly import parse_poly_nfl
from rfqbot.sports.registry import get as get_sport

FIX = Path(__file__).parent / "fixtures"
MARKETS = json.loads((FIX / "kalshi_nfl_markets.json").read_text())


def fake_api():
    """Serves the fixture markets for KXNFLSPREAD (two pages) and KXNFLGAME; empty elsewhere."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        assert request.url.path.endswith("/markets")
        series = request.url.params["series_ticker"]
        rows = [m for m in MARKETS if m["ticker"].startswith(series + "-")]
        if series == "KXNFLSPREAD":
            if request.url.params.get("cursor") == "p2":
                return httpx.Response(200, json={"markets": rows[2:], "cursor": ""})
            return httpx.Response(200, json={"markets": rows[:2], "cursor": "p2"})
        return httpx.Response(200, json={"markets": rows, "cursor": None})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_sync_stores_parsed_rows(store):
    client, calls = fake_api()
    kc = KalshiClient(client=client, min_gap_sec=0)
    res = await KalshiSync(store, kc).sync("nfl")
    assert res["KXNFLSPREAD"] == (4, 0) and res["KXNFLGAME"][0] >= 2
    assert sum(1 for u in calls if u.params["series_ticker"] == "KXNFLSPREAD") == 2
    rows = list(store.iter_kalshi_markets("nfl", parsed_only=True))
    assert len(rows) == sum(n for n, _ in res.values())
    r = next(x for x in rows if x["ticker"] == "KXNFLSPREAD-26OCT26DALPHI-PHI8")
    assert (r["family"], r["team"], r["line"], r["side"], r["game_date"]) == ("spread", "PHI", -7.5, "PHI", "2026-10-26")
    assert r["canonical_key"] == "nfl|2026-10-26|DAL@PHI|FG|spread|PHI|-7.5|PHI"
    assert json.loads(r["rules_json"])["cancel"] == "unstated"
    assert canonical_key(spec_from_row(r)) == r["canonical_key"]
    idx, n, errs = load_kalshi_index(store, "nfl")
    assert n == len(rows) and idx.n == n and not errs
    # re-sync is idempotent (upsert); a row from an older batch is excluded with latest_only
    store.upsert_kalshi_markets([{**dict(r), "ticker": "KXNFLGAME-26NOV02CHISEA-OLD", "synced_at_ms": r["synced_at_ms"] - 1}])
    await KalshiSync(store, KalshiClient(client=fake_api()[0], min_gap_sec=0)).sync("nfl")
    assert store.scalar("SELECT COUNT(*) FROM kalshi_markets") == len(rows) + 1
    assert len(list(store.iter_kalshi_markets("nfl", latest_only=True))) == len(rows)
    assert load_kalshi_index(store, "nfl")[1] == len(rows)


def test_market_row_records_parse_errors():
    m = next(x for x in MARKETS if x["ticker"].startswith("KXNFLRACE"))
    r = market_row(m, "nfl", 0)
    assert r["parse_ok"] == 0 and r["parse_error"] and r["canonical_key"] is None


async def test_client_retries_on_429():
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"markets": [], "cursor": None})

    async with KalshiClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_gap_sec=0) as c:
        assert await c.markets("KXNFLGAME") == [] and n["calls"] == 2


def test_coverage_report_runs_offline(store):
    rows = json.loads((FIX / "poly_nfl_rows.json").read_text())
    store.upsert_markets([{**r, "outcome_price": None, "volume": None, "tags": '["sports", "nfl", "games"]', "synced_at_ms": 0} for r in rows])
    store.upsert_kalshi_markets([market_row(m, "nfl", 0) for m in MARKETS])
    lines = []
    res = report(store, "nfl", out=lines.append)
    text = "\n".join(lines)
    assert res["failed"] == 0 and res["parsed"] == len(rows)
    assert "exact" in text and "unparsed rows: none" in text
    assert res["confidence"]["exact"] >= 4


def test_check_gamma_agrees_with_fixture_and_flags_corruption():
    sp = get_sport("nfl")
    rows = {r["slug"]: [] for r in json.loads((FIX / "poly_nfl_rows.json").read_text())}
    for r in json.loads((FIX / "poly_nfl_rows.json").read_text()):
        rows[r["slug"]].append(parse_poly_nfl(r["slug"], r["title"], r["outcome"], r["outcome_index"], position_id=r["position_id"]))
    checked = 0
    for m in json.loads((FIX / "gamma_nfl_markets.json").read_text()):
        specs = rows.get(m["slug"])
        if not specs:
            # fixture market not in the poly rows: parse from Gamma question/outcomes to still exercise the check
            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
            pids = json.loads(m["positionIds"]) if isinstance(m["positionIds"], str) else m["positionIds"]
            specs = [parse_poly_nfl(m["slug"], m["question"], o, i, position_id=str(pids[i])) for i, o in enumerate(outcomes)]
        assert check_gamma(m, specs, sp) == [], m["slug"]
        checked += 1
        bad = {**m, "line": (m["line"] or 0) + 1, "sportsMarketType": "totals" if m["sportsMarketType"] != "totals" else "spreads",
               "gameStartTime": "2026-12-25 00:00:00+00"}
        problems = check_gamma(bad, specs, sp)
        assert any("sportsMarketType" in p for p in problems) and any("date" in p for p in problems)
    assert checked >= 10
