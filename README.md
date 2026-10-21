# rfqbot — Polymarket Combos RFQ paper-trading bot

A Python 3.11 bot that listens to Polymarket's combinatorial-market RFQ feed, records everything it
sees, prices each combo from live order books on Polymarket and Kalshi with an explicit dependence
model for same-game legs, and logs the paper quote it would have sent. Built against
`polymarket-rfq-bot-spec.md`.

## Status by spec stage

| stage | state |
|---|---|
| 1. RFQ listener and storage | done: websocket client with reconnect, verbatim raw log, normalized SQLite store, derived expiry, replay |
| 2. Pricing and correlation | done in paper mode: NFL and NBA leg parsers and Kalshi matchers, whole-game ladder books, Kalshi price feed, precision-weighted marginal layer, leg independence classifier, NFL joint score engine with validation on resolved games and a per-pair calibration layer, quote assembly (`listen --quote`) |
| 3. Inventory and risk | not built; the margin formula carries an `inventory` slot that is 0 today |
| 4. Backtest | not built; the inputs exist (`data/raw`, `data/decisions.db`, point-in-time books, `market_resolutions`) |

What would come next, and every extension identified along the way, is in `docs/future-work.md`.

## Setup

```bash
uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env            # optional: the public feeds need no credentials
.venv/bin/python -m pytest -q   # ~300 tests; three files skip without data/ (see Data and storage)
```

## Run

Four long-running processes, each its own `python -m` entry point:

```bash
scripts/run.sh rfqbot listen --quote      # RFQ listener (+ paper quotes)   -> data/rfq.db, data/raw/, data/decisions.db
scripts/run.sh rfqbot.books               # Polymarket CLOB leg books and whole-game ladders -> data/books.db
scripts/run.sh rfqbot.kalshi_books        # Kalshi top-of-book sweep, depth polls, leg matches -> data/kalshi_books.db
scripts/run.sh rfqbot.history pull --sport nfl --since 2026-09-01   # one-shot jobs work the same way
scripts/status.sh                         # PID, module and last log line of every running rfqbot process
```

`scripts/run.sh <module> [args]` starts a module exactly once (it refuses if a process for that
module is already running), detached under `caffeinate`, logging to `data/logs/<module>.log`.
One-shot commands run directly with `.venv/bin/python -m ...`:

```bash
.venv/bin/python -m rfqbot --help
# listen, catalog, replay, stats, books, bookstats, kalshi-sync, joint, joint-validate,
# joint-calibrate, nfl-coverage, coverage
.venv/bin/python -m rfqbot listen --duration 600            # record for 10 minutes, no quoting
.venv/bin/python -m rfqbot stats                            # counts from data/rfq.db
.venv/bin/python -m rfqbot catalog                          # combo-markets catalog sync (~215k markets, ~7 min)
.venv/bin/python -m rfqbot replay --from data/raw --to data/replay.db --overwrite
.venv/bin/python -m rfqbot kalshi-sync --sport nfl          # Kalshi game-level catalog -> kalshi_markets
.venv/bin/python -m rfqbot coverage --sport nfl             # parse and match coverage by family/period
.venv/bin/python -m rfqbot joint --game nfl-ne-chi-2026-10-23 --pairs
.venv/bin/python -m rfqbot joint-validate --offsets 15
.venv/bin/python -m rfqbot joint-calibrate --scores-db data/engine_validation_v2.db
```

Standalone module entry points (each has `--help`): `rfqbot.books`, `rfqbot.kalshi_books`
(`--stats`, `--refresh-matches`), `rfqbot.marginals --game <slug>`, `rfqbot.history
{pull,prices,resolve,pairs,reparse,stats}`, `rfqbot.props {pull-box,pull-lines,fit,stats}`,
`rfqbot.sports.nba_coverage`.

`listen` runs three loops: the websocket (auth, keepalive, reconnect with jittered backoff), a 1 s
expiry sweeper, and a catalog refresh (every 6 h; on-demand refreshes for unknown legs are
rate-limited to one per 30 min). `PAPER_TRADING=false` is rejected: there is no signing path.

## Architecture

```
RFQ_REQUEST (ws_client -> messages -> normalizer -> store: rfqs, combos, combo_legs)
  |
  v  listen --quote: quoting/assemble.py
  legs          store.get_market -> catalog rows (position_id, condition_id, slug)
  |
  clusters      rfqbot.clusters: same_market | same_group | same_game | no_game_key |
  |             shared_team_diff_game | disjoint   (deterministic, from slugs)
  |
  marginals     rfqbot.marginals.compute_game (quoting/cache.py refreshes per game):
  |               Polymarket book_top (books.db)  +  Kalshi kalshi_top via leg_matches (kalshi_books.db)
  |               matched through sports/* LegSpec + matcher -> precision-weighted p, err, tier, flags
  |
  dependence    same-game NFL cluster -> rfqbot.pricing.build_game(ladders) -> GameModel.joint(specs)
  |               -> p_joint, interval, calibrated p_cal (pricing/calibration.json)
  |             cross-game clusters -> product;  same_market/same_group -> 0;  no_game_key -> refused
  |
  margin        m = k_m*u_m + k_d*u_d + base_edge + inventory(0)   -> price for the maker's side, size caps
  |
  v
PaperQuote(price_e6, size_e6, source, reason) -> store.quotes + raw log RFQ_QUOTE frame
                                              -> data/decisions.db (every RFQ, every term, quoted or not)
```

