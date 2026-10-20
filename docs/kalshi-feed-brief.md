# Brief: Kalshi live price feed + persisted leg matches

Written 2026-10-20 by the planning session (now `totalis-e0`; earlier totalis-94 / -4d).
Owner: Ruben. Send a summary to `totalis-e0` when done.

## Why

The marginal layer blends a Polymarket quote and a Kalshi quote per leg, precision-weighted.
We have Polymarket books (`rfqbot/books.py`) and Kalshi's market *catalog* (`kalshi-sync`,
`kalshi_markets` table), but no Kalshi *prices*. This task adds them, plus a persisted table of
which Kalshi ticker each Polymarket leg matched to, so the feed and the marginal layer do not
have to recompute matches.

## Verified facts (2026-10-20)

- Order books are **public**, no auth: `GET /markets/{ticker}/orderbook?depth=N` returns
  `{"orderbook_fp": {"yes_dollars": [[price, size], ...], "no_dollars": [[price, size], ...]}}`
  with strings, ascending by price. These are resting **bids** on each side: best yes bid is
  the last `yes_dollars` level; best yes ask is `1 - best no bid` (sample: yes bid 0.13,
  no bid 0.86 -> yes ask 0.14, matching the market record). Verify this on a few markets and
  document it; do not assume asks are listed.
- `GET /markets?tickers=A,B,C` (batch) returns per-market `yes_bid_dollars`,
  `yes_ask_dollars`, `updated_time`. The legacy integer-cent fields (`yes_bid`, `yes_ask`,
  `volume`, `liquidity`) come back null now; use the `*_dollars` fields. Find the max batch
  size by probing.
- Unauthenticated bursts of ~1 req/s returned 429 earlier today. Authenticated requests get
  higher tiers. Auth = headers `KALSHI-ACCESS-KEY` (key id), `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE` = base64(RSA-PSS-SHA256 over `timestamp + METHOD + path`), per
  Kalshi's docs. `.env` now has `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (prod) and
  `KALSHI_DEMO_*`. **Never read the key file into logs, tests, fixtures or messages, and never
  copy it into the repo.** Add `cryptography` to `pyproject.toml` if you implement signing.
- Websocket `wss://api.elections.kalshi.com/trade-api/ws/v2` (channels `ticker`,
  `orderbook_delta`) requires the same auth headers on the upgrade.
- Kalshi quotes are fee-exclusive probabilities of YES in dollars. Taker fees are charged on
  top (roughly `0.07 * p * (1-p)` per contract, rounded up); record quotes as-is and leave fees
  to the pricing layer.
- Existing client: `rfqbot/kalshi.py` (`KalshiClient`, async httpx, public reads).
  `kalshi_markets` holds every synced game-level market with its parsed `canonical_key`,
  `game_key`, family, period, team, line, side. Matching is in `rfqbot/sports/matcher.py`
  (`KalshiIndex`, `match`, `match_all`, `MatchResult` with confidence
  `exact | rules_differ | shifted_line | none` and notes).

## Deliverables

1. **Persisted matches.** A table `leg_matches(position_id PRIMARY KEY, sport, canonical_key,
   game_key, kalshi_ticker, kalshi_side, confidence, notes_json, line_candidates_json,
   matched_at_ms)` in a new sqlite file `data/kalshi_books.db` whose schema lives in your
   module (do not edit `rfqbot/store.py`, owned by `totalis-47`). Fill it for every parsed
   Polymarket leg of registered sports (`rfqbot.sports.registry`) by running the shared
   matcher against the current `kalshi_markets`. Refresh after each `kalshi-sync` and on a
   timer; a leg whose confidence changes is re-written, never silently kept.
2. **Price feed** `rfqbot/kalshi_books.py` (you own it), run as a separate process like
   `books`:
   - Universe: every open game-level `kalshi_markets` row for registered sports (about 8k
     tickers today), swept with the batch endpoint for top-of-book every
     `KALSHI_SWEEP_SEC` (default 15), authenticated when keys are present, unauthenticated
     otherwise with a slower default.
   - Depth: for tickers in `leg_matches` whose leg appeared in RFQ flow in the last
     `BOOK_UNIVERSE_HOURS`, poll `/orderbook?depth=5` every `KALSHI_DEPTH_SEC` (default 30).
   - Storage mirrors `books.db`: change-only rows in `kalshi_top(ticker, at_ms, server_ts_ms,
     yes_bid, yes_ask, bid_size, ask_size, bid_depth, ask_depth, source)` with prices as
     probabilities, plus a gz raw log of every response (`KALSHI_LOG_DIR`, "none" disables).
   - Optional v1.5: the websocket `ticker` / `orderbook_delta` channels with auth, if it is
     straightforward; keep the REST sweep as fallback either way.
   - Rate-limit handling: back off on 429, log the effective request rate, never hammer.
3. **Read API** in the same module: `kalshi_quotes(tickers, at_ms=None) -> dict[ticker,
   KalshiQuote]` (latest, or latest at-or-before `at_ms`, never a future row) and
   `kalshi_for_legs(position_ids, at_ms=None) -> dict[position_id, (MatchRow, KalshiQuote |
   None)]` joining through `leg_matches`. `KalshiQuote`: ticker, yes_bid, yes_ask, bid_size,
   ask_size, bid_depth, ask_depth, at_ms, server_ts_ms. The marginal layer (`totalis-48`) is
   coded against exactly these names; tell it before changing them.
4. **Stats** command (standalone module main, `python -m rfqbot.kalshi_books --stats`, since
   `__main__.py` belongs to `totalis-47`; send it a snippet if you want a top-level
   subcommand): tickers swept, share with a quote in the last 5 min, matches by confidence,
   request rate and 429 count.
5. **Tests**: orderbook parsing (bids-only semantics, ask = 1 - no bid), batch parsing of the
   `*_dollars` fields, change-only writes, point-in-time read semantics, leg_matches refresh
   (mocked client, fixtures from real responses with no credentials in them).

## Ownership

You own `rfqbot/kalshi_books.py`, `tests/test_kalshi_books.py`, `tests/fixtures/kalshi_books_*`,
and the Kalshi settings block in `rfqbot/config.py` (coordinate with `totalis-6c`, which owns
the book settings in the same file: append your own clearly delimited block, do not reorder
theirs). `rfqbot/kalshi.py` and `rfqbot/sports/matcher.py` are `totalis-47`'s; ask it for
changes. Not a git repo: do not edit files you do not own. Keep the suite green (164 now).

## Report back

Batch size limit found, effective request rates with and without auth, 429s, orderbook side
semantics as verified, tickers covered and quote freshness, and match counts by confidence.
