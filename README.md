# rfqbot — Polymarket Combos RFQ paper-trading bot

Stage 1 (this repo state): **RFQ listener and storage**. Later stages: pricing/correlation,
inventory/risk, backtest. See `polymarket-rfq-bot-spec.md`.

## Setup

```bash
uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env   # then fill in credentials
.venv/bin/python -m pytest -q
```

## Run

```bash
.venv/bin/python -m rfqbot listen                 # record until Ctrl-C
.venv/bin/python -m rfqbot listen --duration 600  # record for 10 minutes
.venv/bin/python -m rfqbot stats                  # counts from data/rfq.db
.venv/bin/python -m rfqbot catalog                # one-shot combo-markets sync (~200k markets, ~7 min)
.venv/bin/python -m rfqbot replay --from data/raw --to data/replay.db --overwrite
```

```bash
.venv/bin/python -m rfqbot books                  # record CLOB order books for legs seen in RFQ flow
.venv/bin/python -m rfqbot bookstats              # counts from data/books.db
```

`listen` runs three loops: the websocket (auth, keepalive, reconnect with jittered backoff),
a 1 s expiry sweeper, and a catalog refresh (every 6 h; a full sync is slow, so on-demand
refreshes for unknown legs are rate-limited to one per 30 min).

## What the feed looks like (measured 2026-10-20)

* Endpoint `wss://combos-rfq-gateway-quoter.polymarket.com/ws/rfq` streams `RFQ_REQUEST`,
  `RFQ_TRADE` and an undocumented `RFQ_UNMET_DEMAND_SUMMARY` (every 30 s) **to any connection,
  authenticated or not**. Auth is only needed to receive confirmation/execution frames for
  quotes we submit, so in paper mode the bot connects unauthenticated if credentials are
  incomplete and logs a warning. `--require-auth` makes a rejected auth fatal.
* Volume: ~190 RFQ_REQUEST/s (≈16M/day). ~85% BUY/notional, 90% two-leg. Requesters poll
  the same combo about once a second, so unique combos are ~45% of requests.
* No expire/cancel message reaches makers; expiry is derived: an RFQ still open after
  `submission_deadline + 10 s acceptance + 1 s last-look + EXPIRY_GRACE_MS` with no execution
  update becomes `EXPIRED`. Expiry is computed from message timestamps, never the wall clock
  during replay, so replays never see the future.

## Storage

* `data/raw/YYYY-MM-DD/HH.jsonl.gz` — **every** frame in and out, verbatim, gzip (≈4x,
  ≈2.5–3 GB/day). Sync-flushed every 2 s; a truncated file is readable up to the last flush.
* `data/rfq.db` (SQLite, WAL):
  * `raw_messages` — low-volume frames (everything except `RFQ_REQUEST`; `RAW_DB_TYPES=all` changes this)
  * `rfqs` — one row per RFQ (status machine: RECEIVED → QUOTED → SELECTED → EXECUTING → SETTLED/FAILED;
    side exits EXPIRED, FILLED_BY_OTHER, CANCELLED). ≈150 B/row → ≈2.5 GB/day at current volume.
  * `combos`, `combo_legs`, `requesters` — interned; legs belong to the combo
  * `rfq_events` — quote/confirmation/execution/trade lifecycle (RECEIVED/EXPIRED are implied by `rfqs.status`)
  * `quotes` — every draft revision (paper or real), with state and `reason`
  * `executions`, `trades` (deduped on `rfq_id`), `settlements`, `unmet_demand(_items)`
  * `markets` — catalog of outcome positions (position_id → market, outcome, price, tags)
  * views `rfqs_v` (RFQ joined with requester and combo) and `rfq_legs` (legs joined with markets)
* `python -m rfqbot replay` rebuilds a database from the raw log; it is the entry point the
  backtest will use.

## Leg order books (`rfqbot books`)