`Listener(on_rfq=handler)` is the seam: the handler receives the parsed `RfqRequest` and the
`Store` and returns a `PaperQuote` or `None`. Without `--quote` the listener only records.

## Components

| module | what it does | CLI | notes |
|---|---|---|---|
| `rfqbot/ws_client.py`, `listener.py`, `messages.py`, `normalizer.py`, `store.py`, `rawlog.py`, `replay.py`, `catalog.py` | RFQ gateway client, lifecycle state machine, SQLite store, verbatim raw log, replay, combo-markets catalog | `rfqbot listen / stats / catalog / replay` | this file |
| `rfqbot/books.py`, `ladders.py` | CLOB book recorder for RFQ legs, widened to whole-game ladders for NFL/NBA; `ladders.game_markets(prefix, at_ms)` reads a ladder point-in-time | `rfqbot.books`, `rfqbot bookstats` | `docs/ladder-collector-brief.md` |
| `rfqbot/sports/` | table-driven parsers from venue markets to a sport-agnostic `LegSpec` (game, family, period, team, line, side, settlement rules); `matcher.match` -> `exact / rules_differ / shifted_line / none`; registry per sport | `rfqbot coverage --sport nfl`, `rfqbot.sports.nba_coverage`, `rfqbot kalshi-sync` | `docs/nfl-matching-notes.md`, `docs/nba-matching-notes.md` |
| `rfqbot/kalshi.py`, `kalshi_books.py` | Kalshi public-API client and catalog (`kalshi_markets`); live top-of-book sweep, depth polls, optional ticker websocket, persisted `leg_matches` | `rfqbot.kalshi_books [--stats]` | `docs/kalshi-feed-brief.md` |
| `rfqbot/marginals/` | per-leg fair probability and error from both venues: size-weighted microprice, spread + thinness + staleness sigma, precision weighting, tiers `ok/thin/wide/stale/disagree/skip` | `rfqbot.marginals --game <slug> [--at-ms]` | `docs/marginal-layer-notes.md` |
| `rfqbot/clusters.py` | leg independence classifier and cluster assembly | (library) | `docs/independence-classifier-brief.md` |
| `rfqbot/pricing/` | NFL joint score distribution from line ladders (isotonic ladder fit, IPF on binned targets, 1H/FG coupling, 50k scenarios), indicators for each family, shock interval, validation on resolved games, per-pair calibration | `rfqbot joint / joint-validate / joint-calibrate` | `docs/joint-engine-notes.md`, `docs/engine-validation-notes.md` |
| `rfqbot/quoting/` | combo semantics, margin and size, game cache, decisions log | `rfqbot listen --quote` | `docs/quote-assembly-notes.md` |
| `rfqbot/history/` | resolved games, markets, pre-kickoff price snapshots, leg pairs and `market_resolutions` from Gamma | `rfqbot.history pull / prices / resolve / pairs / reparse / stats` | `docs/history-dataset-brief.md` |
| `rfqbot/props/` | NBA player-prop dataset (ESPN box scores, Polymarket and Kalshi lines) and share-plus-residual fit; parameters in `rfqbot/props/props_fit.json`, not yet wired into pricing | `rfqbot.props pull-box / pull-lines / fit / stats` | `docs/props-fit-notes.md` |

## Data and storage

`data/` is gitignored; a clean checkout starts empty and the processes above fill it. Three test
files read recorded data and skip when it is absent: `tests/test_nfl_poly.py`,
`tests/test_nba_catalog.py`, `tests/test_quoting_semantics.py` (they parse every catalog row and
check combo semantics against recorded trades).

### What the feed looks like (measured 2026-10-20)

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
* Combo semantics (pinned in `rfqbot/quoting/semantics.py` from docs and recorded frames): YES
  pays 1 iff every leg wins; `side` is always YES; BUY means the maker sells YES at `price_e6`,
  SELL means the maker buys; BUY quotes above 0.90909 are rejected by the venue.

### RFQ store

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
  * `markets` — catalog of outcome positions (position_id → market, outcome, price, tags), ~215k rows
  * `kalshi_markets` — Kalshi game-level catalog with parsed `LegSpec` fields (`kalshi-sync`)
  * `market_resolutions` — written by `rfqbot.history resolve`
  * views `rfqs_v` (RFQ joined with requester and combo) and `rfq_legs` (legs joined with markets)
* `python -m rfqbot replay` rebuilds a database from the raw log; it is the entry point the
  backtest will use.
* `data/decisions.db` — one row per RFQ seen by `listen --quote` with legs, clusters, marginals,
  joint terms, margin, price, size and the skip reason when not quoted (the backtest's input).

### Leg order books (`rfqbot.books`)

The combo-markets catalog price is a placeholder, so pricing needs point-in-time leg books.
`books` reads the listener's `rfq.db` (read-only) for the leg `position_id`s of RFQs seen in the
last `BOOK_UNIVERSE_HOURS`, maps them to CLOB token ids, and subscribes to the CLOB market channel.

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
* `data/books/YYYY-MM-DD/HH.jsonl.gz` — every frame verbatim (same format as the RFQ raw log).
* `data/books.db`: `book_top` (one row whenever an asset's best bid/ask, size at best or depth
  within 0.05 changes; prices as integers ×10 000), `leg_trades`, `tick_sizes`, `subscriptions`
  (when each token was in the set), `leg_tokens`, `book_connections`. Deltas for a token without
  a snapshot are raw-logged but not normalized.

### Kalshi (`rfqbot.kalshi_books`, `data/kalshi_books.db`)

Market list, batch top-of-book and order books are public; keys only speed up the sweep and
enable the ticker websocket. `kalshi_top` is change-only (prices as YES probabilities, `at_ms`
local receive time is the point-in-time key); `leg_matches` holds one row per parsed Polymarket
leg with its Kalshi ticker, side and match confidence, rebuilt after each `kalshi-sync`.
Every response and frame goes to `data/kalshi/` verbatim (~0.5 GB/day).

### Historical dataset (`rfqbot.history`, `data/history.db`)

Resolved games from Gamma for engine validation and backtest P&L: `hist_games/markets/legs`
(`pull`), pre-kickoff CLOB price snapshots `hist_prices` (`prices --minutes-before 60,15,5`),
`market_resolutions` in `rfq.db` (`resolve`), every pair of legs per resolved game `hist_pairs`
(`pairs`). Series `nfl-2026`, `nba-2026`. Resolved = Gamma `umaResolutionStatus` resolved and
`outcomePrices` exactly 0/1 (0.5/0.5 = VOID). Gamma's `/markets?condition_ids=` returns only
open markets unless `closed=true`. `history.game_markets_hist(prefix, offset_min)` returns quotes
with bid = ask = snapshot price so `pricing.build_game` runs on history unchanged; the validation
run (93 NFL games, 141,819 pairs at offset 15) is in `docs/engine-validation-notes.md`.

Staleness note for both book stores: rows are written only on change, so the marginal layer
measures staleness from the feed's last confirmation, not from the row's age.

## Docs index

Each component was handed to a session as a brief and came back with a notes file: the brief
(`docs/*-brief.md`) is how the task was framed and what was verified before it started; the notes
(`docs/*-notes.md`) are what was built and what the data said.

| topic | brief | notes |
|---|---|---|
| NFL identity and Kalshi matching | `nfl-matching-brief.md` | `nfl-matching-notes.md` |
| NBA identity and Kalshi matching | `nba-matching-brief.md` | `nba-matching-notes.md` |
| whole-game ladder collector | `ladder-collector-brief.md` | (in this README, Leg order books) |
| Kalshi live feed and leg matches | `kalshi-feed-brief.md` | (module docstring, `rfqbot/kalshi_books.py`) |
| marginal layer | `marginal-layer-brief.md` | `marginal-layer-notes.md` |
| leg independence classifier | `independence-classifier-brief.md` | (module docstring, `rfqbot/clusters.py`) |
| NFL joint engine | `joint-engine-brief.md` | `joint-engine-notes.md` |
| engine validation and calibration | `engine-validation-brief.md` | `engine-validation-notes.md` |
| historical dataset | `history-dataset-brief.md` | (in this README, Historical dataset) |
| NBA player-prop dataset and fit | `props-dataset-brief.md` | `props-fit-notes.md` |
| quote assembly | `quote-assembly-brief.md` | `quote-assembly-notes.md` |

Also: `docs/future-work.md` (everything identified and not built, by area, with stages 3 and 4),
`docs/coupling-environment-study.md` (an idea recorded and deliberately not developed: validating
margin-to-total coupling against decades of scores by spread/total environment).

## Development workflow

The project was built by one planning session and several peer Claude Code sessions working in
parallel on the same checkout, coordinated by the briefs above:

* `.claude/skills/brief` and `.claude/skills/report` are the `/brief` and `/report` skills that
  fix the shape of a delegation brief and a completion report (shipped, numbers, corrections to
  the brief, surprises, not done) and write the matching `docs/` file.
* `.claude/hooks/ownership.py` (PreToolUse) maps each file to an owner label via
  `.claude/ownership.json`; a session claims its label with `scripts/claim-owner <label>`, and an
  edit to a file owned by someone else asks for human confirmation instead of proceeding.
* `.claude/hooks/run_tests.py` (PostToolUse) runs the test file matching an edited module and
  feeds failures back into the editing session.
* `scripts/run.sh` and `scripts/status.sh` keep the long-running processes to one instance each.