The combo-markets catalog price is a placeholder, so pricing and the backtest need point-in-time
leg books. `books` is a second process that reads the listener's `rfq.db` (read-only) for the leg
`position_id`s of RFQs seen in the last `BOOK_UNIVERSE_HOURS`, maps them to CLOB token ids, and
subscribes to the CLOB market channel (`wss://ws-subscriptions-clob.polymarket.com/ws/market`).

* **An RFQ `position_id` is not a CLOB token id** (the market channel answers `[]` for them).
  `TokenMap` resolves position → (condition_id, outcome) via the catalog → `clobTokenIds` via
  Gamma `/markets?condition_ids=…` (100 per request), cached in `books.db.leg_tokens` together
  with Gamma's `event_id`, `game_start_time` and `end_date`.
* Tokens are spread over connections of `BOOK_ASSETS_PER_CONN` (500) and added/removed with the
  channel's dynamic `subscribe`/`unsubscribe` frames; the client sends `PING` every 10 s.
* **Whole-game ladders** (`BOOK_GAME_SPORTS`, default `nfl,nba`): any game an RFQ leg touches is
  widened to every catalog market under its slug prefix (`{league}-{away}-{home}-{YYYY-MM-DD}`),
  tokens filled from Gamma `/events?slug={game}` in one call, games ranked by RFQ count under
  `BOOK_GAME_MAX_TOKENS` and skipped beyond `BOOK_GAME_LOOKAHEAD_HOURS`. Other sports stay leg-level.
  `rfqbot.ladders.game_markets(prefix, at_ms=None)` reads a game's ladder (latest or point-in-time).
* `data/books/YYYY-MM-DD/HH.jsonl.gz` — every frame verbatim (same format as the RFQ raw log).
* `data/books.db`: `book_top` (one row whenever an asset's best bid/ask, size at best or depth
  within 0.05 changes; prices as integers ×10 000), `leg_trades` (`last_trade_price` events),
  `tick_sizes`, `subscriptions` (when each token was in the set), `leg_tokens`, `book_connections`.
  Deltas for a token without a snapshot (the complement token's deltas arrive too) are raw-logged
  but not normalized.

## Historical dataset (`rfqbot.history`, `data/history.db`)

Resolved games from Gamma for engine validation and backtest P&L:

```bash
.venv/bin/python -m rfqbot.history pull --sport nfl --since 2026-09-01   # closed events (main + side) -> hist_games/markets/legs
.venv/bin/python -m rfqbot.history prices --minutes-before 60,15,5       # pre-kickoff CLOB price snapshots -> hist_prices
.venv/bin/python -m rfqbot.history resolve                                # rfq.db market_resolutions for RFQ legs + history
.venv/bin/python -m rfqbot.history pairs --sport nfl                      # every pair of legs per resolved game -> hist_pairs
.venv/bin/python -m rfqbot.history stats
```

* Series: `nfl-2026`, `nba-2026` (the NBA series spans the 2025-26 season and the current preseason). NFL
  player props are side events (`-player-props`, `-first-td-scorer`, `-highest-scoring-quarter`); NBA props
  live inside the main game event. Legs are parsed with the registered sport parser; unparsed ones keep the error.
* Resolved = Gamma `umaResolutionStatus` resolved and `outcomePrices` exactly 0/1 (0.5/0.5 = VOID).
* Gamma's `/markets?condition_ids=` returns only **open** markets unless `closed=true`; `resolve` does both.
* `rfqbot.history.game_markets_hist(game_prefix, offset_min)` returns `MarketQuote`s with bid = ask = snapshot
  price so `rfqbot.pricing.joint.build_game` runs on history unchanged.

## Plugging in pricing (next stage)

`Listener(on_rfq=handler)`: the handler receives the parsed `RfqRequest` and the `Store` and
returns a `PaperQuote(price_e6, size_e6, source, reason)` or `None`. Returned quotes are
stored as revisions in `quotes` and logged to the raw log as the `RFQ_QUOTE` frame that would
have been sent. `PAPER_TRADING=false` is rejected until real signing exists.

## Stage 2a: market identity and Kalshi matching (`rfqbot/sports/`)

Deterministic, table-driven parsers turn venue markets into a sport-agnostic `LegSpec`
(game, family, period, team, line, side, settlement rules) so legs can be compared across
venues and same-game legs can be detected. No fuzzy matching: unknown input raises.

```bash
.venv/bin/python -m rfqbot kalshi-sync --sport nfl        # game-level Kalshi series -> kalshi_markets (public API, ~45 requests)
.venv/bin/python -m rfqbot nfl-coverage                    # parse % by family/period, match confidence, rule differences
.venv/bin/python -m rfqbot nfl-coverage --gamma --sample 60  # also cross-check a sample against the Gamma API
.venv/bin/python -m rfqbot coverage --sport nfl            # same, generic entry point for other sports
```

* `sports/legspec.py` — `LegSpec`, `canonical_key` (venue-independent contract identity, e.g.
  `nfl|2026-10-25|IND@MIN|FG|spread|MIN|-7.5|MIN`), `complement`, `register_family`.
* `sports/registry.py` — per-sport registration (Poly/Kalshi parsers, Kalshi series, slug-date
  convention: NFL slugs carry the UTC kickoff date, NBA slugs the Eastern date, Kalshi always Eastern).
* `sports/nfl_teams.py`, `nfl_poly.py`, `nfl_kalshi.py` — NFL tables and parsers (see module docstrings
  for the verified grammars and sign conventions).
* `sports/matcher.py` — `match(poly_spec, KalshiIndex)` -> `exact | rules_differ | shifted_line | none`
  with every settlement difference listed in `notes`.
* `kalshi.py` — public-API client (rate limited, retrying) and the `kalshi_markets` table.
* Findings and coverage numbers: `docs/nfl-matching-notes.md`.

## Stage 2b: market-implied joint distribution (`rfqbot/pricing/`)

Reads a game's line ladders (spreads, totals, team totals, first half) into a joint score
distribution and prices any set of legs jointly from 50k scenarios, with an interval.

```bash
.venv/bin/python -m rfqbot joint --game nfl-ne-chi-2026-10-23 --pairs   # from recorded books (rfqbot.ladders)
.venv/bin/python -m rfqbot joint --game nfl-dal-phi-2026-10-27 --synthetic --pairs   # known-truth demo
.venv/bin/python -m rfqbot joint-validate --offsets 15            # score the engine on resolved games (history.db)
.venv/bin/python -m rfqbot joint-calibrate --scores-db data/engine_validation_v2.db   # refit rfqbot/pricing/calibration.json
```

API: `rfqbot.pricing.build_game(quotes) -> GameModel`; `GameModel.joint(specs) -> JointQuote`
(`p_joint`, `p_independent`, Fréchet bounds, shock interval `lo/hi`, engine marginals,
`max_marginal_error`); `GameModel.marginal_check()`, `GameModel.correlation_table()`.
Design and findings: `docs/joint-engine-notes.md`.

## Stage 2c: quote assembly (`rfqbot/quoting/`)

`listen --quote` turns each RFQ into a paper quote (off by default; `PAPER_TRADING` stays true):
catalog legs -> `rfqbot.clusters` -> per-leg marginals (`rfqbot.marginals`, cached per game by a
background task) -> joint engine for same-game NFL clusters -> `p_fair` = product over clusters ->
margin = `k_m u_m + k_d u_d + base_edge + inventory` -> price for the maker's side -> size caps.
Every RFQ is logged with all terms in `data/decisions.db` (the backtest's input).

```bash
.venv/bin/python -m rfqbot listen --quote --duration 600            # paper quotes for 10 minutes
QUOTE_BASE_EDGE=0.01 QUOTE_MAX_SIZE_USD=200 .venv/bin/python -m rfqbot listen --quote
```

Combo semantics (docs + recorded frames) are pinned in `rfqbot/quoting/semantics.py`; design and
the first live run in `docs/quote-assembly-notes.md`.
